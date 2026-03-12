const widgetsEl = document.getElementById('widgets');
const tableBody = document.querySelector('#optionChainTable tbody');
const emptyMessage = document.getElementById('emptyMessage');

function trendClass(trend) {
  if (trend === 'BULLISH') return 'bullish';
  if (trend === 'BEARISH') return 'bearish';
  return 'sideways';
}

function renderWidgets(analysis, signal) {
  const items = [
    ['PCR', analysis?.pcr?.toFixed?.(2) ?? '-'],
    ['Trend', `<span class="${trendClass(analysis?.trend)}">${analysis?.trend ?? '-'}</span>`],
    ['Support', analysis?.support ?? '-'],
    ['Resistance', analysis?.resistance ?? '-'],
    ['Signal', `${signal?.signal ?? '-'} (${((signal?.confidence ?? 0) * 100).toFixed(0)}%)`],
  ];

  widgetsEl.innerHTML = items.map(([t, v]) => `<article class="widget"><h3>${t}</h3><strong>${v}</strong></article>`).join('');
}

function renderTable(rows, analysis) {
  if (!rows.length) {
    emptyMessage.classList.remove('hidden');
    tableBody.innerHTML = '';
    return;
  }
  emptyMessage.classList.add('hidden');
  tableBody.innerHTML = rows.map((r) => {
    const classes = [
      r.strike_price === analysis?.atm_strike ? 'atm' : '',
      r.strike_price === analysis?.support ? 'support' : '',
      r.strike_price === analysis?.resistance ? 'resistance' : '',
    ].filter(Boolean).join(' ');

    return `<tr class="${classes}"><td>${r.strike_price}</td><td>${r.call_oi.toFixed(0)}</td><td>${r.call_ltp.toFixed(2)}</td><td>${r.put_ltp.toFixed(2)}</td><td>${r.put_oi.toFixed(0)}</td></tr>`;
  }).join('');
}

async function fetchJson(path, fallback = {}) {
  try {
    const res = await fetch(path);
    return await res.json();
  } catch {
    return fallback;
  }
}

async function toggleMode() {
  const mode = await fetchJson('/trading-mode');
  const next = mode.mode === 'PAPER' ? 'REAL' : 'PAPER';
  if (next === 'REAL' && !confirm('Enable REAL trading? Orders may be live.')) return;
  await fetch('/trading-mode', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: next }) });
}

document.getElementById('modeToggle').addEventListener('click', toggleMode);

async function refresh() {
  const [chain, analysis, signal, pnl, apiStatus, mode, profile, funds] = await Promise.all([
    fetchJson('/option-chain', { data: [] }),
    fetchJson('/analysis', {}),
    fetchJson('/signals', {}),
    fetchJson('/paper-pnl', {}),
    fetchJson('/api-status', {}),
    fetchJson('/trading-mode', {}),
    fetchJson('/profile', {}),
    fetchJson('/funds', {}),
  ]);

  const rows = chain.data || [];
  renderWidgets(analysis, signal);
  renderTable(rows, analysis);
  updateCharts(rows);

  document.getElementById('apiStatus').textContent = apiStatus.status || '-';
  document.getElementById('modeText').textContent = mode.mode || '-';
  document.getElementById('paperPanel').textContent = JSON.stringify(pnl, null, 2);
  document.getElementById('profilePanel').textContent = JSON.stringify(profile, null, 2);
  document.getElementById('fundsPanel').textContent = JSON.stringify(funds, null, 2);
}

initCharts();
refresh();
setInterval(refresh, 3000);
