const widgetsEl = document.getElementById('widgets');
const tableBody = document.querySelector('#optionChainTable tbody');

function trendClass(trend) {
  if (trend === 'BULLISH') return 'bullish';
  if (trend === 'BEARISH') return 'bearish';
  return 'sideways';
}

function renderWidgets(analysis, signal, pnl) {
  const items = [
    ['PCR', analysis?.pcr?.toFixed?.(2) ?? '-'],
    ['Trend', `<span class="${trendClass(analysis?.trend)}">${analysis?.trend ?? '-'}</span>`],
    ['Support', analysis?.support ?? '-'],
    ['Resistance', analysis?.resistance ?? '-'],
    ['Trade Signal', `${signal?.signal ?? '-'} (${((signal?.confidence ?? 0) * 100).toFixed(0)}%)`],
    ['Paper PnL', `₹ ${pnl?.total_pnl ?? 0}`],
    ['Smart Money', analysis?.smart_money_bias ?? '-'],
  ];

  widgetsEl.innerHTML = items
    .map(([title, value]) => `<article class="widget"><h3>${title}</h3><strong>${value}</strong></article>`)
    .join('');
}

function renderTable(rows, analysis) {
  tableBody.innerHTML = rows.map((r) => {
    const classes = [
      r.strike_price === analysis?.atm_strike ? 'atm' : '',
      r.strike_price === analysis?.support ? 'support' : '',
      r.strike_price === analysis?.resistance ? 'resistance' : '',
    ].filter(Boolean).join(' ');

    return `<tr class="${classes}">
      <td>${r.strike_price}</td>
      <td>${r.call_oi.toFixed(0)}</td>
      <td>${r.call_ltp.toFixed(2)}</td>
      <td>${r.put_ltp.toFixed(2)}</td>
      <td>${r.put_oi.toFixed(0)}</td>
    </tr>`;
  }).join('');
}

async function fetchJson(path) {
  const res = await fetch(path);
  return res.json();
}

async function refresh() {
  const [chain, analysis, signal, pnl] = await Promise.all([
    fetchJson('/option-chain'),
    fetchJson('/analysis'),
    fetchJson('/signals'),
    fetchJson('/paper-pnl'),
  ]);

  const rows = chain.data || [];
  renderWidgets(analysis, signal, pnl);
  renderTable(rows, analysis);
  updateCharts(rows);
}

initCharts();
refresh();
setInterval(refresh, 3000);
