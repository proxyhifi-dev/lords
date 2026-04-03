async function loadDashboard() {

    try {

        const res = await fetch("/api/dashboard")
        const data = await res.json()

        document.getElementById("bot-status").innerText = data.bot_status
        document.getElementById("trading-mode").innerText = data.trading_mode
        document.getElementById("nifty-spot").innerText = data.nifty_spot ?? "-"

        document.getElementById("orb-range").innerText =
            (data.orb_high ?? "-") + " / " + (data.orb_low ?? "-")

        document.getElementById("signal").innerText = data.signal ?? "NONE"

        document.getElementById("active-trade").innerText =
            JSON.stringify(data.active_trade ?? "No Active Trade", null, 2)

        document.getElementById("daily-pnl").innerText = data.daily_pnl ?? 0

        const history = document.getElementById("trade-history")
        history.innerHTML = ""

        if(data.trade_history){

            data.trade_history.forEach(trade => {

                const row = document.createElement("div")

                row.innerText =
                    trade.symbol + " | " +
                    trade.pnl + " | " +
                    trade.status

                history.appendChild(row)

            })

        }

    } catch (err) {

        console.error("Dashboard error:", err)

    }

}

document.getElementById("refresh-btn").onclick = loadDashboard

setInterval(loadDashboard, 3000)

loadDashboard()