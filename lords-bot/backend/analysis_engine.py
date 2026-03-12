from __future__ import annotations

import numpy as np
import pandas as pd

from config import yaml_config
from models import AnalysisResult
from utils import utc_now_iso


class AnalysisEngine:
    def analyze(self, df: pd.DataFrame, symbol: str, expiry: str, underlying_price: float) -> AnalysisResult:
        total_put_oi = float(df['put_oi'].sum()) if not df.empty else 0.0
        total_call_oi = float(df['call_oi'].sum()) if not df.empty else 1.0
        pcr = total_put_oi / max(total_call_oi, 1.0)
        trend = 'BULLISH' if pcr > yaml_config.pcr_bullish else 'BEARISH' if pcr < yaml_config.pcr_bearish else 'SIDEWAYS'

        if df.empty:
            return AnalysisResult(
                symbol=symbol,
                expiry=expiry,
                underlying_price=underlying_price,
                pcr=round(pcr, 4),
                trend=trend,
                support=0,
                resistance=0,
                atm_strike=0,
                oi_buildup='NONE',
                volume_spikes=[],
                smart_money_bias='NEUTRAL',
                generated_at=utc_now_iso(),
            )

        support = float(df.loc[df['put_oi'].idxmax(), 'strike_price'])
        resistance = float(df.loc[df['call_oi'].idxmax(), 'strike_price'])
        atm_idx = (df['strike_price'] - underlying_price).abs().idxmin()
        atm_strike = float(df.loc[atm_idx, 'strike_price'])

        vol_threshold = np.percentile(df['volume'].values, 85)
        volume_spikes = df[df['volume'] >= vol_threshold]['strike_price'].astype(float).tolist()

        call_chg = float(df['call_change_oi'].sum())
        put_chg = float(df['put_change_oi'].sum())
        if put_chg > 0 and call_chg > 0:
            oi_buildup = 'LONG_BUILDUP'
        elif put_chg < 0 and call_chg < 0:
            oi_buildup = 'UNWINDING'
        else:
            oi_buildup = 'MIXED'

        smart_money_bias = 'NEUTRAL'
        if put_chg > call_chg * 1.1:
            smart_money_bias = 'PUT_BUYING'
        elif call_chg > put_chg * 1.1:
            smart_money_bias = 'CALL_BUYING'

        return AnalysisResult(
            symbol=symbol,
            expiry=expiry,
            underlying_price=underlying_price,
            pcr=round(pcr, 4),
            trend=trend,
            support=support,
            resistance=resistance,
            atm_strike=atm_strike,
            oi_buildup=oi_buildup,
            volume_spikes=volume_spikes,
            smart_money_bias=smart_money_bias,
            generated_at=utc_now_iso(),
        )


analysis_engine = AnalysisEngine()
