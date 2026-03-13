import asyncio
import logging
from datetime import datetime
from typing import Any

from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge

from config import settings

logger = logging.getLogger(__name__)


class SamcoClient:
    @staticmethod
    def to_expiry_code(expiry: str) -> str:
        return datetime.strptime(expiry, '%Y-%m-%d').strftime('%d%b%Y').upper()

    @staticmethod
    def to_expiry_dash(expiry: str) -> str:
        return datetime.strptime(expiry, '%Y-%m-%d').strftime('%d-%m-%Y')

    def __init__(self) -> None:
        self.samco = StocknoteAPIPythonBridge()
        self.is_authenticated = False

    def authenticate(self) -> bool:
        """Automatically logs into Samco using credentials from config."""
        if self.is_authenticated:
            return True

        if not all([settings.samco_user_id, settings.samco_password, settings.samco_yob]):
            logger.error('samco credentials missing; set SAMCO_USER_ID, SAMCO_PASSWORD, and SAMCO_YOB')
            return False

        logger.info('attempting automatic login for user=%s', settings.samco_user_id)

        try:
            login_response = self.samco.login(
                body={
                    'userId': settings.samco_user_id,
                    'password': settings.samco_password,
                    'yob': settings.samco_yob,
                },
            )
            if login_response.get('status') == 'Success':
                session_token = login_response.get('sessionToken')
                if not session_token:
                    logger.error('samco login did not return session token')
                    return False
                self.samco.set_session_token(sessionToken=session_token)
                self.is_authenticated = True
                logger.info('samco login successful; session token injected')
                return True

            logger.error('samco login failed: %s', login_response.get('statusMessage'))
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error('exception during samco authentication: %s', exc)
            return False

    def _call_with_reauth(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        if not self.authenticate():
            return {}

        response = func(*args, **kwargs)
        if isinstance(response, dict) and response.get('status') == 'Success':
            return response

        message = str(response.get('statusMessage', '') if isinstance(response, dict) else '')
        if 'session' in message.lower() or 'token' in message.lower() or 'unauthorized' in message.lower():
            self.is_authenticated = False
            if self.authenticate():
                return func(*args, **kwargs)
        return response

    def get_nifty_spot(self) -> float | None:
        """Fetches the NIFTY 50 Spot Price to calculate the ATM strike for ORB."""
        logger.info('fetching NIFTY 50 index quote')
        response = self._call_with_reauth(self.samco.index_quote, 'NIFTY 50')
        if response and response.get('status') == 'Success':
            try:
                return float(response['indexDetails'][0]['spotPrice'])
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                logger.error('failed to parse NIFTY spot price: %s | response=%s', exc, response)
                return None

        logger.error('api error fetching NIFTY spot: %s', response)
        return None

    async def get_underlying_price(self, symbol: str) -> float:
        if symbol.upper() in {'NIFTY', 'NIFTY 50'}:
            spot = await asyncio.to_thread(self.get_nifty_spot)
            return float(spot or 0.0)

        response = await asyncio.to_thread(self._call_with_reauth, self.samco.index_quote, symbol)
        if response and response.get('status') == 'Success':
            try:
                return float(response['indexDetails'][0]['spotPrice'])
            except (KeyError, IndexError, TypeError, ValueError):
                return 0.0
        return 0.0

    def get_specific_option(
        self,
        symbol: str = 'NIFTY',
        expiry: str = '2026-03-26',
        strike: str | int = '22000',
        opt_type: str = 'CE',
    ) -> dict[str, Any]:
        """Fetches a specific option chain contract using official SDK parameters."""
        response = self._call_with_reauth(
            self.samco.get_option_chain,
            search_symbol_name=symbol,
            exchange=self.samco.EXCHANGE_NFO,
            expiry_date=expiry,
            strike_price=str(strike),
            option_type=opt_type,
        )
        return response if isinstance(response, dict) else {}

    async def get_option_chain(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        response = await asyncio.to_thread(
            self._call_with_reauth,
            self.samco.get_option_chain,
            search_symbol_name=symbol,
            exchange=self.samco.EXCHANGE_NFO,
            expiry_date=expiry,
        )
        if not isinstance(response, dict):
            return []

        data = response.get('optionChainDetails') or response.get('optionDetails') or response.get('data') or []
        return data if isinstance(data, list) else []

    async def get_profile(self) -> dict[str, Any]:
        response = await asyncio.to_thread(self._call_with_reauth, self.samco.user_details)
        return response if isinstance(response, dict) else {}

    async def get_funds(self) -> dict[str, Any]:
        response = await asyncio.to_thread(self._call_with_reauth, self.samco.get_limits)
        return response if isinstance(response, dict) else {}


samco_client = SamcoClient()
samco_client_instance = samco_client
