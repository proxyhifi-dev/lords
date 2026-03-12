from __future__ import annotations

import pandas as pd

from models import AnalysisResult, SignalResult
from utils import utc_now_iso


class StrategyEngine:
    def generate_signal(self, analysis: AnalysisResult, df: pd.DataFrame) -> SignalResult:
        if df.empty:
            return SignalResult(signal='NO TRADE', confidence=0.1, reason='No market data', generated_at=utc_now_iso())

        atm_row = df.iloc[(df['strike_price'] - analysis.atm_strike).abs().argsort()[:1]]
        row = atm_row.iloc[0]
        bullish_score = 0.0
        bearish_score = 0.0

        if analysis.pcr > 1.2:
            bullish_score += 0.35
        elif analysis.pcr < 0.8:
            bearish_score += 0.35

        if row['put_change_oi'] > row['call_change_oi']:
            bullish_score += 0.2
        elif row['call_change_oi'] > row['put_change_oi']:
            bearish_score += 0.2

        distance_to_support = abs(analysis.atm_strike - analysis.support)
        distance_to_resistance = abs(analysis.resistance - analysis.atm_strike)
        if distance_to_support < distance_to_resistance:
            bullish_score += 0.15
        elif distance_to_resistance < distance_to_support:
            bearish_score += 0.15

        if analysis.smart_money_bias == 'CALL_BUYING':
            bullish_score += 0.2
        elif analysis.smart_money_bias == 'PUT_BUYING':
            bearish_score += 0.2

        if bullish_score >= bearish_score and bullish_score >= 0.5:
            return SignalResult(
                signal='BUY CALL',
                confidence=round(min(0.99, bullish_score), 2),
                reason='Bullish confluence across PCR, OI changes and support proximity',
                generated_at=utc_now_iso(),
            )
        if bearish_score > bullish_score and bearish_score >= 0.5:
            return SignalResult(
                signal='BUY PUT',
                confidence=round(min(0.99, bearish_score), 2),
                reason='Bearish confluence across PCR, OI changes and resistance proximity',
                generated_at=utc_now_iso(),
            )

        return SignalResult(
            signal='NO TRADE',
            confidence=round(max(bullish_score, bearish_score), 2),
            reason='No high-conviction directional edge',
            generated_at=utc_now_iso(),
        )


strategy_engine = StrategyEngine()
