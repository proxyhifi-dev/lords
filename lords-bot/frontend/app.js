async function fetchJson(url, fallback = {}) {
  try {
    const res = await fetch(url);
    return await res.json();
  } catch {
    return fallback;
  }
}

async function refresh() {
  const data = await fetchJson('/dashboard', { option_chain: [] });
  document.getElementById('mode').textContent = `Mode: ${data.trading_mode || '-'} | Signal: ${data.signals?.signal || '-'}`;
  document.getElementById('summary').textContent = JSON.stringify({
    profile: data.profile,
    funds: data.funds,
    analysis: data.analysis,
    signals: data.signals,
  }, null, 2);

  const rows = data.option_chain || [];
  document.getElementById('rows').innerHTML = rows.slice(0, 20).map((r) =>
    `<tr><td>${r.strike_price ?? '-'}</td><td>${Number(r.call_oi || 0).toFixed(0)}</td><td>${Number(r.put_oi || 0).toFixed(0)}</td></tr>`
  ).join('');
  updateChart(rows.slice(0, 20));
}

async function toggleMode() {
  const current = await fetchJson('/trading-mode');
  const next = current.mode === 'PAPER' ? 'REAL' : 'PAPER';
  await fetch('/trading-mode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: next }),
  });
  refresh();
}

document.getElementById('toggle').addEventListener('click', toggleMode);
refresh();
setInterval(refresh, 5000);
