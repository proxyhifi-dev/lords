# Lords Bot Comprehensive Audit (2026-04-22)

## Scope mapped to current repository
Requested files had some path/name drift in this codebase. I audited the current equivalents:

- `backend/app/main.py` → `backend/main.py`
- `backend/app/broker/samco_broker.py` → `backend/app/broker/samco_client.py`
- `backend/app/broker/risk_manager.py` → `backend/app/risk/risk_manager.py`
- `backend/app/engine/execution_engine.py` → `backend/app/engine/trading_engine.py`
- `backend/app/strategy/orb_strategy.py` (exists)
- `backend/app/utils/option_selector.py` → `backend/app/strategy/option_selector.py`
- `backend/app/scheduler/scheduler.py` → `backend/app/scheduler/market_scheduler.py`
- `backend/app/utils/event_bus.py` → `backend/app/core/event_bus.py`

## Phase 1 — Code Audit (prioritized)

### CRITICAL

1) **Expiry format/API mismatch can break option chain lookup**
- Location: `backend/app/broker/samco_client.py`
- Why: `get_expiry_api()` returns ISO date (`YYYY-MM-DD`) while your prior incident notes and SAMCO docs integration expect `DDMMMYYYY`. This is consistent with observed `Option chain empty` log failures.
- Corrective snippet:

```python
# backend/app/broker/samco_client.py

def get_expiry_api() -> str:
    # Example: 21APR2026
    return get_weekly_expiry().strftime("%d%b%Y").upper()
```

2) **Trade sizing can exceed configured lot-size semantics**
- Location: `backend/app/engine/trading_engine.py`, `_get_qty`
- Why: rounding is hardcoded to 25 even though NIFTY lot size is configuration-dependent and currently `.env` may set 50/65/etc. This can produce invalid exchange quantities.
- Corrective snippet:

```python
def _get_qty(self, size_label: str) -> int:
    lot = settings.order_qty  # use configured lot as atomic unit
    if size_label == "FULL":
        return lot
    if size_label == "MEDIUM":
        return max(int(0.75 * lot), 1)
    if size_label == "HALF":
        return max(int(0.50 * lot), 1)
    return lot
```

### HIGH

3) **No fill-confirm check on sell exits**
- Location: `backend/app/engine/trading_engine.py` in `_exit_remaining` and `_exit_trade`
- Why: BUY side confirms fill, but SELL exits do not enforce confirmation. This can cause phantom flat positions in state while broker order is rejected/pending.
- Corrective snippet:

```python
sell_id = sell_resp.get("orderNumber") or sell_resp.get("orderId") or sell_resp.get("order_id")
if not sell_id:
    logger.error("SELL rejected — position may be open! resp=%s", sell_resp)
    return
if not await self.broker.confirm_fill(sell_id):
    logger.error("SELL not confirmed filled id=%s — position retained", sell_id)
    return
```

4) **Signal storm when entries are repeatedly skipped**
- Location: `backend/app/scheduler/market_scheduler.py`
- Why: `_last_signal_time` is updated after `SIGNAL` publish, but when Risk/Engine skip due filters (e.g., low volume), scheduler keeps emitting new signals every minute. Logs show this repeated behavior.
- Corrective snippet:

```python
await self.event_bus.publish("SIGNAL", payload)
self._last_signal_time = now_ts
# Also optionally set state.signal with short TTL to prevent churn until next candle confirm
```

5) **Time-zone ambiguity (IST market logic using server local time)**
- Locations: scheduler, strategy, risk/trading time checks
- Why: all checks use naive `datetime.now()`. If deployment host timezone != IST, ORB window and cutoffs drift.
- Corrective snippet:

```python
from zoneinfo import ZoneInfo
IST = ZoneInfo("Asia/Kolkata")
now = datetime.now(IST)
```

### MEDIUM

6) **Option volume extraction misses nested SAMCO payloads**
- Location: `backend/app/engine/trading_engine.py`
- Why: `vol` only reads top-level keys; SAMCO often nests quote fields.
- Corrective snippet:

```python
details = quote.get("quoteDetails") or quote.get("data") or {}
if isinstance(details, list):
    details = details[0] if details else {}
vol = int(details.get("tradedVolume") or details.get("volume") or quote.get("tradedVolume") or 0)
```

7) **EventBus swallows exceptions silently**
- Location: `backend/app/core/event_bus.py`
- Why: broad `except: pass` can hide systematic subscriber failures.
- Corrective snippet:

```python
except Exception as exc:
    logger.warning("event dispatch failure type=%s err=%s", event.type, exc)
```

8) **`OrbStrategy` class is effectively dormant/inconsistent with scheduler logic**
- Location: `backend/app/strategy/orb_strategy.py`
- Why: scheduler runs independent ORB logic; this class may diverge over time and create conflicting behavior if enabled accidentally.
- Corrective action: either remove or formally integrate and test behind feature flag.

### LOW

9) **Root route returns `FileResponse` from unvalidated `frontend_dir` path**
- Location: `backend/main.py`
- Why: low risk in local deployment, but still better to fail fast on missing frontend with explicit status codes.

10) **Broad bare excepts present in a few hot paths**
- Location: `trading_engine.py`, `event_bus.py`
- Why: reduces observability in production incidents.

---

## Phase 2 — Configuration Validation

### Current profile from `.env`
- Capital: 10,000
- Max daily loss: 500 (5% of capital/day)
- Qty: 65
- SL: 25%, T1: +30%, T2: +100%, trailing: 15%
- Max trades: 2

### Risk assessment
- **Overall:** **Moderate-to-aggressive** for options intraday.
- 5% daily stop is acceptable for aggressive paper testing, but high for live compounding unless strict kill-switch is proven.
- Two trades/day cap is conservative and good.

### Break-even assumption
Using a 2-stage take-profit profile (50% at +30%, 50% at +100%), expected win payoff is roughly +65% premium vs full-loss at -25% (ignoring trailing/EOD effects).
Approximate break-even win rate:

\[
p \cdot 0.65 + (1-p) \cdot (-0.25)=0 \Rightarrow p \approx 27.8\%
\]

With slippage, spread, and occasional failed fills, practical break-even is likely closer to **32–38%**.

### Recommended parameter adjustments (live-hardening)
- Set `MAX_DAILY_LOSS` to **2–3%** of capital initially.
- Keep `MAX_TRADES=2` until 20+ trading sessions stable.
- Increase `SIGNAL_COOLDOWN` to 120 sec if repeated churn persists.
- Keep `MIN_OPTION_VOLUME >= 500`; if zero-volume quote artifacts persist, add OI/last trade freshness filters.
- Ensure `NO_ENTRY_AFTER` remains <= 13:30 and `SQUARE_OFF` <= 15:10 IST.

---

## Phase 3 — Data Validation

### `data/trades.csv`
- Contains only header, **0 trade rows**.
- Verdict: **insufficient data** for P&L and realism checks.

### `logs/bot.log` highlights
- Repeated `Option chain empty` (Apr 15, 2026) suggests expiry format/day logic mismatch.
- Repeated `Low volume 0 — skip` (Apr 22, 2026) suggests quote parser for volume fields needs strengthening.
- Daily reset appears to trigger correctly around 09:14.

### Real vs synthetic verdict
- Logs look structurally real (session/login rhythms, timestamped lifecycle), but with no closed trades, statistical validation is not possible.

---

## Phase 4 — Testing Strategy (pytest)

Added executable tests in `tests/test_lords_bot_audit_plan.py` covering:
- ORB strike/option mapping unit tests
- SAMCO parse helpers (`parse_spot`, `parse_ltp`)
- EventBus publish/subscribe flow
- RiskManager block/approve behavior
- Candle builder behavior in scheduler

Integration/stress test recommendations are documented in test file TODO markers.

---

## Phase 5 — Production readiness checklist status

- [ ] Paper trading runs 5+ full sessions without restarts
- [x] Orders are logged with timestamps
- [ ] P&L reconciled with broker statements
- [x] Daily loss gate exists
- [ ] Square-off reliability under API failures proven
- [ ] Alerting (email/SMS/Telegram) configured
- [ ] Backup/restore drill executed
- [ ] API rate-limit behavior tested under load
- [ ] Network partition handling tested
- [x] Manual flatten endpoint exists

## Production readiness score
**6.2 / 10**

Main blockers: expiry format compliance, sell-fill confirmation, timezone hardening, and lack of real trade dataset validation.
