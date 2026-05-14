# LORDS BOT Gap Report

Date: 2026-05-14

This report reflects the current audited state after the latest safety, reconciliation, strategy, storage, and dashboard patches.

## CRITICAL

### `backend/app/engine/reconciliation.py`
- Function/Class: `ReconciliationEngine._force_sync_from_broker`
- Issue: force-sync can still reconstruct a generic active trade from broker positions without restoring full Iron Condor leg truth.
- Live-trading impact: broker truth may be recognized, but restored local state can lose strategy-specific structure and confuse operator decisions.
- Fix plan: rebuild Iron Condor position model from broker legs/order book when possible, otherwise keep entries disabled and require manual review.

### `backend/app/scheduler/market_scheduler.py`
- Function/Class: `_flatten_iron_condor_trade`, `flatten_position`
- Issue: flatten path relies on engine exit workflow and does not yet independently verify all 4 IC legs are flat at broker after an emergency.
- Live-trading impact: partial broker-side exposure could survive a fail-safe event.
- Fix plan: add post-flatten broker position verification loop for IC legs and escalate to `emergency_flatten_all` if any open quantity remains.

## HIGH

### `backend/app/broker/samco_client.py`
- Function/Class: `SamcoClient.login`, `_call_sdk`
- Issue: auth/session handling is improved, but still depends on broker response patterns and has no persistent token-refresh lifecycle proof.
- Live-trading impact: session churn can still interrupt quote/execution continuity.
- Fix plan: add broker-session telemetry, consecutive auth-failure lockout status, and supervised restart validation.

### `backend/app/engine/trading_engine.py`
- Function/Class: `_monitor_iron_condor_trade`
- Issue: model fallback is now fail-closed, but recovery and operator workflow after degradation still need more end-to-end proof.
- Live-trading impact: quote disruptions remain a serious operational risk during active trades.
- Fix plan: add explicit stale-quote fail-safe path tests and broker-side flatten verification under degraded pricing.

### `backend/app/storage/trade_store.py`
- Function/Class: `_repair_shifted_row`, `_normalize_loaded_row`
- Issue: old inconsistent rows are handled defensively, but schema drift repair is still heuristic.
- Live-trading impact: historical trade review can be correct enough for operations but not yet contract-note-grade.
- Fix plan: add a migration command that rewrites old rows into one canonical schema with validation output.

## MEDIUM

### `backend/app/strategy/iron_condor_strategy.py`
- Function/Class: `calculate_target_metrics`, `is_entry_credit_viable`, `evaluate_entry_regime`
- Issue: charges-aware target logic is stronger now, but still parameter-driven rather than statistically calibrated from real trade distribution.
- Live-trading impact: improved trade quality, but profitability readiness is not yet proven across regimes.
- Fix plan: backtest current filters on real NIFTY sessions and calibrate thresholds from net expectancy.

### `backend/main.py`
- Function/Class: `/api/dashboard`, `/api/iron-condor/stats`, `/api/analytics`
- Issue: dashboard now exposes more operational truth, but some statuses still come from local cache rather than fresh broker reconciliation.
- Live-trading impact: operator view is improved, though not yet a complete independent control panel.
- Fix plan: add last successful reconciliation time, broker health status, and explicit orphan-position alert state.

### `frontend/dashboard.js`
- Function/Class: `updateICPosition`, `updateSystemStatus`, trade history rendering
- Issue: dashboard readability is better, but trade history still uses flat rows rather than expandable structured details.
- Live-trading impact: operator visibility is good, not yet excellent.
- Fix plan: convert IC history rows to summary + expandable leg details with warnings for forced exits and net-negative targets.

### `backend/app/core/math_engine.py`
- Function/Class: `full_analytics`
- Issue: analytics now use net P&L correctly from the main API path, but the helper still permits brokerage subtraction if called elsewhere with gross series.
- Live-trading impact: low direct trading risk, medium reporting risk if reused incorrectly.
- Fix plan: make analytics input mode explicit (`net_series` vs `gross_series`) at the function signature level.

## LOW

### `backend/app/api/dashboard_api.py`
- Function/Class: `build_dashboard_router`
- Issue: route drift was reduced, but duplicated API surface still exists.
- Live-trading impact: low if unused, medium if mounted later.
- Fix plan: consolidate onto one dashboard API path.

### `tests/`
- Function/Class: regression and safety suites
- Issue: targeted safety coverage is now better, but full live workflow simulation is still shallow.
- Live-trading impact: lower confidence than a production trading system should have.
- Fix plan: add suites for restart recovery, reconciliation orphan states, quote degradation flatten flow, and IC leg verification after emergency exit.

## Summary

The bot is materially safer and more internally consistent than before this audit pass. The remaining major gap is no longer missing basic logic; it is proving that the live safety paths hold under real broker/session/quote disruption conditions.
