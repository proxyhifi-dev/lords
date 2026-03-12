async function loadDashboard() {
  const [indexRes, chainRes, signalRes] = await Promise.all([
    fetch('/index-price'),
    fetch('/option-chain'),
    fetch('/signal'),
  ]);

  const indexData = await indexRes.json();
  const chainData = await chainRes.json();
  const signalData = await signalRes.json();

  document.getElementById('spot').textContent = indexData.price ?? '--';
  document.getElementById('atm').textContent = indexData.atm_strike ?? '--';
  document.getElementById('signal').textContent = signalData.signal ?? 'NEUTRAL';

  const rows = chainData.strikes ?? [];
  const atm = indexData.atm_strike;
  const atmRow = rows.find((row) => Number(row.strike_price) === Number(atm));
  document.getElementById('oi').textContent = atmRow
    ? `${Math.round(atmRow.ce_oi)} / ${Math.round(atmRow.pe_oi)}`
    : '--';

  document.getElementById('chainRows').innerHTML = rows
    .map((row) => {
      const highlight = Number(row.strike_price) === Number(atm) ? 'bg-emerald-900/40' : '';
      return `<tr class="border-b border-slate-800 ${highlight}">
        <td class="py-2 pr-4">${row.strike_price}</td>
        <td class="py-2 pr-4">${Math.round(row.ce_oi)}</td>
        <td class="py-2 pr-4">${Math.round(row.pe_oi)}</td>
        <td class="py-2 pr-4">${Number(row.ce_ltp).toFixed(2)}</td>
        <td class="py-2 pr-4">${Number(row.pe_ltp).toFixed(2)}</td>
      </tr>`;
    })
    .join('');
}

loadDashboard();
setInterval(loadDashboard, 5000);
