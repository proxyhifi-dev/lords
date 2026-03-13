from __future__ import annotations

from core.cache import SnapshotStore
from runtime_state import runtime_state


class DashboardService:
    def __init__(self, snapshot: SnapshotStore) -> None:
        self.snapshot = snapshot

    def build_dashboard(self, profile: dict, funds: dict) -> dict:
        payload = {
            'mode': runtime_state.trading_mode,
            'profile': profile,
            'funds': funds,
            'pnl': runtime_state.day_pnl,
            'positions': runtime_state.positions,
            'orders': runtime_state.orders,
            'analysis': runtime_state.latest_analysis,
            'signals': runtime_state.latest_signal,
            'best_trades': runtime_state.latest_best_trades,
            'pending_trade': runtime_state.pending_trade,
            'option_chain': runtime_state.latest_option_chain,
            'errors': runtime_state.scheduler_errors,
        }
        if not payload['option_chain']:
            cached = self.snapshot.load()
            for key in ['option_chain', 'analysis', 'signals', 'best_trades']:
                if not payload.get(key):
                    payload[key] = cached.get(key, payload.get(key))
        self.snapshot.save({
            'option_chain': payload['option_chain'],
            'analysis': payload['analysis'],
            'signals': payload['signals'],
            'best_trades': payload['best_trades'],
        })
        return payload
