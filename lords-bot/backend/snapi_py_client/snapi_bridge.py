class StocknoteAPIPythonBridge:
    EXCHANGE_NFO = 'NFO'

    def __init__(self) -> None:
        self._session_token = ''

    def set_session_token(self, sessionToken: str) -> None:  # noqa: N803
        self._session_token = sessionToken

    def login(self, body: dict):
        return {'status': 'Error', 'statusMessage': 'Stocknote SDK not installed in this environment'}

    def index_quote(self, index_name: str):
        return {'status': 'Error', 'statusMessage': f'No quote provider for {index_name}'}

    def get_option_chain(self, **kwargs):
        return {'status': 'Error', 'statusMessage': 'No option-chain provider available'}

    def user_details(self):
        return {'status': 'Error', 'statusMessage': 'No profile provider available'}

    def get_limits(self):
        return {'status': 'Error', 'statusMessage': 'No funds provider available'}
