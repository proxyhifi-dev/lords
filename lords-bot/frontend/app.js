let isRefreshing = false;
let chart;

async function api(method, url, body) {
let chart;

async function call(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const message = data?.detail || data?.reason || 'Request failed';
    throw new Error(message);
  }
  return data;
}

function safe(v, fallback = '-') {
  return v === null || v === undefined || v === '' ? fallback : v;
}

function renderRows(id, rows, emptyCols) {
  const html = rows.length ? rows.join('') : `<tr><td colspan="${emptyCols}">No data</td></tr>`;
  document.getElementById(id).innerHTML = html;
}

  return res.json();
}

function row(v) { return v ?? '-'; }

function renderTable(id, html) {
  document.getElementById(id).innerHTML = html;
}

function updateChart(chain) {
  const labels = chain.map((r) => r.strike_price);
  const callOI = chain.map((r) => r.call_oi);
  const putOI = chain.map((r) => r.put_oi);

  if (chart) chart.destroy();
  chart = new Chart(document.getElementById('oiChart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Call OI', data: callOI, borderColor: '#f97316', backgroundColor: 'transparent', tension: 0.2 },
        { label: 'Put OI', data: putOI, borderColor: '#38bdf8', backgroundColor: 'transparent', tension: 0.2 },
      ],
    },
    options: { responsive: true, maintainAspectRatio: false },
    data: { labels, datasets: [
      { label: 'Call OI', data: callOI, borderColor: '#ff6b6b' },
      { label: 'Put OI', data: putOI, borderColor: '#4dabf7' },
    ]},
  });
}

async function refresh() {
  if (isRefreshing) return;
  isRefreshing = true;
  try {
    const data = await api('GET', '/dashboard');

  document.getElementById('mode').textContent = safe(data.mode);
  document.getElementById('trend').textContent = safe(data.analysis?.trend);
  document.getElementById('pcr').textContent = safe(data.analysis?.pcr);
  document.getElementById('signal').textContent = safe(data.signals?.signal);

  const profileName = data.profile?.name || data.profile?.clientName || data.profile?.data?.name;
  const availableFunds = data.funds?.available_funds || data.funds?.availableFunds || data.funds?.data?.availableFunds;
  const usedMargin = data.funds?.used_margin || data.funds?.usedMargin || data.funds?.data?.usedMargin || 0;

  renderRows('account-row', [
    `<tr><td>${safe(profileName)}</td><td>SAMCO</td><td>${safe(availableFunds)}</td><td>${safe(usedMargin)}</td><td>${safe(data.pnl, 0)}</td></tr>`,
  ], 5);

  const positions = (data.positions || []).map((p) =>
    `<tr><td>${safe(p.symbol)}</td><td>${safe(p.strike)}</td><td>${safe(p.type)}</td><td>${safe(p.qty)}</td><td>${safe(p.entry)}</td><td>${safe(p.ltp)}</td><td>${safe(p.pnl, 0)}</td><td>${safe(p.status)}</td></tr>`);
  renderRows('positions', positions, 8);

  const orders = (data.orders || []).map((o) =>
    `<tr><td>${safe(o.time)}</td><td>${safe(o.symbol)}</td><td>${safe(o.type)}</td><td>${safe(o.side)}</td><td>${safe(o.qty)}</td><td>${safe(o.price)}</td><td>${safe(o.status)}</td></tr>`);
  renderRows('orders', orders, 7);

  const chain = (data.option_chain || []).slice(0, 30);
  const chainRows = chain.map((r) =>
    `<tr><td>${safe(r.call_oi, 0)}</td><td>${safe(r.call_ltp, 0)}</td><td>${safe(r.strike_price)}</td><td>${safe(r.put_ltp, 0)}</td><td>${safe(r.put_oi, 0)}</td></tr>`);
  renderRows('chain', chainRows, 5);

  renderRows('analysis', [
    `<tr><td>${safe(data.analysis?.support_level, 0)}</td><td>${safe(data.analysis?.resistance_level, 0)}</td><td>${safe(data.analysis?.max_call_oi, 0)}</td><td>${safe(data.analysis?.max_put_oi, 0)}</td><td>${safe(data.analysis?.atm_strike, 0)}</td></tr>`,
  ], 5);

  const best = data.best_trades || [];
  document.getElementById('suggestion').textContent = best.length
    ? `Trade Suggestion: ${best[0].display} (confidence ${best[0].confidence}%)`
    : 'Trade Suggestion: None';

  updateChart(chain);
  } finally {
    isRefreshing = false;
  }
}

async function runAction(method, url, text, body) {
  const statusEl = document.getElementById('action-status');
  try {
    const response = await api(method, url, body);
    statusEl.textContent = `Status: ${text} successful (${response.status || 'ok'})`;
  } catch (err) {
    statusEl.textContent = `Status: ${text} failed (${err.message})`;
  }
  await refresh();
}

document.getElementById('toggle-mode').onclick = async () => {
  const dashboard = await api('GET', '/dashboard');
  const next = dashboard.mode === 'PAPER' ? 'REAL' : 'PAPER';
  await runAction('POST', '/trading-mode', 'Mode change', { mode: next });
};

document.getElementById('approve').onclick = () => runAction('POST', '/trade/approve', 'Trade approval');
document.getElementById('close').onclick = () => runAction('POST', '/trade/close', 'Close positions');
document.getElementById('reset').onclick = () => runAction('POST', '/trade/reset', 'Reset');

refresh().catch(() => {
  document.getElementById('action-status').textContent = 'Status: Initial load failed';
});
setInterval(() => {
  if (document.visibilityState !== 'visible') return;
  refresh().catch(() => {});
}, 5000);
  const data = await call('GET', '/dashboard');
  document.getElementById('mode').textContent = data.mode || '-';
  document.getElementById('trend').textContent = data.analysis?.trend || '-';
  document.getElementById('pcr').textContent = data.analysis?.pcr ?? '-';
  document.getElementById('signal').textContent = data.signals?.signal || '-';

  renderTable('account-row', `<tr><td>${row(data.profile?.name || data.profile?.clientName)}</td><td>SAMCO</td><td>${row(data.funds?.available_funds || data.funds?.availableFunds)}</td><td>${row(data.funds?.used_margin || data.funds?.usedMargin || 0)}</td><td>${row(data.pnl)}</td></tr>`);

  renderTable('positions', (data.positions || []).map((p) => `<tr><td>${row(p.symbol)}</td><td>${row(p.strike)}</td><td>${row(p.type)}</td><td>${row(p.qty)}</td><td>${row(p.entry)}</td><td>${row(p.ltp)}</td><td>${row(p.pnl)}</td><td>${row(p.status)}</td></tr>`).join(''));
  renderTable('orders', (data.orders || []).map((o) => `<tr><td>${row(o.time)}</td><td>${row(o.symbol)}</td><td>${row(o.type)}</td><td>${row(o.side)}</td><td>${row(o.qty)}</td><td>${row(o.price)}</td><td>${row(o.status)}</td></tr>`).join(''));

  const chain = (data.option_chain || []).slice(0, 30);
  renderTable('chain', chain.map((r) => `<tr><td>${row(r.call_oi)}</td><td>${row(r.call_ltp)}</td><td>${row(r.strike_price)}</td><td>${row(r.put_ltp)}</td><td>${row(r.put_oi)}</td></tr>`).join(''));

  renderTable('analysis', `<tr><td>${row(data.analysis?.support_level)}</td><td>${row(data.analysis?.resistance_level)}</td><td>${row(data.analysis?.max_call_oi)}</td><td>${row(data.analysis?.max_put_oi)}</td><td>${row(data.analysis?.atm_strike)}</td></tr>`);

  const first = data.best_trades?.[0];
  document.getElementById('suggestion').textContent = first ? `Trade Suggestion: ${first.display} (${first.confidence}%)` : 'Trade Suggestion: None';
  updateChart(chain);
}

document.getElementById('toggle-mode').onclick = async () => {
  const data = await call('GET', '/dashboard');
  const next = data.mode === 'PAPER' ? 'REAL' : 'PAPER';
  await call('POST', '/trading-mode', { mode: next });
  refresh();
};
document.getElementById('approve').onclick = async () => { await call('POST', '/trade/approve'); refresh(); };
document.getElementById('close').onclick = async () => { await call('POST', '/trade/close'); refresh(); };
document.getElementById('reset').onclick = async () => { await call('POST', '/trade/reset'); refresh(); };

refresh();
setInterval(refresh, 5000);
