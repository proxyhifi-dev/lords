from __future__ import annotations

from typing import Any


class OptionScanner:
    def scan(self, chain: list[dict[str, Any]], signal: dict, symbol: str) -> list[dict[str, Any]]:
        side = signal.get('signal', 'NO TRADE')
        if side == 'NO TRADE' or not chain:
            return []

        option_type = 'CE' if side == 'BUY CALL' else 'PE'
        scored: list[dict[str, Any]] = []
        for row in chain:
            oi = float(row.get('call_oi' if option_type == 'CE' else 'put_oi', 0) or 0)
            ch_oi = float(row.get('call_change_oi' if option_type == 'CE' else 'put_change_oi', 0) or 0)
            ltp = float(row.get('call_ltp' if option_type == 'CE' else 'put_ltp', 0) or 0)
            volume = float(row.get('volume', 0) or 0)
            score = (oi * 0.4) + (max(ch_oi, 0) * 0.3) + (volume * 0.2) + (max(ltp, 0) * 0.1)
            scored.append({
                'symbol': symbol,
                'strike': float(row.get('strike_price', 0) or 0),
                'type': option_type,
                'ltp': ltp,
                'score': score,
            })

        top = sorted(scored, key=lambda x: x['score'], reverse=True)[:3]
        if not top:
            return []
        max_score = max(item['score'] for item in top) or 1
        for item in top:
            item['confidence'] = round((item['score'] / max_score) * 100, 2)
            item['display'] = f"{item['symbol']} {int(item['strike'])} {item['type']}"
        return top


option_scanner = OptionScanner()
