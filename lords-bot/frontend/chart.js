let chart;

function updateChart(rows) {
  const labels = rows.map((r) => r.strike_price ?? r.strike ?? '-');
  const callOi = rows.map((r) => Number(r.call_oi || 0));
  const putOi = rows.map((r) => Number(r.put_oi || 0));

  const data = {
    labels,
    datasets: [
      { label: 'Call OI', data: callOi, borderColor: '#f55' },
      { label: 'Put OI', data: putOi, borderColor: '#5f5' },
    ],
  };

  if (!chart) {
    chart = new Chart(document.getElementById('oiChart'), { type: 'line', data });
  } else {
    chart.data = data;
    chart.update();
  }
}
