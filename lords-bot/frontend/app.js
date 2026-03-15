const $ = (id) => document.getElementById(id);

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

async function refresh() {
  const data = await fetchJSON('/dashboard');
  $('botStatus').textContent = JSON.stringify({ status: data.bot_status, running: data.bot_controls.running }, null, 2);
  $('modeState').textContent = JSON.stringify({ mode: data.trading_mode }, null, 2);
  $('marketStatus').textContent = JSON.stringify(data.market_status, null, 2);
  $('orbRange').textContent = JSON.stringify(data.orb_range, null, 2);
  $('signalPanel').textContent = JSON.stringify(data.signal_panel, null, 2);
  $('executionPanel').textContent = JSON.stringify(data.trade_execution_panel, null, 2);
  $('tradeHistory').textContent = JSON.stringify(data.trade_history, null, 2);
  $('performance').textContent = JSON.stringify(data.performance, null, 2);
}

$('paperBtn').onclick = async () => { await fetchJSON('/trading-mode', { method: 'POST', body: JSON.stringify({ mode: 'PAPER' }) }); refresh(); };
$('realBtn').onclick = async () => {
  const confirmed = window.confirm('Switch to REAL MODE? Real capital will be at risk.');
  if (!confirmed) return;
  await fetchJSON('/trading-mode', { method: 'POST', body: JSON.stringify({ mode: 'REAL', confirm_real: true }) });
  refresh();
};
$('refreshBtn').onclick = refresh;
$('flattenBtn').onclick = async () => { await fetchJSON('/trade/flatten', { method: 'POST' }); refresh(); };

refresh();
setInterval(refresh, 10000);
