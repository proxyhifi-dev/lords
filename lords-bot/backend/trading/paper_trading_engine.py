from __future__ import annotations

from runtime_state import runtime_state


class PaperTradingEngine:
    def place_order(self, trade: dict) -> dict:
        entry = float(trade.get('entry_price', trade.get('ltp', 0)) or 0)
        qty = int(trade.get('qty', 50))
        position = {
            'symbol': trade.get('symbol'),
            'strike': trade.get('strike'),
            'type': trade.get('type'),
            'qty': qty,
            'entry': entry,
            'ltp': entry,
            'stop_loss': round(entry * 0.85, 2),
            'target': round(entry * 1.30, 2),
            'pnl': 0.0,
            'status': 'OPEN',
        }
        runtime_state.positions.append(position)
        return position

    def mark_to_market(self, chain: list[dict]) -> None:
        if not runtime_state.positions:
            runtime_state.day_pnl = 0.0
            return

        strike_map = {float(r.get('strike_price', 0) or 0): r for r in chain}
        total_pnl = 0.0
        for position in runtime_state.positions:
            if position.get('status') != 'OPEN':
                total_pnl += float(position.get('pnl', 0) or 0)
                continue
            row = strike_map.get(float(position.get('strike', 0) or 0), {})
            ltp_key = 'call_ltp' if position.get('type') == 'CE' else 'put_ltp'
            ltp = float(row.get(ltp_key, position.get('ltp', 0)) or 0)
            position['ltp'] = ltp
            position['pnl'] = round((ltp - float(position['entry'])) * int(position['qty']), 2)

            if ltp <= float(position['stop_loss']) or ltp >= float(position['target']):
                position['status'] = 'CLOSED'
            total_pnl += float(position['pnl'])

        runtime_state.day_pnl = round(total_pnl, 2)

    def close_all(self) -> int:
        closed = 0
        for position in runtime_state.positions:
            if position.get('status') == 'OPEN':
                position['status'] = 'CLOSED'
                closed += 1
        return closed


paper_trading_engine = PaperTradingEngine()
