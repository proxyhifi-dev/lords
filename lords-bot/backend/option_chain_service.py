from __future__ import annotations

import pandas as pd

from cache import cache, load_cached_snapshot, persist_cache_snapshot
from config import runtime_state
from samco_client import samco_client
from utils import utc_now_iso


class OptionChainService:
    async def fetch_latest(self) -> pd.DataFrame:
        symbol = runtime_state.symbol
        expiry = runtime_state.expiry
        rows = await samco_client.get_option_chain(symbol=symbol, expiry=expiry)
        df = pd.DataFrame(rows)
        if df.empty:
            fallback = cache.get('option_chain') or load_cached_snapshot().get('option_chain', [])
            return pd.DataFrame(fallback)

        df = df.sort_values('strike_price').reset_index(drop=True)
        payload = {
            'symbol': symbol,
            'expiry': expiry,
            'updated_at': utc_now_iso(),
            'option_chain': df.to_dict(orient='records'),
        }
        cache.set('option_chain', payload['option_chain'])
        cache.set('meta', {'symbol': symbol, 'expiry': expiry, 'updated_at': payload['updated_at']})
        persist_cache_snapshot(payload)
        return df


option_chain_service = OptionChainService()
