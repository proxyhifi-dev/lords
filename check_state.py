import json

try:
    with open('data/runtime_state.json', 'r') as f:
        state = json.load(f)

    print('=== CURRENT STATE ===')
    print(f'Active Trade: {state.get("active_trade") is not None}')

    if state.get('active_trade'):
        trade = state.get('active_trade')
        print(f'Symbol: {trade.get("symbol")}')
        print(f'Entry Price: ₹{trade.get("entry_price")}')
        print(f'Qty: {trade.get("qty")}')
        print(f'Status: {trade.get("status")}')

    print(f'Spot Price: ₹{state.get("spot_price")}')
    print(f'ORB High: ₹{state.get("orb_high")}')
    print(f'Trading Enabled: {state.get("trading_enabled")}')
    print(f'Bot Running: {state.get("bot_running")}')

    # Check if breakout condition is met
    spot = state.get("spot_price")
    orb_high = state.get("orb_high")
    if spot and orb_high:
        breakout = spot > orb_high
        print(f'Breakout Condition: {spot} > {orb_high} = {breakout}')

except Exception as e:
    print(f'Error reading state: {e}')