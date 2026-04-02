from __future__ import annotations

from datetime import UTC, datetime, timedelta


class OptionSelector:
    @staticmethod
    def atm_strike(spot: float) -> int:
        return int(round(spot / 50.0) * 50)

    @staticmethod
    def next_weekly_expiry(now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        days_to_thu = (3 - now.weekday()) % 7
        expiry = now + timedelta(days=days_to_thu)
        return expiry.strftime("%d%b").upper()

    def select_option_symbol(self, spot: float, signal: str) -> str:
        strike = self.atm_strike(spot)
        option_type = "CE" if signal == "BUY_CE" else "PE"
        expiry = self.next_weekly_expiry()
        return f"NIFTY{expiry}{strike}{option_type}"
