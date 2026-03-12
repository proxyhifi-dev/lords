from __future__ import annotations

import logging
import pandas as pd

from cache import cache, load_cached_snapshot, persist_cache_snapshot
from config import runtime_state
from samco_client import samco_client
from utils import utc_now_iso

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {
    "strike_price",
    "call_oi",
    "put_oi",
    "call_change_oi",
    "put_change_oi",
    "call_ltp",
    "put_ltp",
    "volume",
}


class OptionChainService:

    async def fetch_latest(self) -> tuple[pd.DataFrame, float]:

        symbol = runtime_state.symbol
        expiry = runtime_state.expiry

        try:
            rows = await samco_client.get_option_chain(symbol, expiry)
        except Exception as e:
            logger.warning("Option chain API failed, using cache: %s", str(e))
            rows = []

        try:
            underlying = await samco_client.get_underlying_price(symbol)
        except Exception:
            underlying = cache.get("underlying_price", 0)

        # Convert to DataFrame
        df = pd.DataFrame(rows)

        # Fallback if API failed
        if df.empty:
            fallback = cache.get("option_chain") or load_cached_snapshot().get(
                "option_chain", []
            )
            df = pd.DataFrame(fallback)

            if df.empty:
                return pd.DataFrame(), float(underlying)

        # Validate schema
        missing = REQUIRED_COLUMNS - set(df.columns)
        if missing:
            logger.error(
                "Option chain schema invalid; missing columns: %s", sorted(missing)
            )

            fallback = cache.get("option_chain") or load_cached_snapshot().get(
                "option_chain", []
            )

            df = pd.DataFrame(fallback)

            if df.empty:
                return pd.DataFrame(), float(underlying)

        # Ensure correct columns
        df = df[list(REQUIRED_COLUMNS)].copy()

        # Convert numeric columns
        numeric_cols = [
            "strike_price",
            "call_oi",
            "put_oi",
            "call_change_oi",
            "put_change_oi",
            "call_ltp",
            "put_ltp",
            "volume",
        ]

        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        df = df.sort_values("strike_price").reset_index(drop=True)

        payload = {
            "symbol": symbol,
            "expiry": expiry,
            "underlying_price": float(underlying),
            "updated_at": utc_now_iso(),
            "option_chain": df.to_dict(orient="records"),
        }

        cache.set("option_chain", payload["option_chain"])
        cache.set(
            "meta",
            {
                "symbol": symbol,
                "expiry": expiry,
                "updated_at": payload["updated_at"],
            },
        )
        cache.set("underlying_price", payload["underlying_price"])

        persist_cache_snapshot(payload)

        return df, payload["underlying_price"]


option_chain_service = OptionChainService()