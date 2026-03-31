let chart;

function updateChart(rows) {
  const labels = rows.map((r) => r.strike_price ?? r.strike ?? "-");

  const callOi = rows.map((r) => Number(r.call_oi || 0));
  const putOi = rows.map((r) => Number(r.put_oi || 0));

  const data = {
    labels,
    datasets: [
      {
        label: "Call OI",
        data: callOi,
        borderColor: "#ef4444",
        backgroundColor: "rgba(239,68,68,0.2)",
        tension: 0.3,
      },
      {
        label: "Put OI",
        data: putOi,
        borderColor: "#22c55e",
        backgroundColor: "rgba(34,197,94,0.2)",
        tension: 0.3,
      },
    ],
  };

  const config = {
    type: "line",
    data,
    options: {
      responsive: true,
      plugins: {
        legend: { position: "top" },
      },
      scales: {
        x: { title: { display: true, text: "Strike Price" } },
        y: { title: { display: true, text: "Open Interest" } },
      },
    },
  };

  if (!chart) {
    chart = new Chart(document.getElementById("oiChart"), config);
  } else {
    chart.data = data;
    chart.update();
  }
}