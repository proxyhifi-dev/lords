const $ = (id) => document.getElementById(id);

function headers() {
  const key = window.localStorage.getItem('apiKey') || '';
  return { 'Content-Type': 'application/json', 'X-API-Key': key };
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, { headers: headers(), ...options });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function safeNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

async function refresh() {
  const data = await fetchJSON('/api/dashboard');
  $('botStatus').textContent = JSON.stringify({ status: data.bot_status, running: data.bot_controls.running, last_error: data.last_error }, null, 2);
  $('modeState').textContent = JSON.stringify({ mode: data.trading_mode }, null, 2);
  $('marketStatus').textContent = JSON.stringify(data.market_status, null, 2);
  $('orbRange').textContent = JSON.stringify({ high: safeNumber(data.orb_range?.high), low: safeNumber(data.orb_range?.low) }, null, 2);
  $('signalPanel').textContent = JSON.stringify(data.signal_panel || {}, null, 2);
  $('executionPanel').textContent = JSON.stringify(data.active_trade || {}, null, 2);
  $('tradeHistory').textContent = JSON.stringify(data.trade_history || [], null, 2);
  $('performance').textContent = JSON.stringify(data.pnl_metrics || {}, null, 2);
}

$('paperBtn').onclick = async () => {
  await fetchJSON('/api/trading-mode', { method: 'POST', body: JSON.stringify({ mode: 'PAPER' }) });
  refresh();
};

$('realBtn').onclick = async () => {
  const confirmed = window.confirm('Switch to REAL MODE? Real capital will be at risk.');
  if (!confirmed) return;
  await fetchJSON('/api/trading-mode', { method: 'POST', body: JSON.stringify({ mode: 'REAL', confirm_real: true }) });
  refresh();
};

$('refreshBtn').onclick = refresh;
$('flattenBtn').onclick = async () => {
  await fetchJSON('/api/trade/flatten', { method: 'POST' });
  refresh();
};

refresh();
setInterval(refresh, 10000);
