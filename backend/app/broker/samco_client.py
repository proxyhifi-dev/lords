"""
Lords Bot — SAMCO Client
Safe version:
- Paper mode works without snapi_py_client SDK.
- Live mode / real data download requires official Samco Python SDK.
- Clear error if SDK is missing.
- Windows-safe timezone fallback.
- Circuit breaker + retry wrapper.
- Quote / option-chain cache.
- Market order placement.
- Fill confirmation polling.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = timezone(timedelta(hours=5, minutes=30))

from backend.app.core.circuit_breaker import CircuitBreaker
from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger

settings = get_settings()
logger = get_logger("samco_client")

_paper_counter = 0


def _extract_filled_qty(data: dict) -> int:
    if not isinstance(data, dict):
        return 0
    for key in (
        "filledShares",
        "tradedQty",
        "filledQty",
        "executedQty",
        "filled_quantity",
        "tradedQuantity",
    ):
        value = data.get(key)
        try:
            qty = int(float(str(value).replace(",", "").strip()))
        except (TypeError, ValueError):
            continue
        if qty > 0:
            return qty
    return 0


def _next_paper_id() -> str:
    global _paper_counter
    _paper_counter += 1
    return f"PAPER-{_paper_counter:05d}"


class SamcoSdkMissingError(RuntimeError):
    pass


class PaperBridge:
    """
    Minimal paper bridge.
    This prevents imports from crashing when snapi_py_client is absent.
    IMPORTANT:
    This does NOT provide real Samco data.
    Real data download still requires official Samco SDK.
    """

    EXCHANGE_NSE = "NSE"
    EXCHANGE_NFO = "NFO"
    EXCHANGE_BSE = "BSE"

    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    ORDER_TYPE_MARKET = "MARKET"
    ORDER_TYPE_LIMIT = "LIMIT"
    ORDER_TYPE_SL_M = "SL-M"

    PRODUCT_MIS = "MIS"
    PRODUCT_NRML = "NRML"
    VALIDITY_DAY = "DAY"
    POSITION_TYPE_NET = "NET"

    def login(self, body: dict | None = None) -> dict:
        return {"status": "Success", "sessionToken": "PAPER_SESSION"}

    def set_session_token(self, sessionToken: str | None = None) -> dict:
        return {"status": "Success"}

    def index_quote(self, indexName: str | None = None) -> dict:
        return {
            "status": "Success",
            "indexDetails": [
                {
                    "indexName": indexName or "NIFTY 50",
                    "spotPrice": "25000.00",
                }
            ],
        }

    def get_quote(self, symbol_name: str | None = None, exchange: str | None = None) -> dict:
        return {
            "status": "Success",
            "quoteDetails": {
                "symbolName": symbol_name or "PAPER",
                "lastTradedPrice": "100.00",
                "bestBidPrice": "99.50",
                "bestAskPrice": "100.50",
            },
        }

    def get_option_chain(self, **kwargs) -> dict:
        return {"status": "Success", "optionChainDetails": []}

    def place_order(self, body: dict | None = None) -> dict:
        return {"status": "Success", "orderNumber": _next_paper_id(), "body": body or {}}

    def get_order_status(self, orderNumber: str | None = None) -> dict:
        return {
            "status": "Success",
            "orderDetails": {
                "orderNumber": orderNumber,
                "orderStatus": "COMPLETE",
                "averagePrice": "100.00",
            },
        }

    def cancel_order(self, orderNumber: str | None = None) -> dict:
        return {"status": "Success", "orderNumber": orderNumber}

    def get_positions_data(self, position_type: str | None = None) -> list[dict]:
        return []

    def get_trade_book(self) -> list[dict]:
        return []

    def get_order_book(self) -> list[dict]:
        return []

    def get_intraday_candle_data(self, **kwargs) -> dict:
        return {
            "status": "Failed",
            "statusMessage": "PaperBridge has no real intraday candle data. Install Samco SDK.",
            "data": [],
        }

    def get_index_intraday_candle_data(self, **kwargs) -> dict:
        return {
            "status": "Failed",
            "statusMessage": "PaperBridge has no real index candle data. Install Samco SDK.",
            "data": [],
        }


class SamcoClient:
    def __init__(self):
        self._session_live = False
        self._lock = asyncio.Lock()
        self._breaker = CircuitBreaker(
            failure_threshold=settings.circuit_failure_threshold,
            cooldown_seconds=settings.circuit_cooldown_seconds,
        )
        self._quote_cache: dict[str, dict] = {}
        self._chain_cache: dict[str, dict] = {}
        self._QUOTE_TTL = 1
        self._CHAIN_TTL = 5
        self._samco = None
        self._auth_failed_until = 0.0
        self._last_auth_error = ""

    @staticmethod
    def _looks_like_auth_error(payload: Any) -> bool:
        if isinstance(payload, dict):
            status = str(payload.get("status") or "").strip().lower()
            message = str(
                payload.get("statusMessage")
                or payload.get("message")
                or payload.get("error")
                or ""
            ).strip().lower()
            combined = f"{status} {message}"
        else:
            combined = str(payload or "").strip().lower()

        if not combined:
            return False

        tokens = (
            "sessionexpired",
            "session expired",
            "invalid session",
            "invalid token",
            "access token",
            "mismatched access token",
            "token expired",
            "unauthorized",
            "authorisation failed",
            "authorization failed",
            "login required",
        )
        return any(token in combined for token in tokens)

    def _using_placeholder_credentials(self) -> bool:
        user = str(getattr(settings, "samco_user_id", "") or "")
        password = str(getattr(settings, "samco_password", "") or "")
        yob = str(getattr(settings, "samco_yob", "") or "")
        placeholders = {
            "",
            "YOUR_SAMCO_USER_ID",
            "YOUR_PASSWORD",
            "YOUR_SAMCO_PASSWORD",
            "YOUR_YOB",
            "CHANGE_ME",
            "None",
            "none",
        }
        return user in placeholders or password in placeholders or yob in placeholders

    def _get_bridge(self):
        """
        Load official Samco SDK bridge.
        Paper mode:
            If SDK is missing, use PaperBridge so paper bot does not crash.
        Live mode / real data download:
            SDK is mandatory. Raise a clear error if missing.
        """
        if self._samco is not None:
            return self._samco

        try:
            from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge

            self._samco = StocknoteAPIPythonBridge()
            return self._samco
        except ImportError as exc:
            if not getattr(settings, "is_live", False):
                logger.warning(
                    "snapi_py_client not installed. Using PaperBridge. "
                    "Paper mode can run, but real Samco data download will not work."
                )
                self._samco = PaperBridge()
                return self._samco

            raise SamcoSdkMissingError(
                "snapi_py_client is not installed. Real Samco live/data mode requires "
                "the official Samco Python SDK. Download/install the SDK from Samco, "
                "then ensure the folder snapi_py_client is importable from this venv."
            ) from exc

    async def login(self) -> dict:
        async with self._lock:
            now_ts = time.time()
            if now_ts < self._auth_failed_until:
                wait = int(self._auth_failed_until - now_ts)
                raise RuntimeError(
                    f"SAMCO login cooldown active ({wait}s): {self._last_auth_error}"
                )

            if self._using_placeholder_credentials() and getattr(settings, "is_live", False):
                raise RuntimeError(
                    "SAMCO credentials are placeholders. Update .env before live/data mode."
                )

            logger.info("SAMCO login user=%s", settings.samco_user_id)
            body: dict[str, Any] = {
                "userId": settings.samco_user_id,
                "password": settings.samco_password,
                "yob": settings.samco_yob,
            }
            if getattr(settings, "samco_access_token", ""):
                body["accessToken"] = settings.samco_access_token

            bridge = self._get_bridge()
            resp = self._parse_response(await asyncio.to_thread(bridge.login, body=body))

            if resp.get("status") != "Success":
                msg = str(resp.get("statusMessage") or resp)
                self._last_auth_error = msg
                self._session_live = False
                self._auth_failed_until = time.time() + max(
                    float(settings.reconnect_base_delay), 30.0
                )
                raise RuntimeError(f"SAMCO login failed: {resp}")

            token = resp.get("sessionToken")
            if not token:
                raise RuntimeError("SAMCO login returned Success but no sessionToken")

            await asyncio.to_thread(bridge.set_session_token, sessionToken=token)
            self._session_live = True
            self._quote_cache.clear()
            self._chain_cache.clear()
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
        except Exception as exc:
            logger.warning("SAMCO healthcheck failed: %s", exc)
            return False

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

        exch = self._map_exchange(exchange)
        result = await self._call_sdk(
            lambda: self._get_bridge().get_quote(
                symbol_name=symbol_name,
                exchange=exch,
            ),
            "get_quote",
        )
        if not isinstance(result, dict):
            result = {}
        self._quote_cache[key] = {"ts": time.time(), "data": result}
        return result

    async def get_index_intraday_candles(
        self,
        index_name: str,
        from_date: str,
        to_date: str,
    ) -> dict:
        await self.ensure_session()
        return await self._call_sdk(
            lambda: self._get_bridge().get_index_intraday_candle_data(
                index_name=index_name,
                from_date=from_date,
                to_date=to_date,
            ),
            "get_index_intraday_candle_data",
        )

    async def get_intraday_candles(
        self,
        symbol_name: str,
        exchange: str,
        from_date: str,
        to_date: str,
    ) -> dict:
        await self.ensure_session()
        exch = self._map_exchange(exchange)
        return await self._call_sdk(
            lambda: self._get_bridge().get_intraday_candle_data(
                symbol_name=symbol_name,
                exchange=exch,
                from_date=from_date,
                to_date=to_date,
            ),
            "get_intraday_candle_data",
        )

    async def get_option_chain(
        self,
        search_symbol_name: str,
        exchange: str,
        expiry_date: str,
        strike_price: str,
        option_type: str,
    ) -> dict:
        await self.ensure_session()
        key = f"{search_symbol_name}_{exchange}_{expiry_date}_{strike_price}_{option_type}"
        cached = self._chain_cache.get(key)
        if cached and (time.time() - cached["ts"]) < self._CHAIN_TTL:
            return cached["data"]

        exch = self._map_exchange(exchange)
        result = await self._call_sdk(
            lambda: self._get_bridge().get_option_chain(
                search_symbol_name=search_symbol_name,
                exchange=exch,
                expiry_date=expiry_date,
                strike_price=str(strike_price),
                option_type=option_type,
            ),
            "get_option_chain",
        )
        if not isinstance(result, dict):
            result = {}
        self._chain_cache[key] = {"ts": time.time(), "data": result}
        return result

    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        exchange: str = "NFO",
    ) -> dict:
        side = side.upper()
        if side not in {"BUY", "SELL"}:
            return {"status": "Failed", "statusMessage": f"Invalid side: {side}"}
        if quantity <= 0:
            return {"status": "Failed", "statusMessage": f"Invalid quantity: {quantity}"}

        if not getattr(settings, "is_live", False):
            oid = _next_paper_id()
            logger.info("[PAPER] %s %s qty=%s -> %s", side, symbol, quantity, oid)
            return {"status": "Success", "orderNumber": oid}

        await self.ensure_session()
        bridge = self._get_bridge()
        txn_type = (
            bridge.TRANSACTION_TYPE_BUY
            if side == "BUY"
            else bridge.TRANSACTION_TYPE_SELL
        )
        exch = self._map_exchange(exchange)
        body = {
            "symbolName": symbol,
            "exchange": exch,
            "transactionType": txn_type,
            "orderType": bridge.ORDER_TYPE_MARKET,
            "quantity": str(quantity),
            "productType": bridge.PRODUCT_MIS,
            "orderValidity": bridge.VALIDITY_DAY,
        }
        result = await self._call_sdk(
            lambda: bridge.place_order(body=body),
            "place_order",
        )
        if not isinstance(result, dict):
            result = {}

        if result.get("status") != "Success":
            logger.error(
                "Order rejected side=%s symbol=%s qty=%s resp=%s",
                side,
                symbol,
                quantity,
                result,
            )
        else:
            logger.info(
                "Order placed side=%s symbol=%s qty=%s id=%s",
                side,
                symbol,
                quantity,
                result.get("orderNumber"),
            )
        return result

    async def get_order_status(self, order_id: str) -> dict:
        await self.ensure_session()
        result = await self._call_sdk(
            lambda: self._get_bridge().get_order_status(orderNumber=order_id),
            "get_order_status",
        )
        return result if isinstance(result, dict) else {}

    async def cancel_order(self, order_id: str) -> dict:
        await self.ensure_session()
        result = await self._call_sdk(
            lambda: self._get_bridge().cancel_order(orderNumber=order_id),
            "cancel_order",
        )
        return result if isinstance(result, dict) else {}

    async def confirm_fill(
        self,
        order_id: str,
        requested_qty: int = 0,
        max_attempts: int = 10,
        delay: float = 0.5,
    ) -> tuple[str, int, float | None]:
        if str(order_id).startswith("PAPER-"):
            return "FILLED", requested_qty, None

        for attempt in range(1, max_attempts + 1):
            try:
                resp = await self.get_order_status(order_id)
                data = resp.get("orderDetails") or resp.get("data") or resp
                if isinstance(data, list):
                    data = data[0] if data else {}

                status = str(data.get("orderStatus") or data.get("status") or "").upper()
                logger.debug(
                    "Fill check attempt=%d order=%s status=%s",
                    attempt,
                    order_id,
                    status,
                )

                if status in {"COMPLETE", "FILLED", "TRADED", "EXECUTED"}:
                    avg = await self.get_actual_fill_price(order_id)
                    return "FILLED", requested_qty, avg

                if status in {"REJECTED", "CANCELLED", "CANCELED"}:
                    logger.error("Order %s terminal status=%s", order_id, status)
                    return status, 0, None

                if status in {"PARTIAL", "PARTIALLY_FILLED"}:
                    avg = await self.get_actual_fill_price(order_id)
                    partial_qty = _extract_filled_qty(data)
                    return "PARTIAL", partial_qty, avg
            except Exception as exc:
                logger.warning("confirm_fill attempt=%d error=%s", attempt, exc)

            await asyncio.sleep(delay)

        logger.error("confirm_fill timeout order=%s", order_id)
        return "UNKNOWN", 0, None

    async def place_order_with_fill_info(
        self,
        symbol: str,
        side: str,
        quantity: int,
        exchange: str = "NFO",
        max_fill_wait: int = 10,
    ) -> tuple[str | None, float | None, str, int]:
        resp = await self.place_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            exchange=exchange,
        )
        order_id = resp.get("orderNumber") or resp.get("orderId") or resp.get("order_id")
        if not order_id:
            logger.error(
                "place_order_with_fill_info no order_id side=%s symbol=%s resp=%s",
                side,
                symbol,
                resp,
            )
            return None, None, "NO_ORDER_ID", 0

        fill_state, filled_qty, fill_price = await self.confirm_fill(
            order_id,
            requested_qty=quantity,
            max_attempts=max_fill_wait,
        )
        return str(order_id), fill_price, fill_state, filled_qty

    async def place_order_and_wait_fill(
        self,
        symbol: str,
        side: str,
        quantity: int,
        exchange: str = "NFO",
        max_fill_wait: int = 10,
    ) -> tuple[str | None, float | None]:
        order_id, fill_price, fill_state, _ = await self.place_order_with_fill_info(
            symbol=symbol,
            side=side,
            quantity=quantity,
            exchange=exchange,
            max_fill_wait=max_fill_wait,
        )
        if not order_id:
            return None, None
        if fill_state != "FILLED":
            return order_id, None
        return order_id, fill_price

    async def place_stop_loss_order(
        self,
        symbol: str,
        quantity: int,
        trigger_price: float,
        side: str = "SELL",
        exchange: str = "NFO",
    ) -> dict:
        if not getattr(settings, "is_live", False):
            oid = _next_paper_id()
            return {"status": "Success", "orderNumber": oid, "type": "PAPER_SL"}

        await self.ensure_session()
        bridge = self._get_bridge()
        txn_type = (
            bridge.TRANSACTION_TYPE_BUY
            if side.upper() == "BUY"
            else bridge.TRANSACTION_TYPE_SELL
        )
        exch = self._map_exchange(exchange)
        body = {
            "symbolName": symbol,
            "exchange": exch,
            "transactionType": txn_type,
            "orderType": getattr(bridge, "ORDER_TYPE_SL_M", "SL-M"),
            "triggerPrice": str(trigger_price),
            "quantity": str(quantity),
            "productType": bridge.PRODUCT_MIS,
            "orderValidity": bridge.VALIDITY_DAY,
        }
        return await self._call_sdk(
            lambda: bridge.place_order(body=body),
            "place_stop_loss_order",
        )

    async def get_positions(self) -> list[dict]:
        try:
            await self.ensure_session()
            bridge = self._get_bridge()
            result = await self._call_sdk(
                lambda: bridge.get_positions_data(
                    position_type=getattr(bridge, "POSITION_TYPE_NET", "NET")
                ),
                "get_positions_data",
            )
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                data = (
                    result.get("positionDetails")
                    or result.get("data")
                    or result.get("positions")
                    or []
                )
                return data if isinstance(data, list) else []
            return []
        except Exception as exc:
            logger.warning("get_positions failed: %s", exc)
            return []

    async def get_trade_book(self) -> list[dict]:
        try:
            await self.ensure_session()
            result = await self._call_sdk(
                lambda: self._get_bridge().get_trade_book(),
                "get_trade_book",
            )
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                data = result.get("tradeBookDetails") or result.get("data") or []
                return data if isinstance(data, list) else []
            return []
        except Exception as exc:
            logger.warning("get_trade_book failed: %s", exc)
            return []

    async def get_orders(self) -> list[dict]:
        try:
            await self.ensure_session()
            result = await self._call_sdk(
                lambda: self._get_bridge().get_order_book(),
                "get_order_book",
            )
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                data = result.get("orderBookDetails") or result.get("data") or []
                return data if isinstance(data, list) else []
            return []
        except Exception as exc:
            logger.warning("get_order_book failed: %s", exc)
            return []

    async def cancel_all_open_orders(self) -> None:
        orders = await self.get_orders()
        for order in orders:
            oid = order.get("orderNumber") or order.get("orderId")
            status = str(order.get("orderStatus") or "").upper()
            if oid and status in {"OPEN", "PENDING", "TRIGGER_PENDING"}:
                try:
                    await self.cancel_order(str(oid))
                except Exception as exc:
                    logger.error("cancel order failed order_id=%s error=%s", oid, exc)

    async def close_all_positions_market(self) -> None:
        positions = await self.get_positions()
        for position in positions:
            sym = position.get("tradingSymbol") or position.get("symbolName")
            qty = 0
            for key in ("netQty", "netQuantity", "net_qty"):
                try:
                    qty = int(float(str(position.get(key, 0)).replace(",", "")))
                    break
                except Exception:
                    continue

            if not sym or qty == 0:
                continue

            side = "SELL" if qty > 0 else "BUY"
            await self.place_order(symbol=sym, side=side, quantity=abs(qty))

    @staticmethod
    def parse_spot(quote: dict | None) -> float | None:
        if not quote:
            return None

        def _f(val):
            if val is None:
                return None
            try:
                f = float(str(val).replace(",", "").strip())
                return f if f > 0 else None
            except (TypeError, ValueError):
                return None

        details = quote.get("indexDetails")
        if isinstance(details, list) and details:
            for key in ("spotPrice", "lastTradedPrice", "lastTradePrice", "indexValue"):
                value = _f(details[0].get(key))
                if value:
                    return value

        for key in ("spotPrice", "indexValue", "lastTradedPrice", "lastTradePrice", "ltp", "close"):
            value = _f(quote.get(key))
            if value:
                return value
        return None

    @staticmethod
    def parse_ltp(quote: dict | None) -> float | None:
        if not quote:
            return None

        def _f(val):
            if val is None:
                return None
            try:
                f = float(str(val).replace(",", "").strip())
                return f if f > 0 else None
            except (TypeError, ValueError):
                return None

        def _extract(data: dict) -> float | None:
            for key in ("lastTradedPrice", "lastTradePrice", "ltp", "last_price", "close"):
                value = _f(data.get(key))
                if value:
                    return value
            return None

        inner = quote.get("quoteDetails")
        if isinstance(inner, dict):
            value = _extract(inner)
            if value:
                return value
        if isinstance(inner, list) and inner:
            value = _extract(inner[0])
            if value:
                return value

        data = quote.get("data")
        if isinstance(data, dict):
            value = _extract(data)
            if value:
                return value
        if isinstance(data, list) and data:
            value = _extract(data[0])
            if value:
                return value

        return _extract(quote)

    @staticmethod
    def parse_bid_ask(quote: dict | None) -> tuple[float | None, float | None]:
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

        def _from_level_container(container: Any) -> tuple[float | None, float | None]:
            if isinstance(container, dict):
                bid = None
                ask = None

                for key in (
                    "bestBidPrice",
                    "bidPrice",
                    "best_bid",
                    "buyPrice",
                    "bid",
                    "bp",
                    "bPrice",
                ):
                    bid = _f(container.get(key))
                    if bid:
                        break

                for key in (
                    "bestAskPrice",
                    "askPrice",
                    "best_ask",
                    "sellPrice",
                    "ask",
                    "sp",
                    "aPrice",
                ):
                    ask = _f(container.get(key))
                    if ask:
                        break

                if not bid:
                    for key in ("bestBids", "bids", "bidBook", "buyBook"):
                        levels = container.get(key)
                        if isinstance(levels, list) and levels:
                            level0 = levels[0] if isinstance(levels[0], dict) else {}
                            for price_key in ("price", "bidPrice", "bestBidPrice", "bp"):
                                bid = _f(level0.get(price_key))
                                if bid:
                                    break
                        if bid:
                            break

                if not ask:
                    for key in ("bestAsks", "asks", "askBook", "sellBook"):
                        levels = container.get(key)
                        if isinstance(levels, list) and levels:
                            level0 = levels[0] if isinstance(levels[0], dict) else {}
                            for price_key in ("price", "askPrice", "bestAskPrice", "sp"):
                                ask = _f(level0.get(price_key))
                                if ask:
                                    break
                        if ask:
                            break

                return bid, ask

            if isinstance(container, list) and container:
                first = container[0]
                if isinstance(first, dict):
                    return _from_level_container(first)

            return None, None

        for key in ("quoteDetails", "data", "response", "result"):
            if key in quote:
                bid, ask = _from_level_container(quote.get(key))
                if bid or ask:
                    return bid, ask

        bid, ask = _from_level_container(quote)
        return bid, ask

    async def get_actual_fill_price(self, order_id: str) -> float | None:
        if str(order_id).startswith("PAPER-"):
            return None

        try:
            trades = await self.get_trade_book()
            for trade in trades:
                if str(trade.get("orderNumber") or trade.get("orderId") or "") == str(order_id):
                    for key in (
                        "avgFillPrice",
                        "averagePrice",
                        "price",
                        "fillPrice",
                        "tradedPrice",
                        "lastTradedPrice",
                    ):
                        val = trade.get(key)
                        if val is None:
                            continue
                        try:
                            price = float(str(val).replace(",", "").strip())
                            if price > 0:
                                return price
                        except (ValueError, TypeError):
                            continue
        except Exception as exc:
            logger.warning(
                "get_actual_fill_price tradebook failed order=%s err=%s",
                order_id,
                exc,
            )

        try:
            resp = await self.get_order_status(order_id)
            data = resp.get("orderDetails") or resp.get("data") or resp
            if isinstance(data, list):
                data = data[0] if data else {}
            for key in ("avgFillPrice", "averagePrice", "price", "filledPrice"):
                val = data.get(key)
                if val is None:
                    continue
                try:
                    price = float(str(val).replace(",", "").strip())
                    if price > 0:
                        return price
                except (ValueError, TypeError):
                    continue
        except Exception as exc:
            logger.warning(
                "get_actual_fill_price status failed order=%s err=%s",
                order_id,
                exc,
            )

        return None

    def _map_exchange(self, exchange: str) -> str:
        bridge = self._get_bridge()
        mapping = {
            "NSE": getattr(bridge, "EXCHANGE_NSE", "NSE"),
            "NFO": getattr(bridge, "EXCHANGE_NFO", "NFO"),
            "BSE": getattr(bridge, "EXCHANGE_BSE", "BSE"),
        }
        return mapping.get(str(exchange).upper(), str(exchange).upper())

    async def _call_sdk(self, fn: Callable[[], Any], api_name: str) -> dict | list:
        if not self._breaker.allow_request():
            raise RuntimeError(f"Circuit breaker OPEN for {api_name}")

        attempts = int(settings.reconnect_max_attempts)
        delay = float(settings.reconnect_base_delay)

        for attempt in range(1, attempts + 1):
            try:
                result = await asyncio.to_thread(fn)
                resp = self._parse_response(result)
                self._breaker.record_success()

                if self._looks_like_auth_error(resp):
                    logger.warning("SAMCO %s auth/session issue detected. Re-logging in.", api_name)
                    self._session_live = False
                    await self.login()
                    result = await asyncio.to_thread(fn)
                    resp = self._parse_response(result)

                return resp
            except RuntimeError as exc:
                self._breaker.record_failure()
                if self._looks_like_auth_error(exc) and attempt < attempts:
                    self._session_live = False
                    logger.warning(
                        "SAMCO %s runtime auth/session exception detected. Re-logging in before retry.",
                        api_name,
                    )
                    await self.login()
                    continue
                raise
            except Exception as exc:
                self._breaker.record_failure()
                logger.error(
                    "SAMCO %s attempt=%d/%d error=%s",
                    api_name,
                    attempt,
                    attempts,
                    exc,
                )
                if self._looks_like_auth_error(exc):
                    self._session_live = False
                    if attempt < attempts:
                        logger.warning(
                            "SAMCO %s auth/session exception detected. Re-logging in before retry.",
                            api_name,
                        )
                        await self.login()
                        continue
                if attempt < attempts:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60.0)

        return {}

    @staticmethod
    def _parse_response(result: Any) -> dict | list:
        if result is None:
            return {}
        if isinstance(result, (dict, list)):
            return result
        if isinstance(result, str):
            result = result.strip()
            if not result:
                return {}
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return {}
        return {}


_EXPIRY_CHANGE_DATE = date(2025, 9, 2)


def get_weekly_expiry(base_date: date | None = None) -> date:
    """
    NIFTY weekly expiry helper.
    Before 2025-09-02:
        Thursday
    From 2025-09-02:
        Tuesday
    """
    today = base_date or datetime.now(IST).date()
    now = datetime.now(IST).time()
    target_weekday = 1 if today >= _EXPIRY_CHANGE_DATE else 3
    days = (target_weekday - today.weekday()) % 7

    if base_date is None and days == 0 and now >= datetime.strptime("15:30", "%H:%M").time():
        days = 7

    return today + timedelta(days=days)


def get_expiry_api(base_date: date | None = None) -> str:
    return get_weekly_expiry(base_date).strftime("%Y-%m-%d")
