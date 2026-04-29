"""
Lords Bot — SAMCO Client
Async wrapper with:
  • Paper mode (no real orders in MODE=paper)
  • Circuit breaker
  • Exponential-backoff retry
  • 1s quote cache, 5s chain cache
  • Fill confirmation polling
  • Correct parse_spot (indexDetails.spotPrice)
  • Correct parse_ltp (quoteDetails nesting)
  • _map_exchange uses SDK constants
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from backend.app.core.circuit_breaker import CircuitBreaker
from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger

settings = get_settings()
logger   = get_logger("samco_client")

_paper_counter = 0
IST = ZoneInfo("Asia/Kolkata")

def _next_paper_id() -> str:
    global _paper_counter
    _paper_counter += 1
    return f"PAPER-{_paper_counter:05d}"


class SamcoClient:

    def __init__(self):
        self._session_live = False
        self._lock   = asyncio.Lock()
        self._breaker = CircuitBreaker(
            failure_threshold=settings.circuit_failure_threshold,
            cooldown_seconds=settings.circuit_cooldown_seconds,
        )
        self._quote_cache: dict = {}
        self._chain_cache: dict = {}
        self._QUOTE_TTL = 1
        self._CHAIN_TTL = 5
        self._samco = None
        self._auth_failed_until = 0.0
        self._last_auth_error = ""

    def _get_bridge(self):
        if self._samco is None:
            try:
                from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge
                self._samco = StocknoteAPIPythonBridge()
            except ImportError as exc:
                raise RuntimeError("snapi_py_client not installed") from exc
        return self._samco

    # ── AUTH ──────────────────────────────────────────
    async def login(self) -> dict:
        async with self._lock:
            now_ts = time.time()
            if now_ts < self._auth_failed_until:
                wait = int(self._auth_failed_until - now_ts)
                raise RuntimeError(f"SAMCO login cooldown active ({wait}s): {self._last_auth_error}")

            logger.info("SAMCO login user=%s", settings.samco_user_id)
            body: dict[str, Any] = {
                "userId":   settings.samco_user_id,
                "password": settings.samco_password,
                "yob":      settings.samco_yob,
            }
            if settings.samco_access_token:
                body["accessToken"] = settings.samco_access_token

            bridge = self._get_bridge()
            resp   = self._parse_response(await asyncio.to_thread(bridge.login, body=body))

            if resp.get("status") != "Success":
                msg = str(resp.get("statusMessage") or resp)
                self._last_auth_error = msg
                self._session_live = False
                self._auth_failed_until = time.time() + max(float(settings.reconnect_base_delay), 30.0)
                raise RuntimeError(f"SAMCO login failed: {resp}")

            token = resp.get("sessionToken")
            if not token:
                raise RuntimeError("Login OK but no sessionToken")

            await asyncio.to_thread(bridge.set_session_token, sessionToken=token)
            self._session_live = True
            self._auth_failed_until = 0.0
            self._last_auth_error = ""
            logger.info("SAMCO login successful")
            return resp

    async def ensure_session(self) -> None:
        if not self._session_live:
            await self.login()

    async def healthcheck(self) -> bool:
        try:
            q = await self.get_index_quote(settings.nifty_symbol)
            return bool(q)
        except Exception:
            return False

    # ── QUOTES ────────────────────────────────────────
    async def get_index_quote(self, index_name: str) -> dict:
        await self.ensure_session()
        return await self._call_sdk(
            lambda: self._get_bridge().index_quote(indexName=index_name),
            "index_quote",
        )

    async def get_quote(self, symbol_name: str, exchange: str = "NFO") -> dict:
        await self.ensure_session()
        key = f"{symbol_name}_{exchange}"
        cached = self._quote_cache.get(key)
        if cached and (time.time() - cached["ts"]) < self._QUOTE_TTL:
            return cached["data"]
        exch   = self._map_exchange(exchange)
        result = await self._call_sdk(
            lambda: self._get_bridge().get_quote(symbol_name=symbol_name, exchange=exch),
            "get_quote",
        )
        if not isinstance(result, dict): result = {}
        # Debug: log top-level keys once to help diagnose field name issues
        if result and logger.isEnabledFor(10):  # DEBUG level
            logger.debug("get_quote(%s) top-level keys: %s", symbol_name, list(result.keys()))
        self._quote_cache[key] = {"ts": time.time(), "data": result}
        return result

    # ── OPTION CHAIN ──────────────────────────────────
    async def get_option_chain(
        self,
        search_symbol_name: str,
        exchange: str,
        expiry_date: str,
        strike_price: str,
        option_type: str,
    ) -> dict:
        await self.ensure_session()
        key = f"{search_symbol_name}_{expiry_date}_{strike_price}_{option_type}"
        cached = self._chain_cache.get(key)
        if cached and (time.time() - cached["ts"]) < self._CHAIN_TTL:
            return cached["data"]
        exch   = self._map_exchange(exchange)
        result = await self._call_sdk(
            lambda: self._get_bridge().get_option_chain(
                search_symbol_name=search_symbol_name,
                exchange=exch,
                expiry_date=expiry_date,
                strike_price=strike_price,
                option_type=option_type,
            ),
            "get_option_chain",
        )
        if not isinstance(result, dict): result = {}
        self._chain_cache[key] = {"ts": time.time(), "data": result}
        return result

    # ── ORDERS ────────────────────────────────────────
    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        exchange: str = "NFO",
    ) -> dict:
        # PAPER MODE — simulate order, no real API call
        if not settings.is_live:
            oid = _next_paper_id()
            logger.info("[PAPER] %s %s qty=%s → %s", side, symbol, quantity, oid)
            return {"status": "Success", "orderNumber": oid}

        await self.ensure_session()
        bridge   = self._get_bridge()
        txn_type = bridge.TRANSACTION_TYPE_BUY if side == "BUY" else bridge.TRANSACTION_TYPE_SELL
        exch     = self._map_exchange(exchange)

        result = await self._call_sdk(
            lambda: bridge.place_order(body={
                "symbolName":      symbol,
                "exchange":        exch,
                "transactionType": txn_type,
                "orderType":       bridge.ORDER_TYPE_MARKET,
                "quantity":        str(quantity),
                "productType":     bridge.PRODUCT_MIS,
                "orderValidity":   bridge.VALIDITY_DAY,
            }),
            "place_order",
        )
        if result.get("status") != "Success":
            logger.error("Order rejected side=%s symbol=%s qty=%s resp=%s", side, symbol, quantity, result)
        else:
            logger.info("Order placed side=%s symbol=%s qty=%s id=%s",
                        side, symbol, quantity, result.get("orderNumber"))
        return result

    async def get_order_status(self, order_id: str) -> dict:
        await self.ensure_session()
        result = await self._call_sdk(
            lambda: self._get_bridge().get_order_status(orderNumber=order_id),
            "get_order_status",
        )
        return result if isinstance(result, dict) else {}

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel order by order number."""
        await self.ensure_session()
        result = await self._call_sdk(
            lambda: self._get_bridge().cancel_order(orderNumber=order_id),
            "cancel_order",
        )
        return result if isinstance(result, dict) else {}

    async def confirm_fill(self, order_id: str, max_attempts: int = 10, delay: float = 0.5) -> bool:
        """Poll until order fills. Paper orders are always filled."""
        if order_id.startswith("PAPER-"):
            return True
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await self.get_order_status(order_id)
                data = resp.get("orderDetails") or resp.get("data") or resp
                if isinstance(data, list): data = data[0] if data else {}
                status = (data.get("orderStatus") or data.get("status") or "").upper()
                logger.debug("Fill check #%d order=%s status=%s", attempt, order_id, status)
                if status in ("COMPLETE", "FILLED", "TRADED"): return True
                if status in ("REJECTED", "CANCELLED", "CANCELED"):
                    logger.error("Order %s %s", order_id, status)
                    return False
            except Exception as exc:
                logger.warning("confirm_fill attempt %d error: %s", attempt, exc)
            await asyncio.sleep(delay)
        logger.error("confirm_fill timeout order=%s", order_id)
        return False

    async def get_positions(self) -> list[dict]:
        """Get current positions. NOTE: This may not be available in all SAMCO SDK versions."""
        try:
            await self.ensure_session()
            # Try to get positions - this method may not exist in all SDK versions
            bridge = self._get_bridge()
            result = await self._call_sdk(
                lambda: bridge.get_positions_data(position_type=bridge.POSITION_TYPE_NET),
                "get_positions_data",
            )
            return result if isinstance(result, list) else []
        except Exception as exc:
            logger.warning(f"get_positions_data not available or failed: {exc}")
            # Return empty list - positions will be handled via trade book
            return []

    async def get_trade_book(self) -> list[dict]:
        """Get trade book for the day."""
        try:
            await self.ensure_session()
            result = await self._call_sdk(
                lambda: self._get_bridge().get_trade_book(),
                "get_trade_book",
            )
            return result if isinstance(result, list) else []
        except Exception as exc:
            logger.warning(f"get_trade_book failed: {exc}")
            return []

    async def get_orders(self) -> list[dict]:
        """Get current orders. NOTE: This may not be available in all SAMCO SDK versions."""
        try:
            await self.ensure_session()
            # Try to get orders - this method may not exist in all SDK versions
            result = await self._call_sdk(
                lambda: self._get_bridge().get_order_book(),
                "get_order_book",
            )
            return result if isinstance(result, list) else []
        except Exception as exc:
            logger.warning(f"get_order_book not available or failed: {exc}")
            # Return empty list - orders will be tracked internally
            return []

    # ── PARSE SPOT ────────────────────────────────────
    @staticmethod
    def parse_spot(quote: dict | None) -> float | None:
        if not quote: return None

        def _f(val):
            if val is None: return None
            try:
                f = float(str(val).replace(",", "").strip())
                return f if f > 0 else None
            except (TypeError, ValueError):
                return None

        # Primary: indexDetails[0].spotPrice
        details = quote.get("indexDetails")
        if isinstance(details, list) and details:
            for k in ("spotPrice", "lastTradedPrice", "lastTradePrice", "indexValue"):
                v = _f(details[0].get(k))
                if v: return v

        # Flat keys
        for k in ("spotPrice", "indexValue", "lastTradedPrice", "lastTradePrice", "ltp", "close"):
            v = _f(quote.get(k))
            if v: return v

        return None

    # ── PARSE LTP ─────────────────────────────────────
    @staticmethod
    def parse_ltp(quote: dict | None) -> float | None:
        if not quote: return None

        def _f(val):
            if val is None: return None
            try:
                f = float(str(val).replace(",", "").strip())
                return f if f > 0 else None
            except (TypeError, ValueError):
                return None

        def _extract(d: dict) -> float | None:
            for k in ("lastTradedPrice", "lastTradePrice", "ltp", "last_price", "close"):
                v = _f(d.get(k))
                if v: return v
            return None

        # Unwrap quoteDetails
        inner = quote.get("quoteDetails")
        if isinstance(inner, dict):
            v = _extract(inner)
            if v: return v
        elif isinstance(inner, list) and inner:
            v = _extract(inner[0])
            if v: return v

        # Unwrap data
        data = quote.get("data")
        if isinstance(data, dict):
            v = _extract(data)
            if v: return v
        elif isinstance(data, list) and data:
            v = _extract(data[0])
            if v: return v

        return _extract(quote)

    @staticmethod
    def parse_bid_ask(quote: dict | None) -> tuple[float | None, float | None]:
        """Extract best bid/ask from quote payload."""
        if not quote:
            return None, None

        def _f(val):
            if val is None:
                return None
            try:
                f = float(str(val).replace(",", "").strip())
                return f if f > 0 else None
            except (TypeError, ValueError):
                return None

        def _extract(d: dict) -> tuple[float | None, float | None]:
            bid = None
            ask = None
            for key in ("bestBidPrice", "bidPrice", "best_bid", "buyPrice"):
                bid = _f(d.get(key))
                if bid:
                    break
            for key in ("bestAskPrice", "askPrice", "best_ask", "sellPrice"):
                ask = _f(d.get(key))
                if ask:
                    break
            return bid, ask

        inner = quote.get("quoteDetails")
        if isinstance(inner, list) and inner:
            return _extract(inner[0])
        if isinstance(inner, dict):
            return _extract(inner)

        data = quote.get("data")
        if isinstance(data, list) and data:
            return _extract(data[0])
        if isinstance(data, dict):
            return _extract(data)

        return _extract(quote)

    async def get_actual_fill_price(self, order_id: str) -> float | None:
        """
        Fetch average fill price from tradebook/order status.
        Paper orders return None (caller uses LTP fallback).
        """
        if order_id.startswith("PAPER-"):
            return None

        try:
            trades = await self.get_trade_book()
            for t in trades:
                if str(t.get("orderNumber") or t.get("orderId") or "") == str(order_id):
                    for key in ("avgFillPrice", "averagePrice", "price", "fillPrice", "tradedPrice", "lastTradedPrice"):
                        val = t.get(key)
                        if val is not None:
                            try:
                                p = float(str(val).replace(",", "").strip())
                                if p > 0:
                                    return p
                            except (ValueError, TypeError):
                                continue
        except Exception as exc:
            logger.warning("get_actual_fill_price tradebook failed order=%s err=%s", order_id, exc)

        try:
            resp = await self.get_order_status(order_id)
            data = resp.get("orderDetails") or resp.get("data") or resp
            if isinstance(data, list):
                data = data[0] if data else {}
            for key in ("avgFillPrice", "averagePrice", "price", "filledPrice"):
                val = data.get(key)
                if val is not None:
                    try:
                        p = float(str(val).replace(",", "").strip())
                        if p > 0:
                            return p
                    except (ValueError, TypeError):
                        continue
        except Exception as exc:
            logger.warning("get_actual_fill_price status failed order=%s err=%s", order_id, exc)
        return None

    async def place_order_and_wait_fill(
        self,
        symbol: str,
        side: str,
        quantity: int,
        exchange: str = "NFO",
        max_fill_wait: int = 10,
    ) -> tuple[str | None, float | None]:
        """Place order and wait for fill confirmation."""
        resp = await self.place_order(symbol=symbol, side=side, quantity=quantity, exchange=exchange)
        order_id = resp.get("orderNumber") or resp.get("orderId") or resp.get("order_id")
        if not order_id:
            logger.error("place_order_and_wait_fill no order_id side=%s symbol=%s resp=%s", side, symbol, resp)
            return None, None

        filled = await self.confirm_fill(order_id, max_attempts=max_fill_wait)
        if not filled:
            return order_id, None
        fill_price = await self.get_actual_fill_price(order_id)
        return order_id, fill_price

    async def place_stop_loss_order(self, symbol: str, quantity: int, trigger_price: float, side: str = "SELL", exchange: str = "NFO") -> dict:
        """Place broker-level stop-loss order (fail-closed caller must verify acceptance)."""
        if not settings.is_live:
            oid = _next_paper_id()
            return {"status": "Success", "orderNumber": oid, "type": "PAPER_SL"}
        await self.ensure_session()
        bridge = self._get_bridge()
        txn_type = bridge.TRANSACTION_TYPE_BUY if side == "BUY" else bridge.TRANSACTION_TYPE_SELL
        exch = self._map_exchange(exchange)
        return await self._call_sdk(
            lambda: bridge.place_order(body={
                "symbolName": symbol,
                "exchange": exch,
                "transactionType": txn_type,
                "orderType": getattr(bridge, "ORDER_TYPE_SL_M", "SL-M"),
                "triggerPrice": str(trigger_price),
                "quantity": str(quantity),
                "productType": bridge.PRODUCT_MIS,
                "orderValidity": bridge.VALIDITY_DAY,
            }),
            "place_stop_loss_order",
        )

    async def cancel_all_open_orders(self) -> None:
        orders = await self.get_orders()
        for o in orders:
            oid = o.get("orderNumber") or o.get("orderId")
            status = str(o.get("orderStatus") or "").upper()
            if oid and status in {"OPEN", "PENDING", "TRIGGER_PENDING"}:
                try:
                    await self.cancel_order(str(oid))
                except Exception as exc:
                    logger.error("cancel order failed order_id=%s error=%s", oid, exc)

    async def close_all_positions_market(self) -> None:
        positions = await self.get_positions()
        for p in positions:
            sym = p.get("tradingSymbol") or p.get("symbolName")
            qty = 0
            for key in ("netQty", "netQuantity", "net_qty"):
                try:
                    qty = int(float(str(p.get(key, 0)).replace(",", "")))
                    break
                except Exception:
                    continue
            if not sym or qty == 0:
                continue
            side = "SELL" if qty > 0 else "BUY"
            await self.place_order(symbol=sym, side=side, quantity=abs(qty))

    # ── EXCHANGE MAPPER ───────────────────────────────
    def _map_exchange(self, exchange: str) -> str:
        bridge = self._get_bridge()
        mapping = {
            "NSE": getattr(bridge, "EXCHANGE_NSE", "NSE"),
            "NFO": getattr(bridge, "EXCHANGE_NFO", "NFO"),
            "BSE": getattr(bridge, "EXCHANGE_BSE", "BSE"),
        }
        return mapping.get(exchange.upper(), exchange.upper())

    # ── SDK WRAPPER ───────────────────────────────────
    async def _call_sdk(self, fn: Callable[[], Any], api_name: str) -> dict:
        if not self._breaker.allow_request():
            raise RuntimeError(f"Circuit breaker OPEN for {api_name}")

        attempts = settings.reconnect_max_attempts
        delay    = float(settings.reconnect_base_delay)

        for attempt in range(1, attempts + 1):
            try:
                result = await asyncio.to_thread(fn)
                resp   = self._parse_response(result)
                self._breaker.record_success()

                # Auto re-login on session expiry
                if isinstance(resp, dict) and resp.get("status") == "SessionExpired":
                    logger.warning("Session expired — re-logging in")
                    self._session_live = False
                    await self.login()
                    result = await asyncio.to_thread(fn)
                    resp   = self._parse_response(result)

                return resp

            except RuntimeError:
                raise
            except Exception as exc:
                self._breaker.record_failure()
                logger.error("SAMCO %s attempt=%d/%d error=%s", api_name, attempt, attempts, exc)
                if attempt < attempts:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60.0)

        return {}

    @staticmethod
    def _parse_response(result: Any) -> dict | list:
        if result is None: return {}
        if isinstance(result, (dict, list)): return result
        if isinstance(result, str):
            result = result.strip()
            if not result: return {}
            try: return json.loads(result)
            except json.JSONDecodeError: return {}
        return {}


# ── Weekly expiry helpers ─────────────────────────────
#
# NSE EXPIRY DAY CHANGE (SEBI Circular, effective Sep 2 2025):
#   Before Sep 2 2025 : NIFTY weekly expiry = THURSDAY
#   From   Sep 2 2025 : NIFTY weekly expiry = TUESDAY
#
# Bug before fix: get_weekly_expiry() always returned Thursday.
# On Apr 15 2026 (Wednesday) it returned Apr 16 (Thursday) →
# SAMCO returned "Option chain empty" because that contract doesn't exist.
# Correct answer: Apr 21 2026 (Tuesday).

_EXPIRY_CHANGE_DATE = date(2025, 9, 2)   # NSE changed to Tuesday on this date


def get_weekly_expiry() -> date:
    """
    Returns the next NIFTY weekly expiry date.
    Accounts for the NSE rule change on Sep 2 2025:
      - Before Sep 2 2025 → Thursday (weekday=3)
      - From   Sep 2 2025 → Tuesday  (weekday=1)
    """
    import datetime as _dt
    now_dt = _dt.datetime.now(IST)
    today = now_dt.date()
    now   = now_dt.time()

    # Choose target weekday based on NSE rule
    target = 1 if today >= _EXPIRY_CHANGE_DATE else 3   # Tue=1, Thu=3

    days = (target - today.weekday()) % 7

    # If today IS the expiry day but market has closed → next week
    if days == 0 and now >= _dt.time(15, 30):
        days = 7

    return today + timedelta(days=days)


def get_expiry_api() -> str:
    # SAMCO expiry format: DDMMMYYYY (e.g. 21APR2026)
    return get_weekly_expiry().strftime("%d%b%Y").upper()
