"""
Example usage of WebsocketService with callback style, T1/production dynamic URL, and correct access token format.
"""
import asyncio
from lords_bot.app.websocket_service import WebsocketService
from lords_bot.app.fyers_client import FyersClient
from lords_bot.app.auth import AuthService

# --- Callbacks ---
def on_message(msg):
    print("Received:", msg)

def on_error(msg):
    print("Error:", msg)

def on_close(msg):
    print("Connection closed:", msg)

def on_open():
    print("WebSocket Connected")

async def main():
    # Setup auth and client
    auth = AuthService()
    await auth.auto_login()  # Ensure token is loaded
    client = FyersClient(auth)

    # Setup websocket service with callbacks
    ws = WebsocketService(
        client,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open,
    )

    # Example symbol(s)
    symbols = ["NSE:NIFTY50-INDEX"]

    async def on_tick(tick):
        print("Tick:", tick)

    await ws.start(symbols, on_tick)
    await asyncio.sleep(30)  # Run for 30 seconds
    await ws.stop()

if __name__ == "__main__":
    asyncio.run(main())
