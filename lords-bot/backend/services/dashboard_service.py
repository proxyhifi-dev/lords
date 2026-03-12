from __future__ import annotations

from backend.core.cache import SnapshotStore
from backend.runtime_state import runtime_state


class DashboardService:
    def __init__(self, snapshot: SnapshotStore) -> None:
        self.snapshot = snapshot

    def build_dashboard(self, profile: dict, funds: dict) -> dict:
        payload = {
            'profile': profile,
            'funds': funds,
            'analysis': runtime_state.latest_analysis,
            'signals': runtime_state.latest_signal,
            'option_chain': runtime_state.latest_option_chain,
            'trading_mode': runtime_state.trading_mode,
        }
        if not payload['option_chain']:
            cached = self.snapshot.load()
            payload['option_chain'] = cached.get('option_chain', [])
            payload['analysis'] = payload['analysis'] or cached.get('analysis', {})
            payload['signals'] = payload['signals'] or cached.get('signals', {})
        self.snapshot.save({
            'option_chain': payload['option_chain'],
            'analysis': payload['analysis'],
            'signals': payload['signals'],
        })
        return payload
