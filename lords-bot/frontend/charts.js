let heatmapChart;
let oiChangeChart;

function initCharts() {
  const heatCtx = document.getElementById('oiHeatmap').getContext('2d');
  const oiCtx = document.getElementById('oiChangeChart').getContext('2d');

  heatmapChart = new Chart(heatCtx, {
    type: 'bar',
    data: { labels: [], datasets: [{ label: 'OI Heatmap (Put - Call)', data: [], backgroundColor: [] }] },
    options: { responsive: true, plugins: { legend: { labels: { color: '#e2e8f0' } } }, scales: { x: { ticks: { color: '#e2e8f0' } }, y: { ticks: { color: '#e2e8f0' } } } }
  });

  oiChangeChart = new Chart(oiCtx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        { label: 'Call OI Change', data: [], borderColor: '#ef4444' },
        { label: 'Put OI Change', data: [], borderColor: '#22c55e' }
      ]
    },
    options: { responsive: true, plugins: { legend: { labels: { color: '#e2e8f0' } } }, scales: { x: { ticks: { color: '#e2e8f0' } }, y: { ticks: { color: '#e2e8f0' } } } }
  });
}

function updateCharts(rows) {
  if (!heatmapChart || !oiChangeChart) return;
  const labels = rows.map(r => r.strike_price);
  const heat = rows.map(r => r.put_oi - r.call_oi);
  const colors = heat.map(v => (v >= 0 ? 'rgba(34,197,94,0.6)' : 'rgba(239,68,68,0.6)'));

  heatmapChart.data.labels = labels;
  heatmapChart.data.datasets[0].data = heat;
  heatmapChart.data.datasets[0].backgroundColor = colors;
  heatmapChart.update();

  oiChangeChart.data.labels = labels;
  oiChangeChart.data.datasets[0].data = rows.map(r => r.call_change_oi);
  oiChangeChart.data.datasets[1].data = rows.map(r => r.put_change_oi);
  oiChangeChart.update();
}
