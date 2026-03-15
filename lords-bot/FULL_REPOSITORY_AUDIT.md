# Lords Bot – Full Repository Audit

## 1) Repository Architecture Overview

Current implementation is a **mixed architecture** with two partially overlapping stacks:

- Active path (used by FastAPI app):
  - `backend/main.py` → `backend/engine/scheduler.py` → `backend/services/*` → `backend/strategies/orb_strategy.py` → `backend/risk/risk_manager.py` → `backend/engine/order_manager.py` → `backend/brokers/samco_client.py`.
- Legacy/parallel path (still in repo, partially broken):
  - `backend/runtime_state.py` + `backend/trading/*` + `backend/services/dashboard_service.py` + `backend/services/analysis_service.py`.

This split introduces maintainability risk and runtime drift (several legacy modules reference missing config or client methods).

---

## 2) Critical Bugs

### Bug C1 — `analysis_ttl` missing in config
- **File:** `backend/services/analysis_service.py`
- **Problematic code:**
```python
self.cache.set(key, result, settings.analysis_ttl)
```
- **Corrected code:**
```python
# Option A: add field in Settings
analysis_ttl: int = 30

# Option B: fallback for safety
self.cache.set(key, result, getattr(settings, "analysis_ttl", 30))
```
- **Why:** `Settings` does not define `analysis_ttl`; calling this path raises `AttributeError`.

### Bug C2 — `funds_ttl` missing + invalid Samco client method
- **File:** `backend/services/funds_service.py`
- **Problematic code:**
```python
payload = await samco_client.get_funds()
self.cache.set('funds', payload, settings.funds_ttl)
```
- **Corrected code:**
```python
payload = await samco_client.get_limits()
self.cache.set('funds', payload, getattr(settings, 'funds_ttl', 10))
```
- **Why:** `SamcoClient` implements `get_limits()`, not `get_funds()`; `funds_ttl` is also undefined.

### Bug C3 — `profile_ttl` missing + invalid Samco client method
- **File:** `backend/services/profile_service.py`
- **Problematic code:**
```python
payload = await samco_client.get_profile()
self.cache.set('profile', payload, settings.profile_ttl)
```
- **Corrected code:**
```python
payload = await samco_client.user_details()
self.cache.set('profile', payload, getattr(settings, 'profile_ttl', 10))
```
- **Why:** `SamcoClient` implements `user_details()`, not `get_profile()`; `profile_ttl` missing.

### Bug C4 — compile check scans vendored virtualenv
- **File:** repository layout (`backend/.venv` committed)
- **Problematic pattern:** static tooling recurses into `backend/.venv`.
- **Corrected approach:** ignore `.venv` in repo and tooling (`.gitignore`, lint/test include paths).
- **Why:** slows QA/static analysis and can mask project-only issues.

---

## 3) Strategy Logic Errors

### Bug S1 — Live signal path bypasses ORB+RSI logic entirely
- **Files:** `backend/engine/strategy_engine.py`, `backend/services/signal_service.py`
- **Problematic code:** signal generation based only on PCR thresholds.
- **Corrected code (concept):**
```python
if spot > orb_high and rsi >= 55 and bias == 'BULLISH':
    signal = 'BUY CALL'
elif spot < orb_low and rsi <= 45 and bias == 'BEARISH':
    signal = 'BUY PUT'
else:
    signal = 'NO TRADE'
```
- **Why:** primary ORB requirements are not enforced in this branch.

### Bug S2 — ORB accepts `NEUTRAL` bias for both directions
- **File:** `backend/strategies/orb_strategy.py`
- **Problematic code:**
```python
option_chain_bias in {"BULLISH", "NEUTRAL"}
option_chain_bias in {"BEARISH", "NEUTRAL"}
```
- **Corrected code:**
```python
option_chain_bias == "BULLISH"  # for CALL
option_chain_bias == "BEARISH"  # for PUT
```
- **Why:** violates stricter signal conditions provided.

### Bug S3 — Risk distance artificially capped at 3% of spot
- **File:** `backend/strategies/orb_strategy.py`
- **Problematic code:**
```python
risk = min(risk, max(1.0, spot_price * 0.03))
```
- **Corrected code:**
```python
risk = abs(spot_price - stop_loss)
```
- **Why:** requested formula uses direct ORB range-derived risk; capping alters SL/target math.

### Bug S4 — Exit RSI logic can force premature closes unrelated to ORB plan
- **File:** `backend/engine/scheduler.py`
- **Problematic code:** exits CALL at RSI>=70 and PUT at RSI<=30 regardless of RR plan.
- **Corrected approach:** keep exit based on stop/target/time unless explicitly configured.
- **Why:** may reduce expected edge and distort backtest/live parity.

---

## 4) Market Data Issues

### Bug M1 — Market hours use server local time, not IST
- **File:** `backend/engine/scheduler.py`
- **Problematic code:**
```python
now = datetime.now()
```
- **Corrected code:**
```python
from zoneinfo import ZoneInfo
now = datetime.now(ZoneInfo("Asia/Kolkata"))
```
- **Why:** deployment timezone mismatch can trade outside NSE hours.

### Bug M2 — ORB range from tick-derived candles only; no session boundary reset in candle buffer
- **Files:** `backend/engine/scheduler.py`, `backend/engine/candle_builder.py`
- **Problematic behavior:** persistent in-memory tick list may carry stale buckets until pruned.
- **Corrected approach:** reset intraday buffers at day rollover and key candles by session date.
- **Why:** stale data can taint first 30-minute ORB window on restart/long process.

### Bug M3 — Duplicate candle generators (`CandleBuilder` and `MarketDataService.get_historical_candles`)
- **Files:** `backend/engine/candle_builder.py`, `backend/services/market_data_service.py`
- **Why:** two implementations can diverge in OHLC semantics and lead to inconsistent analytics.

---

## 5) Broker Integration Issues

### Bug B1 — Broker success mapping mismatch can block entries
- **File:** `backend/engine/scheduler.py`
- **Problematic code:**
```python
if verification.get("order_status") in {"COMPLETE", "FILLED"}:
```
- **Corrected code:**
```python
status = str(verification.get("order_status") or verification.get("status") or "").upper()
if status in {"COMPLETE", "FILLED", "SUCCESS"}:
```
- **Why:** `SamcoClient.place_order()` may return success without a later filled `order_status` key.

### Bug B2 — No guard for empty/invalid `order_id`
- **File:** `backend/engine/scheduler.py`
- **Problematic behavior:** always calls verify even if placement response misses ID.
- **Corrected approach:** validate `order_id` and handle rejection path before verification call.

### Bug B3 — Real-mode payload schema not validated against Samco required fields
- **File:** `backend/engine/order_manager.py`
- **Why:** live order fields are hand-built without schema validation; broker-side rejections likely.

---

## 6) Risk Management Weaknesses

### Bug R1 — Circuit breaker inputs hardcoded healthy
- **File:** `backend/engine/scheduler.py`
- **Problematic code:**
```python
system_status = self.risk_manager.circuit_breaker(self.state, broker_ok=True, api_ok=True)
```
- **Corrected code:**
```python
system_status = self.risk_manager.circuit_breaker(
    self.state,
    broker_ok=last_broker_call_ok,
    api_ok=last_data_call_ok,
)
```
- **Why:** breaker can never trip for real connectivity instability.

### Bug R2 — capital hardcoded to 100000
- **File:** `backend/engine/scheduler.py`
- **Problematic code:**
```python
capital=100000.0
```
- **Corrected approach:** derive from available broker funds (or configured paper capital).
- **Why:** invalid sizing during both paper and real trading.

### Bug R3 — Risk model uses spot/ORB levels for options premium position sizing
- **Files:** `backend/strategies/orb_strategy.py`, `backend/risk/risk_manager.py`, `backend/engine/scheduler.py`
- **Why:** option premium risk per lot differs from underlying point risk; size can be materially wrong.

---

## 7) Scheduler Problems

### Bug SCH1 — `tick()` lock serializes but lacks overrun protection/metrics
- **File:** `backend/engine/scheduler.py`
- **Why:** if one tick runs longer than interval, backlog/latency accumulates silently.

### Bug SCH2 — Chain refresh cadence ties to `option_chain_ttl` only; no API budget governor
- **File:** `backend/engine/scheduler.py`
- **Why:** no global request quota/backoff per minute; risk of burst calls after retries.

### Bug SCH3 — Day reset handled only at state load
- **Files:** `backend/engine/state_manager.py`, `backend/engine/scheduler.py`
- **Why:** long-running process may keep yesterday state until restart.

---

## 8) UI Data Issues

### Bug UI1 — Dashboard reflects only scheduler state, not broker-validated positions
- **File:** `backend/api/routes_dashboard.py`
- **Why:** UI can show stale/optimistic trade status after broker rejects/partial fills.

### Bug UI2 — No endpoint-level schema contracts (response models)
- **Files:** all `backend/api/routes_*.py`
- **Why:** shape drift can break frontend silently and makes QA hard.

### Bug UI3 — `flatten` only clears local state, does not send broker exit order
- **File:** `backend/api/routes_trade.py`
- **Problematic code:**
```python
scheduler.state.active_trade = {}
```
- **Corrected approach:** place opposite market order in REAL mode before local state mutation.

---

## 9) Performance Problems

### Bug P1 — Blocking sleeps in Samco wrapper thread worker path
- **File:** `backend/brokers/samco_client.py`
- **Why:** `time.sleep` in `_call` plus to_thread can still bottleneck high-frequency polling.

### Bug P2 — Recomputing RSI from full candle history every tick
- **Files:** `backend/engine/scheduler.py`, `backend/strategies/orb_strategy.py`
- **Why:** avoidable repeated O(n) work; use incremental RSI state.

### Bug P3 — JSONL trade log loaded fully for each dashboard request
- **File:** `backend/services/trade_logger.py`
- **Why:** linear disk read on each request; degrade with history growth.

---

## 10) Security Risks

### Risk SEC1 — CORS wide open with credentials enabled
- **File:** `backend/main.py`
- **Problematic code:**
```python
allow_origins=['*'], allow_credentials=True
```
- **Corrected code:**
```python
allow_origins=["https://<trusted-ui-domain>"], allow_credentials=False
```
- **Why:** permissive cross-origin policy is unsafe for trading controls.

### Risk SEC2 — No auth/authorization on trade control endpoints
- **Files:** `backend/api/routes_trade.py`, `backend/api/routes_trading_mode.py`
- **Why:** anyone who can reach API can flatten, reset, or switch modes.

### Risk SEC3 — Secrets handling and observability gaps
- **Files:** `backend/config.py`, `backend/brokers/samco_client.py`
- **Why:** no explicit secret vault integration/rotation path; operational risk for real trading.

---

## Priority Remediation Plan

1. Remove/bypass broken legacy stack (`services/analysis_service.py`, `funds_service.py`, `profile_service.py`, `trading/*`) or make it fully consistent.
2. Enforce timezone-aware IST clock and daily state reset inside scheduler loop.
3. Make ORB+RSI+bias logic the single source of truth (eliminate PCR-only alternate signal path).
4. Harden live-order flow: strict payload schema, resilient status mapping, rejection handling, and true flatten exits.
5. Add API auth, tighten CORS, and add structured response models.
6. Add integration tests for market-hours guard, ORB window, strike symbol format, and real-mode safety gates.
