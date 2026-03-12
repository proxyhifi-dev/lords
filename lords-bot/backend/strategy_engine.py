from __future__ import annotations

from models import AnalysisResult, SignalResult
from utils import utc_now_iso


class StrategyEngine:
    def generate_signal(self, analysis: AnalysisResult, previous_pcr: float | None = None) -> SignalResult:
        pcr_rising = previous_pcr is not None and analysis.pcr > previous_pcr
        pcr_falling = previous_pcr is not None and analysis.pcr < previous_pcr
        near_support = abs(analysis.underlying_price - analysis.support) <= 50
        near_resistance = abs(analysis.underlying_price - analysis.resistance) <= 50

        if near_support and pcr_rising:
            return SignalResult(
                signal='BUY CALL',
                confidence=0.78,
                reason='Price near support with rising PCR',
                generated_at=utc_now_iso(),
            )
        if near_resistance and pcr_falling:
            return SignalResult(
                signal='BUY PUT',
                confidence=0.78,
                reason='Price near resistance with falling PCR',
                generated_at=utc_now_iso(),
            )

        if analysis.trend == 'BULLISH' and analysis.smart_money_bias == 'PUT_BUYING':
            return SignalResult(signal='BUY CALL', confidence=0.62, reason='Bullish trend continuation', generated_at=utc_now_iso())
        if analysis.trend == 'BEARISH' and analysis.smart_money_bias == 'CALL_BUYING':
            return SignalResult(signal='BUY PUT', confidence=0.62, reason='Bearish trend continuation', generated_at=utc_now_iso())

        return SignalResult(signal='NO TRADE', confidence=0.25, reason='No high-conviction setup', generated_at=utc_now_iso())


strategy_engine = StrategyEngine()
