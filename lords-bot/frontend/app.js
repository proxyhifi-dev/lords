async function loadDashboard() {

    try {

        const res = await fetch("/api/dashboard")
        const data = await res.json()

        // Bot status
        document.getElementById("bot-status").innerText = data.bot_status

        // Trading mode
        document.getElementById("trading-mode").innerText = data.trading_mode

        // Nifty spot
        document.getElementById("nifty-spot").innerText =
            data.nifty_spot ?? "-"

        // ORB range
        document.getElementById("orb-range").innerText =
            (data.orb_high ?? "-") + " / " + (data.orb_low ?? "-")

        // Signal
        document.getElementById("signal").innerText =
            data.signal ?? "NONE"

        // Active trade
        document.getElementById("active-trade").innerText =
            JSON.stringify(data.active_trade ?? {}, null, 2)

        // PnL
        document.getElementById("daily-pnl").innerText =
            data.daily_pnl ?? 0

        // Trade history
        const history = document.getElementById("trade-history")
        history.innerHTML = ""

        data.trade_history.forEach(trade => {

            const row = document.createElement("div")

            row.innerText =
                trade.symbol + " | " +
                trade.pnl + " | " +
                trade.status

            history.appendChild(row)

        })

    } catch (err) {

        console.error("Dashboard error:", err)

    }

}

// refresh button
document.getElementById("refresh-btn").onclick = loadDashboard

// auto refresh every 5 seconds
setInterval(loadDashboard, 5000)

// load on startup
loadDashboard()