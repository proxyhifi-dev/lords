# Lords Bot — End-to-End Master Document

A single reference that takes the system from understanding what it is, through fixing and validating it, to scaling it. Single-broker (Samco), Iron Condor focus, as scoped.

> This document covers software and strategy construction. It is not financial advice and does not guarantee any trading outcome. Every change below must be re-validated on paper before live capital.

**How to read this:** Parts 1–2 are the situation. Parts 3–9 are the system, section by section, each ending in concrete fixes. Part 10 is the prioritized backlog. Part 11 is the backtesting app. Part 12 is how you prove any of it works. Part 13 is scaling. Part 14 is the reference appendix.

---

## Table of contents

1. System overview & current architecture
2. Current-state scorecard
3. Strategy thesis — probability of profit vs expected value
4. Entry pipeline — specification, gaps, fixes
5. Exit pipeline — specification, gaps, fixes
6. Risk management — specification
7. Execution & order integrity — specification, fixes
8. Data & quotes layer
9. Persistence & observability
10. Prioritized fix backlog
11. Backtesting application — full build
12. Validation & go-live protocol
13. Scaling roadmap (single-broker)
14. Appendix — config reference & glossary

---

## Part 1 — System overview & current architecture

The bot is a single-process, async FastAPI application that trades NIFTY weekly Iron Condors (and a directional mode) through Samco. Everything runs as cooperating asyncio tasks in one event loop: a market scheduler, the trading engine, a risk manager, a reconciliation loop, and a health loop.

Core components:
- `TradingEngine` — entry/exit orchestration, monitoring, emergency handling.
- `IronCondorStrategy` — pure decision logic: Greeks, strike selection, PoP, scoring, exits, rolling, P&L, charges.
- `RiskManager` — independent risk gates and global stops.
- `ExecutionManager` + `OrderExecutionSequence` — order placement with a state machine and uncertain-order handling.
- `SamcoClient` — broker adapter over a synchronous SDK (wrapped in threads), with a rate limiter and circuit breaker.
- `StateManager` — single-row runtime state in SQLite (WAL) with optional Redis cache.
- `TradeStore` — trade log in CSV.
- `Reconciliation` — compares internal state against broker positions/orders.
- `OptionChainCollector` — periodic option-chain snapshots (the seed of your data pipeline).

The defining architectural fact: it is **single-everything** — one process, one active trade at a time, one instrument, one broker, REST polling. That is correct for today and is the backdrop for Parts 10 and 13.

---

## Part 2 — Current-state scorecard

Engineering robustness, not guaranteed profit. Overall **78/100 (B+)** — a strong, production-minded build with specific, fixable gaps.

| Dimension | Score | One-line |
|---|---|---|
| Entry logic | 88 | Exceptional multi-gate filtering; IV-rank is only an intraday proxy. |
| Risk management | 86 | Many independent stops, drawdown high-water mark, fail-closed, kill switch. |
| Execution integrity | 84 | Standout uncertain-state handling, leg rollback, reconciliation. Market-order slippage hurts. |
| Strategy / edge math | 80 | Correct PoP, Greeks, charge model. Static IV band + wing-width config bug. |
| Exit logic | 72 | Good backstops, but the partial scale-out ladder is effectively dead code. |
| Data & quotes | 70 | Degradation handling solid; 2s REST polling limits stop precision. |
| Persistence / data | 66 | Journaling + analytics good; CSV trade store is fragile. |

---

## Part 3 — Strategy thesis: probability of profit vs expected value

The single most important idea in this whole document: **a high win rate is not the goal — a high probability of profit combined with positive expected value after costs is.**

You can push win rate toward 90%+ by selling far OTM (low delta) and taking profit early, but each trade then collects tiny credit while still risking the full wing width, so rare losses erase many wins and the system goes negative-EV. Your code already understands this — the `ic_target_short_delta = 0.16` comment notes 16-delta beats 10-delta on premium-to-charge.

The levers that genuinely raise probability of success without destroying the edge, in order of impact:
1. **Entry selectivity** — `ic_min_entry_score` (raise 60 → 65). Fewer, higher-quality trades.
2. **Volatility regime** — only sell when IV is paid (`ic_min_iv_rank`, IV regime band). Built on a weak signal today; see Part 4.
3. **Manage winners early** — `ic_target_profit_pct` (0.35); exiting before price wanders into strikes lifts hit rate.
4. **Avoid expiry gamma** — `ic_force_exit_dte` (1.0). Do not loosen.
5. **Short delta** — the master dial. Keep 0.14–0.18; never below 0.12.

EV peaks at a moderate delta and a managed profit target, and collapses if you chase win rate alone.

---

## Part 4 — Entry pipeline (specification, gaps, fixes)

### Specification (the gates, in order)
1. Trading enabled, weekend block, entry window.
2. Active-trade guard (one position at a time).
3. Expiry-day handling + one-day-before-expiry cutoff (fail-closed on lookup error).
4. Cadence: one-per-day (cross-checked across four date fields) or monthly window.
5. Gap filter (skip if spot has moved too far from session open).
6. Regime filter (`evaluate_entry_regime`).
7. IV-rank window.
8. Strike selection: delta-targeted when live IV + DTE known; IV-adaptive fixed-distance fallback otherwise.
9. Expected-move filter (short distance must clear the expected move + buffer).
10. Composite score gate (`score_entry` ≥ `ic_min_entry_score`).
11. Slippage-adjusted credit and economics viability (`is_entry_credit_viable`).
12. Margin check and final risk validation.

This layering is excellent — capital is only committed after a dozen independent checks pass.

### Gaps
- **"IV rank" is an intraday proxy.** `_compute_session_iv_rank` ranks IV only within the current session's deque. The proper `evaluate_iv_rank` (takes 52-week high/low) is unused. The 10% rank weight and the regime rank gate rest on a weak signal.
- **Trend-strength window is ~60 seconds** (`_compute_trend_strength` efficiency ratio) — too short to judge a directionless day; noisy.
- **Static IV regime band** (15–25%, `assumed_iv = 0.15`) — NIFTY's baseline IV moves; a fixed band misclassifies.

### Fixes
1. Build a historical ATM-IV / India-VIX store; compute a real rolling IV rank; wire `evaluate_iv_rank` to it. Biggest entry-quality lift.
2. Replace the 60s efficiency ratio with a longer-horizon trend filter (intraday ADX or multi-window efficiency over the morning session).
3. Make the IV regime band a percentile of recent IV rather than absolute.

---

## Part 5 — Exit pipeline (specification, gaps, fixes)

### Specification (triggers)
- Profit `TARGET` at 35% of credit.
- `STOP_LOSS` at 1.6× entry credit; `EXTREME_LOSS` at 2.4×.
- Spot-proximity / breach exits with a noise buffer (require spot past the strike by a buffer before confirming).
- 1-DTE `GAMMA_RISK_EXIT`.
- EOD decision branch: profit-lock / no-positive-target / loss-cut / EOD.
- Ratchet-to-breakeven (arms at 50% profit).
- Partial scale-out ladder (25% / 50% / 75%).
- Leg rolling for threatened shorts (`should_roll_leg`).

### Gaps (where the marks are lost)
- **The partial scale-out is effectively dead code.** `get_exit_reason` returns `TARGET` at 35% profit and the engine closes the *entire* position — before the 50%/75% partial levels can ever be reached. The 25% branch only logs, the 50% branch only arms the ratchet (also never reached), and partial-quantity exits never run. The docstrings describe scale-out that does not happen.
- **Stops are premium-based and poll-gated.** "Premium ≥ 1.6× credit" checked every ~2s can be gapped through on a fast move; proximity/breach is the sturdier stop but is ordered secondary.

### Fixes
1. Decide the exit design explicitly: either keep the clean 35% full exit and **delete the unused partial/ratchet machinery**, or **implement real partial-quantity exits** and reorder thresholds so the ladder fires before the full target. Match code to docstrings.
2. Promote the spot-based hard stop (proximity/breach) above the premium multiple — far harder to gap through.
3. Tighten in-trade monitoring cadence (event-driven on ticks, or a shorter poll while a position is open).

---

## Part 6 — Risk management (specification)

Independent, layered stops, all of which can disable trading:
- Max daily loss (`max_daily_loss`).
- Max trades per day (`max_trades`).
- Consecutive-loss limit (`max_consecutive_losses`, default 3).
- Max drawdown (`max_drawdown_pct`, 20%) against a maintained peak-equity high-water mark.
- Per-trade max loss (`ic_max_loss_per_trade`).
- Margin / equity check before entry.
- Global risk stop and fail-closed on fatal exceptions; environment kill switch (`TRADING_KILL_SWITCH`).

This is a strength. The one dependency: `max_potential_loss = (spread_width − net_premium) × order_qty` is only accurate once wing width is pinned (Part 10, fix #1).

---

## Part 7 — Execution & order integrity (specification, fixes)

### Specification (the standout area)
- Order state machine with an explicit `ORDER_UNCERTAIN` state; partial/unknown fills are escalated, never assumed filled.
- `_rollback_iron_condor` reverses filled legs on a later-leg failure; partial rollback raises a critical event and triggers emergency flatten.
- `_ensure_position_closed` re-verifies against broker positions with retries after exits; `_validate_post_order_position` checks fills after entry.
- Emergency-flatten path with order proof; reconciliation loop comparing internal vs broker state.
- Broker-side rate limiter enforcing a minimum inter-call interval.

### Gaps
- **Bare market orders** (`orderType = MARKET`, `productType = MIS`) with no price protection. On a narrow-wing condor with small credit, uncontrolled slippage across eight fills per round trip is a steady, invisible EV drain.
- **Leg-in risk on entry** — four legs placed sequentially (longs first, correct order), but the market can move between legs and realized net credit can differ; rollback handles failures, not adverse fills.
- Per-leg sequential quote fetches through a synchronous SDK add exit latency exactly when speed matters.

### Fixes
1. **Add price protection** — replace market orders with marketable-limit orders (limit banded around LTP) using the existing retry/uncertain machinery. Likely the largest realized-EV gain.
2. Reduce leg-in risk — use a multi-leg/basket order if Samco supports it; otherwise minimise inter-leg latency and watch for adverse fills.
3. Move broker calls to a fully async client or batch quote fetches so exit latency does not scale with leg count.

---

## Part 8 — Data & quotes layer

REST polling (`poll_seconds = 1`, monitor sleeps ~2s); per-leg sequential quotes; 3s last-good cache; `model_fallback` pricing; degradation tracking with warn/critical escalation and a lockdown path. Auto-exits are blocked on degraded pricing except time-based EOD exits — the right safety trade-off.

Fixes: add a WebSocket tick feed into an in-memory/Redis cache the engine reads from (keep the existing cache as a freshness layer); make in-trade monitoring event-driven off ticks.

---

## Part 9 — Persistence & observability

- Event journaling of TRADE_ENTRY / TRADE_EXIT; dashboard; an analytics endpoint computing win rate, profit factor, Sharpe, Sortino, max drawdown, and Kelly fraction from the trade log; reconciliation status surfaced via API.
- **Weakness:** trades are stored in CSV, and the volume of repair code in `main.py` (`_repair_shifted_trade_row`, column-shift detection, charge re-derivation) is evidence the CSV schema has drifted and corrupted rows. Your analytics — how you measure the edge — sits on a fragile store. Runtime state is a single JSON blob in one SQLite row.

Fix: move trades to a typed table (SQLite/Postgres) with explicit columns and types; keep the journaling, give it a schema.

---

## Part 10 — Prioritized fix backlog

1. **Pin wing width / short distance** so `config_loader.py` and `iron_condor_strategy.py` agree. `ic_wing_width` is 50 in config but 150 in the strategy fallback (and `ic_short_distance` 200 vs 250). Wing width *is* max loss per trade — this is a correctness bug, not a tuning choice. Do this first.
2. **Add limit/price protection to orders** (Part 7). Biggest realized-EV lever.
3. **Resolve the dead exit ladder** (Part 5) — implement real partials or delete the unused machinery.
4. **Build a historical IV-rank store** and wire `evaluate_iv_rank` (Part 4). Biggest entry-quality lever.
5. **Promote the spot-based hard stop** and tighten in-trade monitoring cadence (Parts 5, 8).
6. **Move the trade store to a typed table** (Part 9) so edge measurement is reliable.
7. **Add streaming market data** feeding a shared cache (Part 8).

---

## Part 11 — Backtesting application (full build)

### Why a separate app
It must never touch live orders or live state; it runs at a different rhythm (replaying months in seconds); and you want to iterate freely without risking the live bot.

### The one rule that matters most
Both the live bot and the backtester import the **same** strategy module. `iron_condor_strategy.py` is already pure decision logic (`score_entry`, `calculate_strikes_by_delta`, `get_exit_reason`, `should_roll_leg`, `compute_pnl`). If the backtester re-implements any of it, you are testing different code than you trade. Import the real thing.

### Pipeline
`Collector → Data store → Backtest engine → Report`, with the shared strategy core consumed by both the backtest engine and the live bot.

### Data collection requirements (extend `OptionChainCollector`)
Your collector already snapshots CE/PE bid/ask/LTP/volume per strike every 60s. Add:
- **Spot price** on every snapshot. Required — the entry logic needs spot at decision time.
- **IV** (per-strike or ATM, plus India VIX if available). Required for delta/PoP/score replay.
- **Full strike range** — capture the whole chain across roughly ATM ± 700 points, not just ATM, so all four legs are present.
- Optionally the relevant expiries if you want to test across DTEs.

Suggested snapshot schema (one row per strike per timestamp):

| Field | Notes |
|---|---|
| timestamp | UTC ISO, the decision clock |
| expiry | weekly expiry date |
| spot | index spot at snapshot |
| atm_iv / india_vix | volatility inputs |
| strike | int |
| ce_bid, ce_ask, ce_ltp | call quotes |
| pe_bid, pe_ask, pe_ltp | put quotes |
| ce_iv, pe_iv | per-leg IV if available |
| volume, oi | liquidity context |

Roll daily CSV/JSONL into **Parquet** (or load into **DuckDB**) so the backtester scans months in seconds.

### Fill model (make-or-break)
Never backtest on mid or LTP. For each leg:
- Sells fill near the bid, buys fill near the ask (the conservative side), plus a slippage allowance.
- Apply the same round-trip charges you already compute (`estimate_round_trip_charges` / `compute_pnl`).
- This is the same market-order slippage flagged in Part 7 — the backtest must include it or the equity curve will lie.

### Methodology / pitfalls
- **No lookahead** — at each decision timestamp, only read snapshots at or before that time.
- **Expiry rollover** — handle weekly rollover; skip or penalise illiquid / zero-quote strikes.
- **Same clock** — replay at the cadence the live monitor uses so exits trigger comparably.
- **Forward-data reality** — collecting now only yields *future* data; a statistically meaningful sample is weeks–months away. To test past periods you need historical NIFTY intraday option-chain data from a vendor (verify current providers/pricing separately).

### Output
Print the same metrics as `/api/analytics` (win rate, profit factor, Sharpe, Sortino, max drawdown, Kelly) so a backtest result is directly comparable to a live result.

---

## Part 12 — Validation & go-live protocol

No change in this document is proven by code alone. Sequence:
1. Make changes behind config flags; keep `MODE=paper`.
2. Run a meaningful paper sample (target 50–100 trades) with `PAPER_MODE_USE_BROKER` so quotes are real.
3. Read `/api/analytics`. Go-live gates (guidelines, not promises):
   - Profit factor comfortably above ~1.3.
   - Positive expected value per trade after costs.
   - Positive Kelly fraction; size live at no more than half-Kelly.
   - Realized win rate not far below modelled PoP — if it is, the gap is slippage/timing (fixes #2 and #5).
4. Cross-check the backtest and the paper run agree directionally. Divergence means the fill model or data is wrong.
5. Go live small, keep every risk gate and the kill switch armed, and re-check the same metrics weekly.

---

## Part 13 — Scaling roadmap (single-broker)

Multi-broker and multi-account are out of scope by your choice. The relevant axes:

- **More concurrent positions** — the prerequisite for everything. Replace the single `active_trade` + one `_trade_lock` with a position-keyed portfolio model and per-position locks; aggregate risk at the portfolio level. High effort, high payoff.
- **More instruments** (BANKNIFTY, FINNIFTY) — extract NIFTY-specific constants into a per-instrument registry (lot size, strike step, expiry calendar, session); make strategy params per-instrument.
- **Faster data** — the streaming feed from Part 8; turns polling latency from a constraint into a non-issue.

Target shape: split the single process into a market-data service, strategy/engine workers, a portfolio+risk service, and an execution gateway, all over a shared durable substrate (typed DB + event log). Each layer then scales on its own axis. Sequence: harden (Part 10) → positions → instruments → streaming.

The hard part of scaling here is not throughput — it is re-proving the safety invariants (rollback, reconciliation, position verification, kill switch) at the new concurrency level. A bug in a web app is a 500; here it is a naked option leg.

---

## Part 14 — Appendix

### Key config reference (current values)

| Setting | Value | Meaning |
|---|---|---|
| `ic_target_short_delta` | 0.16 | ~80% modelled PoP |
| `ic_min_entry_score` | 60 | composite score gate (0–100) |
| `ic_target_profit_pct` | 0.35 | exit at 35% of credit |
| `ic_stop_loss_multiple` | 1.60 | stop when premium ≥ 1.6× credit |
| `ic_extreme_loss_multiple` | 2.40 | hard backstop |
| `ic_force_exit_dte` | 1.0 | force exit under 1 DTE (gamma) |
| `ic_min_iv_rank` | 0.50 | regime IV-rank floor |
| `ic_short_distance` | 200 (config) / 250 (strategy) | **inconsistent — fix** |
| `ic_wing_width` | 50 (config) / 150 (strategy) | **inconsistent — IS max loss — fix** |
| `assumed_iv` | 0.15 | IV fallback / regime anchor |
| `poll_seconds` | 1 | quote poll cadence |
| `max_drawdown_pct` | 0.20 | global drawdown stop |
| `max_consecutive_losses` | 3 | loss-streak stop |

### Glossary
- **PoP** — probability of profit: chance the position finishes between the credit-adjusted breakevens.
- **EV** — expected value per trade after costs; the real target alongside PoP.
- **Delta** — option's sensitivity to spot; ~16-delta short ≈ ~1 standard deviation ≈ ~80% PoP.
- **Wing width** — distance between short and long strike on one side; defines maximum loss.
- **Theta / Vega** — daily time-decay earned / sensitivity to IV; the score rewards theta per unit vega.
- **Roll** — buying back a threatened short and selling a further-OTM one to extend the trade.
- **Lookahead bias** — using data not yet available at the decision time; invalidates backtests.

*End of document.*
