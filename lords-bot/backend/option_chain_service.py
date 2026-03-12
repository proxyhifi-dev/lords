from __future__ import annotations

import logging

import pandas as pd

from cache import cache, load_cached_snapshot, persist_cache_snapshot
from config import runtime_state
from samco_client import samco_client
from utils import utc_now_iso

logger = logging.getLogger(__name__)
REQUIRED_COLUMNS = {
    'strike_price',
    'call_oi',
    'put_oi',
    'call_change_oi',
    'put_change_oi',
    'call_ltp',
    'put_ltp',
    'volume',
}


class OptionChainService:
    async def fetch_latest(self) -> tuple[pd.DataFrame, float]:
        symbol = runtime_state.symbol
        expiry = runtime_state.expiry
        rows = await samco_client.get_option_chain(symbol=symbol, expiry=expiry)
        underlying = await samco_client.get_underlying_price(symbol)
        df = pd.DataFrame(rows)

        if df.empty:
            fallback = cache.get('option_chain') or load_cached_snapshot().get('option_chain', [])
            return pd.DataFrame(fallback), underlying

        missing = REQUIRED_COLUMNS - set(df.columns)
        if missing:
            logger.error('Option chain schema invalid; missing columns: %s', sorted(missing))
            fallback = cache.get('option_chain') or load_cached_snapshot().get('option_chain', [])
            return pd.DataFrame(fallback), underlying

        df = df[list(REQUIRED_COLUMNS)].copy()
        df = df.sort_values('strike_price').reset_index(drop=True)
        payload = {
            'symbol': symbol,
            'expiry': expiry,
            'underlying_price': underlying,
            'updated_at': utc_now_iso(),
            'option_chain': df.to_dict(orient='records'),
        }
        cache.set('option_chain', payload['option_chain'])
        cache.set('meta', {'symbol': symbol, 'expiry': expiry, 'updated_at': payload['updated_at']})
        cache.set('underlying_price', underlying)
        persist_cache_snapshot(payload)
        return df, underlying


option_chain_service = OptionChainService()
