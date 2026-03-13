let chart;

async function call(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
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
    data: { labels, datasets: [
      { label: 'Call OI', data: callOI, borderColor: '#ff6b6b' },
      { label: 'Put OI', data: putOI, borderColor: '#4dabf7' },
    ]},
  });
}

async function refresh() {
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
