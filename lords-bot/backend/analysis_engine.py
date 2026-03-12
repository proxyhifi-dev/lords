from __future__ import annotations

import numpy as np
import pandas as pd

from models import AnalysisResult
from utils import utc_now_iso


class AnalysisEngine:
    def analyze(self, df: pd.DataFrame, symbol: str, expiry: str) -> AnalysisResult:
        if df.empty:
            return AnalysisResult(
                symbol=symbol,
                expiry=expiry,
                pcr=1.0,
                trend='SIDEWAYS',
                support=0,
                resistance=0,
                atm_strike=0,
                volume_spikes=[],
                smart_money_bias='NEUTRAL',
                generated_at=utc_now_iso(),
            )

        total_put_oi = float(df['put_oi'].sum())
        total_call_oi = float(df['call_oi'].sum()) or 1.0
        pcr = total_put_oi / total_call_oi
        trend = 'BULLISH' if pcr > 1.2 else 'BEARISH' if pcr < 0.8 else 'SIDEWAYS'

        support_idx = df['put_oi'].idxmax()
        resistance_idx = df['call_oi'].idxmax()
        support = float(df.loc[support_idx, 'strike_price'])
        resistance = float(df.loc[resistance_idx, 'strike_price'])

        atm_idx = (df['call_ltp'] - df['put_ltp']).abs().idxmin()
        atm_strike = float(df.loc[atm_idx, 'strike_price'])

        volume_threshold = np.percentile(df['volume'].values, 85)
        volume_spikes = df[df['volume'] >= volume_threshold]['strike_price'].astype(float).tolist()

        positive_put_oi = float(df[df['put_change_oi'] > 0]['put_change_oi'].sum())
        positive_call_oi = float(df[df['call_change_oi'] > 0]['call_change_oi'].sum())
        if positive_put_oi > positive_call_oi * 1.2:
            smart_money_bias = 'PUT_BUYING'
        elif positive_call_oi > positive_put_oi * 1.2:
            smart_money_bias = 'CALL_BUYING'
        else:
            smart_money_bias = 'NEUTRAL'

        return AnalysisResult(
            symbol=symbol,
            expiry=expiry,
            pcr=round(pcr, 4),
            trend=trend,
            support=support,
            resistance=resistance,
            atm_strike=atm_strike,
            volume_spikes=volume_spikes,
            smart_money_bias=smart_money_bias,
            generated_at=utc_now_iso(),
        )


analysis_engine = AnalysisEngine()
