# QA Test Plan

## Unit Tests

### 1) FyersClient retry logic
- **Precondition:** API returns 503 once then 200.
- **Steps:** call `FyersClient.request()` and observe retries.
- **Expected:** request succeeds within bounded retries.
- **Log verification:** `lords_bot.retry` emits retry warning with attempt count.

### 2) Circuit breaker activation/reset
- **Precondition:** repeated 503 responses exceed threshold.
- **Steps:** invoke request repeatedly; then call `reset_circuit_breaker()`.
- **Expected:** breaker opens (trading paused), then reset re-enables calls.
- **Log verification:** `lords_bot.circuit` logs open/reset events.

### 3) Token refresh on 401
- **Precondition:** first response is 401, refresh token is available.
- **Steps:** request endpoint and verify refresh method invocation.
- **Expected:** token refresh called once, second request succeeds.
- **Log verification:** `lords_bot.api` logs refresh success.

### 4) ORB builder logic with simulated ticks
- **Precondition:** ticks captured for 09:15-09:30.
- **Steps:** feed high/low/last ticks then call `check_breakout()`.
- **Expected:** breakout direction reflects last price crossing ORB range.
- **Log verification:** strategy logger records ORB lock.

### 5) Risk engine loss limits
- **Precondition:** daily realized PnL below allowed limit.
- **Steps:** call `can_trade_now()`.
- **Expected:** returns blocked with `max_daily_loss_hit`.
- **Log verification:** risk logger emits shutdown activation when breached.

### 6) Order placement and cancellation
- **Precondition:** valid order payload.
- **Steps:** call `place_order()`, `cancel_order()`, `modify_order()`.
- **Expected:** API endpoints invoked with correct payload.
- **Log verification:** trade logger records each order lifecycle action.

## Integration Tests

### 7) Simulate 503 from data API
- **Precondition:** quotes endpoint intermittently returns 503.
- **Steps:** run scan with mocked broker.
- **Expected:** retries happen; no process crash.
- **Log verification:** retry logger entries + bounded backoff timestamps.

### 8) Simulate 401 and confirm auto refresh
- **Precondition:** expired access token, valid refresh token.
- **Steps:** trigger request against protected endpoint.
- **Expected:** one refresh call and resumed successful request.
- **Log verification:** auth + api log entries for refresh path.

### 9) Simulate websocket disconnect/reconnect
- **Precondition:** websocket closes unexpectedly.
- **Steps:** run websocket listener; force disconnection.
- **Expected:** reconnect with exponential delay; ticks resume dispatching.
- **Log verification:** websocket warnings followed by reconnect info message.

### 10) UI scan/monitor safe values
- **Precondition:** strategy or broker throws errors.
- **Steps:** call `/scan` and `/monitor`.
- **Expected:** structured JSON response with defaults; no 500 crash.
- **Log verification:** ui logger captures exception and safe fallback response.

## Load/Stress Tests

### 11) 100 concurrent scan calls
- **Precondition:** app running in test mode.
- **Steps:** fire 100 parallel `/scan` requests.
- **Expected:** all responses are 200 with `status` field; no worker crash.
- **Log verification:** no uncaught exceptions; latency remains stable.

### 12) Circuit breaker under sustained failure
- **Precondition:** broker returns continuous 503 for 2+ minutes.
- **Steps:** bombard quote/order endpoints.
- **Expected:** circuit opens and blocks further calls during cool-down.
- **Log verification:** single open event per cycle, no endless retries.

### 13) UI responsiveness during broker outage
- **Precondition:** data/trade APIs unavailable.
- **Steps:** repeatedly call `/monitor` while failures continue.
- **Expected:** endpoint remains responsive with degraded data payload.
- **Log verification:** warnings/errors present, but no fatal shutdown.
