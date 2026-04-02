from backend.config import settings


class OptionSelector:
    @staticmethod
    def atm_strike(spot: float) -> int:
        return int(round(spot / 50.0) * 50)

    def select_option_symbol(self, spot: float, signal: str) -> str:
        strike = self.atm_strike(spot)
        opt_type = "CE" if signal == "BUY_CE" else "PE"
        return f"NIFTY{settings.option_expiry}{strike}{opt_type}"
