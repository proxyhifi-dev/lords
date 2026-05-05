from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class OrderExecutionSequence:
    """
    Paper-safe / broker-compatible Iron Condor execution helper.

    Design:
    - In paper mode: simulate success, no real broker orders.
    - In live mode: this helper requires resolved broker symbols for each leg.
    - If symbols are not supplied, it fails closed instead of guessing.
    """

    def __init__(self, broker_client, settings, logger):
        self.broker = broker_client
        self.settings = settings
        self.logger = logger
        self.max_retries = 3
        self.retry_delay = 1.0

    def _is_live(self) -> bool:
        return str(getattr(self.settings, "mode", "paper")).strip().lower() == "live"

    def _paper_order_id(self, prefix: str, symbol: str) -> str:
        ts = int(datetime.now(IST).timestamp())
        return f"PAPER-{prefix}-{symbol}-{ts}"

    async def enter_iron_condor_sequence(
        self,
        strikes: dict,
        premiums: dict,
        symbols: dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        """
        Execute Iron Condor with hedge-first sequence.

        Expected symbols in live mode:
            {
                "long_call": "...",
                "long_put": "...",
                "short_call": "...",
                "short_put": "...",
            }
        """

        self.logger.info("=" * 80)
        self.logger.info("IRON CONDOR ENTRY SEQUENCE STARTING")
        self.logger.info(
            "Strikes: SC=%s LC=%s SP=%s LP=%s",
            strikes.get("short_call"),
            strikes.get("long_call"),
            strikes.get("short_put"),
            strikes.get("long_put"),
        )
        self.logger.info("=" * 80)

        if not self._is_live():
            self.logger.info("PAPER MODE: simulating hedge-first Iron Condor sequence")
            return {
                "success": True,
                "order_ids": {
                    "long_call": self._paper_order_id("BUY", f"LC-{strikes['long_call']}"),
                    "long_put": self._paper_order_id("BUY", f"LP-{strikes['long_put']}"),
                    "short_call": self._paper_order_id("SELL", f"SC-{strikes['short_call']}"),
                    "short_put": self._paper_order_id("SELL", f"SP-{strikes['short_put']}"),
                },
                "margin_used": float(getattr(self.settings, "ic_margin_required", 0)),
                "filled_legs": [
                    ("LONG_CALL", f"LC-{strikes['long_call']}"),
                    ("LONG_PUT", f"LP-{strikes['long_put']}"),
                    ("SHORT_CALL", f"SC-{strikes['short_call']}"),
                    ("SHORT_PUT", f"SP-{strikes['short_put']}"),
                ],
                "error": None,
            }

        required_keys = {"long_call", "long_put", "short_call", "short_put"}
        if not symbols or not required_keys.issubset(symbols.keys()):
            error = (
                "Live IC entry requires resolved option symbols for all 4 legs. "
                "Missing symbols prevents safe order placement."
            )
            self.logger.error(error)
            return {"success": False, "order_ids": {}, "filled_legs": [], "error": error}

        order_ids: dict[str, str] = {}
        filled_legs: list[tuple[str, str]] = []

        try:
            self.logger.info("PHASE 1: Buying protective hedges first")

            lc = await self._place_market_order_with_retry(
                symbol=symbols["long_call"],
                side="BUY",
                quantity=self.settings.order_qty,
                leg_name="LONG_CALL",
            )
            if not lc["success"]:
                return {"success": False, "order_ids": {}, "filled_legs": [], "error": "Long Call fill failed"}
            order_ids["long_call"] = lc["order_id"]
            filled_legs.append(("LONG_CALL", lc["order_id"]))

            lp = await self._place_market_order_with_retry(
                symbol=symbols["long_put"],
                side="BUY",
                quantity=self.settings.order_qty,
                leg_name="LONG_PUT",
            )
            if not lp["success"]:
                await self._offset_filled_legs(
                    [{"symbol": symbols["long_call"], "side": "BUY", "qty": self.settings.order_qty}]
                )
                return {"success": False, "order_ids": {}, "filled_legs": [], "error": "Long Put fill failed"}
            order_ids["long_put"] = lp["order_id"]
            filled_legs.append(("LONG_PUT", lp["order_id"]))

            self.logger.info("PHASE 2: Selling short strikes after hedges confirmed")

            sc = await self._place_market_order_with_retry(
                symbol=symbols["short_call"],
                side="SELL",
                quantity=self.settings.order_qty,
                leg_name="SHORT_CALL",
            )
            if not sc["success"]:
                await self._offset_filled_legs(
                    [
                        {"symbol": symbols["long_call"], "side": "BUY", "qty": self.settings.order_qty},
                        {"symbol": symbols["long_put"], "side": "BUY", "qty": self.settings.order_qty},
                    ]
                )
                return {"success": False, "order_ids": {}, "filled_legs": [], "error": "Short Call fill failed"}
            order_ids["short_call"] = sc["order_id"]
            filled_legs.append(("SHORT_CALL", sc["order_id"]))

            sp = await self._place_market_order_with_retry(
                symbol=symbols["short_put"],
                side="SELL",
                quantity=self.settings.order_qty,
                leg_name="SHORT_PUT",
            )
            if not sp["success"]:
                await self._offset_filled_legs(
                    [
                        {"symbol": symbols["long_call"], "side": "BUY", "qty": self.settings.order_qty},
                        {"symbol": symbols["long_put"], "side": "BUY", "qty": self.settings.order_qty},
                        {"symbol": symbols["short_call"], "side": "SELL", "qty": self.settings.order_qty},
                    ]
                )
                return {"success": False, "order_ids": {}, "filled_legs": [], "error": "Short Put fill failed"}
            order_ids["short_put"] = sp["order_id"]
            filled_legs.append(("SHORT_PUT", sp["order_id"]))

            self.logger.info("IRON CONDOR ENTRY COMPLETE - ALL 4 LEGS FILLED")
            return {
                "success": True,
                "order_ids": order_ids,
                "margin_used": float(getattr(self.settings, "ic_margin_required", 0)),
                "filled_legs": filled_legs,
                "error": None,
            }

        except Exception as exc:
            self.logger.error("Critical error in entry sequence: %s", exc, exc_info=True)
            return {"success": False, "order_ids": order_ids, "filled_legs": filled_legs, "error": str(exc)}

    async def _place_market_order_with_retry(
        self,
        symbol: str,
        side: str,
        quantity: int,
        leg_name: str,
    ) -> Dict[str, Any]:
        for attempt in range(1, self.max_retries + 1):
            try:
                self.logger.info(
                    "Attempt %d/%d: %s %s qty=%d",
                    attempt,
                    self.max_retries,
                    side,
                    symbol,
                    quantity,
                )
                resp = await self.broker.place_order(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    exchange="NFO",
                )
                order_id = resp.get("orderNumber") or resp.get("orderId") or resp.get("order_id")

                if not order_id:
                    raise RuntimeError(f"{leg_name} order placement returned no order id: {resp}")

                fill_state, _, fill_price = await self.broker.confirm_fill(
                    order_id=str(order_id),
                    requested_qty=quantity,
                    max_attempts=10,
                    delay=0.5,
                )

                if fill_state == "FILLED":
                    self.logger.info(
                        "%s filled successfully order=%s fill_price=%s",
                        leg_name,
                        order_id,
                        fill_price,
                    )
                    return {
                        "success": True,
                        "order_id": str(order_id),
                        "filled_price": fill_price,
                    }

                self.logger.warning(
                    "%s not fully filled attempt=%d state=%s order=%s",
                    leg_name,
                    attempt,
                    fill_state,
                    order_id,
                )

            except Exception as exc:
                self.logger.warning(
                    "%s attempt %d/%d failed: %s",
                    leg_name,
                    attempt,
                    self.max_retries,
                    exc,
                )

            if attempt < self.max_retries:
                await asyncio.sleep(self.retry_delay * (2 ** (attempt - 1)))

        self.logger.error("%s failed after %d attempts", leg_name, self.max_retries)
        return {"success": False}

    async def _offset_filled_legs(self, legs: List[Dict[str, Any]]) -> None:
        """
        Offset already-filled legs with opposite side market orders.
        This is safer than trying to cancel filled orders.
        """
        for leg in reversed(legs):
            try:
                exit_side = "SELL" if leg["side"].upper() == "BUY" else "BUY"
                self.logger.warning(
                    "Offsetting filled leg symbol=%s original_side=%s exit_side=%s qty=%d",
                    leg["symbol"],
                    leg["side"],
                    exit_side,
                    leg["qty"],
                )
                await self.broker.place_order(
                    symbol=leg["symbol"],
                    side=exit_side,
                    quantity=leg["qty"],
                    exchange="NFO",
                )
            except Exception as exc:
                self.logger.error("Offset failed for %s: %s", leg, exc)


class ExpiryDaySafetyProtocol:
    """
    Simple expiry safety gate.

    For now, uses configured same-day IC_EXIT_TIME as the main intraday control.
    This avoids overcomplicated monthly-expiry assumptions for a daily paper bot.
    """

    def __init__(self, settings, logger):
        self.settings = settings
        self.logger = logger

    def get_safe_exit_deadline(self, entry_date: datetime) -> datetime:
        exit_text = str(getattr(self.settings, "ic_exit_time", "15:00"))
        try:
            hour, minute = map(int, exit_text.split(":"))
        except Exception:
            hour, minute = 15, 0

        deadline = entry_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if deadline < entry_date:
            deadline = deadline + timedelta(days=1)

        return deadline

    def should_force_exit(self, current_time: datetime, entry_time: datetime) -> Tuple[bool, str]:
        safe_deadline = self.get_safe_exit_deadline(entry_time)

        if current_time >= safe_deadline:
            reason = f"EOD/expiry safety exit deadline reached: {safe_deadline.isoformat()}"
            self.logger.warning(reason)
            return True, reason

        return False, ""


class WebSocketResilience:
    def __init__(self, websocket_url: str, logger):
        self.websocket_url = websocket_url
        self.logger = logger
        self.ws = None
        self.is_connected = False
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 60.0
        self.heartbeat_interval = 30

    async def connect(self):
        try:
            import websockets
        except ImportError:
            self.logger.error("websockets library not installed. Install with: pip install websockets")
            return

        while not self.is_connected:
            try:
                self.logger.info("Connecting to WebSocket: %s", self.websocket_url)
                self.ws = await websockets.connect(self.websocket_url)
                self.is_connected = True
                self.reconnect_delay = 1.0
                self.logger.info("WebSocket connected successfully")
                asyncio.create_task(self._heartbeat_monitor())
            except Exception as exc:
                self.logger.error("WebSocket connection failed: %s", exc)
                self.is_connected = False
                wait_time = min(self.reconnect_delay, self.max_reconnect_delay)
                self.logger.info("Retrying in %.1fs...", wait_time)
                await asyncio.sleep(wait_time)
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)

    async def _heartbeat_monitor(self):
        while self.is_connected:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                await self.ws.ping()
                self.logger.debug("WebSocket heartbeat sent")
            except Exception as exc:
                self.logger.error("Heartbeat failed: %s", exc)
                self.is_connected = False
                await self.connect()

    async def receive(self) -> dict:
        while True:
            try:
                if not self.is_connected:
                    await self.connect()

                data = await asyncio.wait_for(self.ws.recv(), timeout=self.heartbeat_interval)

                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="ignore")

                if isinstance(data, str):
                    try:
                        parsed = json.loads(data)
                        if isinstance(parsed, dict):
                            return parsed
                        return {"data": parsed}
                    except json.JSONDecodeError:
                        return {"raw": data}

                return {"data": data}

            except asyncio.TimeoutError:
                self.logger.warning("WebSocket timeout - no data received")
                self.is_connected = False
                await self.connect()
            except Exception as exc:
                self.logger.error("WebSocket error: %s", exc)
                self.is_connected = False
                await self.connect()


class MarginUtilizationMonitor:
    def __init__(self, total_capital: float, safety_buffer: float, logger):
        self.total_capital = float(total_capital)
        self.safety_buffer = float(safety_buffer)
        self.available_capital = max(self.total_capital - self.safety_buffer, 1.0)
        self.logger = logger
        self.warning_threshold = 0.85
        self.critical_threshold = 0.95

    def check_margin(self, margin_used: float) -> Dict[str, Any]:
        margin_used = float(margin_used or 0.0)
        usage_pct = margin_used / self.available_capital

        status = "OK"
        if usage_pct >= self.critical_threshold:
            status = "CRITICAL"
            self.logger.critical(
                "CRITICAL MARGIN: %.1f%% used (₹%s/₹%s)",
                usage_pct * 100,
                f"{margin_used:,.0f}",
                f"{self.available_capital:,.0f}",
            )
        elif usage_pct >= self.warning_threshold:
            status = "WARNING"
            self.logger.warning(
                "HIGH MARGIN: %.1f%% used (₹%s/₹%s)",
                usage_pct * 100,
                f"{margin_used:,.0f}",
                f"{self.available_capital:,.0f}",
            )
        else:
            self.logger.info(
                "Margin OK: %.1f%% used (₹%s/₹%s)",
                usage_pct * 100,
                f"{margin_used:,.0f}",
                f"{self.available_capital:,.0f}",
            )

        return {
            "status": status,
            "usage_pct": usage_pct,
            "margin_used": margin_used,
            "available": self.available_capital - margin_used,
        }