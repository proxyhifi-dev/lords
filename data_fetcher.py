import asyncio
import pandas as pd
from datetime import datetime, time
from pathlib import Path

from backend.app.broker.samco_client import SamcoClient

# ===== CONFIG =====
SYMBOL_NAME = "NIFTY"
EXPIRY = "2026-04-28"

INTERVAL = 60  # seconds
SAVE_DIR = Path("data/options_chain")
SAVE_DIR.mkdir(parents=True, exist_ok=True)

MARKET_START = time(9, 15)
MARKET_END = time(15, 30)


async def fetch_option_chain(client):
    try:
        resp = await client.get_option_chain(
            search_symbol_name=SYMBOL_NAME,
            exchange="NFO",
            expiry_date=EXPIRY,
            strike_price="0",  # fetch full chain
            option_type="CE",  # API ignores this when full chain
        )

        rows = resp.get("optionChainDetails") or resp.get("data") or []
        return rows

    except Exception as e:
        print(f"❌ Error: {e}")
        return []


async def record_full_chain():
    client = SamcoClient()
    await client.login()

    print("✅ Full chain recorder started")

    all_data = []

    try:
        while True:
            now = datetime.now()
            current_time = now.time()

            # ⏳ wait for market
            if current_time < MARKET_START:
                await asyncio.sleep(30)
                continue

            # 🛑 stop after market close
            if current_time >= MARKET_END:
                print("🛑 Market closed")
                break

            # ❌ skip weekends
            if now.weekday() >= 5:
                print("Weekend. Exiting.")
                break

            chain = await fetch_option_chain(client)

            if chain:
                print(f"{now.strftime('%H:%M:%S')} → {len(chain)} contracts")

                for row in chain:
                    try:
                        all_data.append({
                            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "strike": int(float(row.get("strikePrice", 0))),
                            "type": row.get("optionType"),
                            "ltp": float(str(row.get("lastTradedPrice") or 0).replace(",", "")),
                            "bid": float(str(row.get("bidPrice") or 0).replace(",", "")),
                            "ask": float(str(row.get("askPrice") or 0).replace(",", "")),
                            "volume": int(row.get("volume") or 0),
                        })
                    except:
                        continue

            await asyncio.sleep(INTERVAL)

    except KeyboardInterrupt:
        print("\n🛑 Manual stop")

    # Save CSV
    df = pd.DataFrame(all_data)

    file_name = SAVE_DIR / f"{SYMBOL_NAME}_{EXPIRY}_{datetime.now().date()}.csv"
    df.to_csv(file_name, index=False)

    print(f"✅ Saved → {file_name}")


if __name__ == "__main__":
    asyncio.run(record_full_chain())