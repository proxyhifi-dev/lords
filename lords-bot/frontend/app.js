document.addEventListener("DOMContentLoaded", () => {

async function loadDashboard() {
    try {
        const res = await fetch("/api/dashboard");
        const data = await res.json();

        document.getElementById("bot-status").innerText = data.bot_status ?? "-";
        document.getElementById("trading-mode").innerText = data.trading_mode ?? "-";
        document.getElementById("nifty-spot").innerText = data.nifty_spot ?? "-";
        document.getElementById("signal").innerText = data.signal ?? "NONE";
        document.getElementById("daily-pnl").innerText = data.daily_pnl ?? 0;
        document.getElementById("trade-count").innerText = data.trade_count ?? 0;

        document.getElementById("orb-high").innerText = data.orb_high ?? "-";
        document.getElementById("orb-low").innerText = data.orb_low ?? "-";
        if (data.orb_high && data.orb_low) {
            document.getElementById("orb-points").innerText = (data.orb_high - data.orb_low).toFixed(1);
        } else {
            document.getElementById("orb-points").innerText = "-";
        }

        document.getElementById("active-trade").innerText =
            JSON.stringify(data.active_trade ?? "No Active Trade", null, 2);

        const historyTable = document.getElementById("trade-history");
        historyTable.innerHTML = "";
        if (data.trade_history && data.trade_history.length > 0) {
            data.trade_history.forEach(trade => {
                const row = document.createElement("tr");
                row.innerHTML = `
                    <td>${trade.entry_time ? new Date(trade.entry_time).toLocaleTimeString() : '-'}</td>
                    <td>${trade.symbol ?? '-'}</td>
                    <td>${trade.side ?? '-'}</td>
                    <td>${trade.entry_price ?? '-'}</td>
                    <td>${trade.exit_price ?? '-'}</td>
                    <td>${trade.pnl !== undefined ? trade.pnl.toFixed(2) : '-'}</td>
                    <td>${trade.status ?? '-'}</td>
                `;
                historyTable.appendChild(row);
            });
        } else {
            const row = document.createElement("tr");
            row.innerHTML = `<td colspan="7">No Trades Yet</td>`;
            historyTable.appendChild(row);
        }
    } catch (err) {
        console.error("Dashboard error:", err);
    }
}

document.getElementById("refresh-btn").onclick = loadDashboard;
setInterval(loadDashboard, 5000);
loadDashboard();

});