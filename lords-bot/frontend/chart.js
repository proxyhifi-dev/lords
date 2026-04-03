let chart = null;

function updateChart(rows) {

    if (!rows || rows.length === 0) {
        console.warn("No option chain data");
        return;
    }

    const canvas = document.getElementById("oiChart");

    if (!canvas) {
        console.warn("oiChart canvas not found");
        return;
    }

    // Normalize strike values
    const labels = rows.map(r =>
        r.strike_price ??
        r.strike ??
        r.strikePrice ??
        "-"
    );

    const callOi = rows.map(r =>
        Number(r.call_oi ?? r.callOI ?? 0)
    );

    const putOi = rows.map(r =>
        Number(r.put_oi ?? r.putOI ?? 0)
    );

    const data = {
        labels: labels,
        datasets: [

            {
                label: "Call OI",
                data: callOi,
                borderColor: "#ef4444",
                backgroundColor: "rgba(239,68,68,0.2)",
                tension: 0.3,
                fill: true
            },

            {
                label: "Put OI",
                data: putOi,
                borderColor: "#22c55e",
                backgroundColor: "rgba(34,197,94,0.2)",
                tension: 0.3,
                fill: true
            }

        ]
    };

    const config = {

        type: "line",

        data: data,

        options: {

            responsive: true,

            plugins: {

                legend: {
                    position: "top"
                }

            },

            scales: {

                x: {
                    title: {
                        display: true,
                        text: "Strike Price"
                    }
                },

                y: {
                    title: {
                        display: true,
                        text: "Open Interest"
                    }
                }

            }

        }

    };

    if (!chart) {

        chart = new Chart(canvas, config);

    } else {

        chart.data = data;
        chart.update();

    }

}