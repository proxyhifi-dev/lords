# LORDS BOT Paper Validation Checklist

Use this checklist before considering any move toward live deployment.

## Startup
- [ ] Start app with `MODE=paper`
- [ ] Confirm no live orders are placed
- [ ] Confirm dashboard loads without API errors
- [ ] Confirm scheduler status shows `RUNNING` after startup
- [ ] Confirm reconciliation status is visible and non-error

## Market Data
- [ ] Confirm NIFTY spot updates
- [ ] Confirm IV updates when available
- [ ] Confirm quote age updates for active IC positions
- [ ] Confirm cached quote warning appears only when expected
- [ ] Confirm stale/model fallback shows alert state

## Iron Condor Entry
- [ ] Confirm one-IC-per-day lock works
- [ ] Confirm expiry-day entry is blocked by default
- [ ] Confirm high-probability regime filters reject bad IV conditions
- [ ] Confirm low-credit / poor reward-risk entries are rejected

## Active Trade Monitoring
- [ ] Confirm current premium updates
- [ ] Confirm gross, charges, and net values are visible
- [ ] Confirm target shows `TARGET POSSIBLE = YES` only when net positive after buffer
- [ ] Confirm model fallback marks live P&L as estimated
- [ ] Confirm quote degradation triggers manual-review / lockdown behavior

## Exit and Flatten
- [ ] Confirm target/stop/EOD logic closes trades correctly
- [ ] Confirm manual flatten closes active trade and updates history
- [ ] Confirm fail-safe flatten works when scheduler stall is simulated
- [ ] Confirm no duplicate active trade remains after flatten

## Restart Safety
- [ ] Restart app with no active trade and verify clean recovery
- [ ] Restart app with active paper IC and verify state reload behavior
- [ ] Confirm trade history remains readable after restart

## Reconciliation and Storage
- [ ] Confirm reconciliation mismatch disables new entries
- [ ] Confirm old trade rows still display usable exit/gross/net data
- [ ] Confirm each IC trade stores 4 legs with readable details
- [ ] Confirm net P&L matches stored gross minus charges

## Final Sanity
- [ ] Confirm no duplicate entries occur in one session
- [ ] Confirm dashboard emergency/risk status is visible
- [ ] Confirm tests pass in the project venv
- [ ] Confirm compile step passes
