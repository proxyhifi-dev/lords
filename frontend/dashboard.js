/**
 * Lords Bot — Dashboard v3
 * Full live trading dashboard with:
 *   - Real-time spot price with flash animation
 *   - ORB progress bar (fills during 9:15–9:30 build phase)
 *   - Signal levels display (SL / T1 / T2)
 *   - Live PnL meter bar
 *   - Risk meter (daily loss vs limit)
 *   - Alert banner for key events
 *   - Equity curve chart
 *   - Complete trade log
 */

const POLL_MS = 2000;
let pnlChart   = null;
let prevSpot   = null;
let prevDailyPnl = null;
let prevActiveTrade = null;
let alertTimer = null;

// ─────────────────────────────────────────────
//  INIT
// ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initChart();
  poll();
  setInterval(poll, POLL_MS);
});

// ─────────────────────────────────────────────
//  POLL
// ─────────────────────────────────────────────
async function poll() {
  try {
    const res = await fetch('/api/dashboard');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    render(await res.json());
  } catch (err) {
    setOffline();
  }
}

// ─────────────────────────────────────────────
//  RENDER
// ─────────────────────────────────────────────
function render(d) {
  renderHeader(d);
  renderPnl(d);
  renderOrb(d);
  renderSignal(d);
  renderActiveTrade(d);
  renderRiskMeter(d);
  renderTradeLog(d.trade_history || []);
  updateChart(d);
  updateFooter(d);
  detectAlerts(d);
}

function renderHeader(d) {
  const pill = document.getElementById('status-pill');
  const txt  = document.getElementById('status-text');
  if (d.bot_running) {
    pill.className = 'status-pill running';
    txt.textContent = 'RUNNING';
  } else {
    pill.className = 'status-pill stopped';
    txt.textContent = 'STOPPED';
  }

  const badge = document.getElementById('mode-badge');
  const mode  = (d.trading_mode || 'PAPER').toUpperCase();
  badge.textContent = mode;
  badge.className   = 'mode-badge' + (mode === 'LIVE' ? ' live' : '');

  // Spot price with flash + change indicator
  const spotEl = document.getElementById('nifty-spot');
  const chgEl  = document.getElementById('spot-change');
  if (d.nifty_spot != null) {
    if (prevSpot !== null && d.nifty_spot !== prevSpot) {
      const up = d.nifty_spot > prevSpot;
      flash(spotEl, up ? 'flash-up' : 'flash-dn');
      const diff = (d.nifty_spot - prevSpot).toFixed(1);
      chgEl.textContent = (up ? '▲' : '▼') + ' ' + Math.abs(diff);
      chgEl.style.color = up ? 'var(--green)' : 'var(--red)';
    }
    spotEl.textContent = fmt(d.nifty_spot, 2);
    prevSpot = d.nifty_spot;
  }

  const ts = d.timestamp ? new Date(d.timestamp).toLocaleTimeString('en-IN') : '—';
  document.getElementById('last-update').textContent = ts;
}

function renderPnl(d) {
  const dp     = d.daily_pnl ?? 0;
  const dpEl   = document.getElementById('daily-pnl');
  const dpCard = document.getElementById('card-daily-pnl');
  if (prevDailyPnl !== null && dp !== prevDailyPnl)
    flash(dpEl, dp > prevDailyPnl ? 'flash-up' : 'flash-dn');
  dpEl.textContent  = fmtPnl(dp);
  dpCard.className  = 'card card--pnl ' + pnlCls(dp);
  prevDailyPnl      = dp;
  document.getElementById('trade-count').textContent =
    `${d.trade_count ?? 0} trade${d.trade_count === 1 ? '' : 's'} today`;

  const lp     = d.live_pnl ?? 0;
  const lpEl   = document.getElementById('live-pnl');
  const lpCard = document.getElementById('card-live-pnl');
  lpEl.textContent  = fmtPnl(lp);
  lpCard.className  = 'card card--pnl ' + pnlCls(lp);
  document.getElementById('live-pnl-sub').textContent =
    d.active_trade ? 'open position' : 'no open position';
}

function renderOrb(d) {
  const high = d.orb_high, low = d.orb_low;
  document.getElementById('orb-high').textContent = high != null ? fmt(high, 2) : '—';
  document.getElementById('orb-low').textContent  = low  != null ? fmt(low,  2) : '—';

  if (high != null && low != null) {
    const range = (high - low).toFixed(1);
    document.getElementById('orb-range-pts').textContent = `Range: ${range} pts`;
    document.getElementById('orb-progress').style.width = '100%';
  } else {
    // Show build progress during 9:15–9:30
    const now = new Date();
    const mins = now.getHours() * 60 + now.getMinutes();
    const start = 9 * 60 + 15, end = 9 * 60 + 30;
    if (mins >= start && mins < end) {
      const pct = Math.min(((mins - start) / (end - start)) * 100, 100);
      document.getElementById('orb-progress').style.width = pct + '%';
      document.getElementById('orb-range-pts').textContent = 'Building ORB...';
    } else {
      document.getElementById('orb-progress').style.width = '0%';
      document.getElementById('orb-range-pts').textContent = 'Range: —';
    }
  }
}

function renderSignal(d) {
  const el  = document.getElementById('signal-display');
  const sub = document.getElementById('signal-sub');
  const lvl = document.getElementById('signal-levels');
  const sig = d.signal;

  if (sig === 'CALL') {
    el.textContent = '▲ CALL'; el.className = 'card-value signal-display signal-call';
    sub.textContent = 'breakout above ORB high';
  } else if (sig === 'PUT') {
    el.textContent = '▼ PUT'; el.className = 'card-value signal-display signal-put';
    sub.textContent = 'breakdown below ORB low';
  } else {
    el.textContent = 'NONE'; el.className = 'card-value signal-display signal-none';
    sub.textContent = 'waiting for candle close';
    lvl.classList.add('hidden'); return;
  }

  // Show SL/T1/T2 from active trade if available
  const t = d.active_trade;
  if (t) {
    lvl.classList.remove('hidden');
    document.getElementById('sig-sl').textContent = `SL ₹${fmt(t.sl_price, 0)}`;
    document.getElementById('sig-t1').textContent = `T1 ₹${fmt(t.t1_price, 0)}`;
    document.getElementById('sig-t2').textContent = `T2 ₹${fmt(t.t2_price, 0)}`;
  } else {
    lvl.classList.add('hidden');
  }
}

function renderActiveTrade(d) {
  const trade   = d.active_trade;
  const badge   = document.getElementById('trade-badge');
  const noTrade = document.getElementById('no-trade');
  const details = document.getElementById('trade-details');
  const flatBtn = document.getElementById('btn-flatten');
  const meter   = document.getElementById('pnl-meter');
  const mLabel  = document.getElementById('pnl-meter-label');

  // Detect new trade opened → alert
  const hasNewTrade = trade && !prevActiveTrade;
  const tradeJustClosed = !trade && prevActiveTrade;
  if (hasNewTrade)   showAlert(`ENTRY ${trade.signal} ${trade.symbol} @ ₹${fmt(trade.entry_price,2)}`, 'info');
  if (tradeJustClosed) {
    const hist = d.trade_history;
    const last = hist && hist.length ? hist[hist.length-1] : null;
    if (last) {
      const p = parseFloat(last.pnl || 0);
      showAlert(`EXIT ${last.symbol} ₹${fmtPnl(p)} — ${last.reason}`, p >= 0 ? 'info' : 'danger');
    }
  }
  prevActiveTrade = trade;

  if (!trade) {
    badge.textContent = 'NO POSITION'; badge.className = 'trade-status-badge';
    noTrade.classList.remove('hidden'); details.classList.add('hidden');
    flatBtn.classList.add('hidden'); return;
  }

  badge.textContent = '● OPEN'; badge.className = 'trade-status-badge open';
  noTrade.classList.add('hidden'); details.classList.remove('hidden');
  flatBtn.classList.remove('hidden');

  set('td-symbol', trade.symbol || '—');
  set('td-entry',  trade.entry_price != null ? `₹${fmt(trade.entry_price,2)}` : '—');
  set('td-qty',    trade.qty ?? '—');
  set('td-sl',     trade.sl_price != null ? `₹${fmt(trade.sl_price,2)}` : '—');
  set('td-t1',     trade.t1_price != null ? `₹${fmt(trade.t1_price,2)}` : '—');
  set('td-t2',     trade.t2_price != null ? `₹${fmt(trade.t2_price,2)}` : '—');
  set('td-t1hit',  trade.t1_booked ? '✓ BOOKED' : trade.t1_hit ? '✓ HIT' : 'pending');

  // Compute LTP from live_pnl
  const lp = d.live_pnl ?? 0;
  const rem = trade.t1_booked ? (trade.t2_qty || trade.qty/2) : trade.qty;
  const ltp = rem > 0 ? trade.entry_price + lp / rem : trade.entry_price;
  set('td-ltp', `₹${fmt(ltp, 2)}`);

  // PnL meter: center=entry, left=SL, right=T1
  const sl  = trade.sl_price  || trade.entry_price * 0.75;
  const t1  = trade.t1_price  || trade.entry_price * 1.40;
  const range = t1 - sl;
  const pct   = range > 0 ? ((ltp - sl) / range) * 100 : 50;
  const clamp = Math.max(0, Math.min(100, pct));
  meter.style.width      = clamp + '%';
  meter.style.background = lp >= 0 ? 'var(--green)' : 'var(--red)';
  mLabel.textContent     = `Live P&L: ${fmtPnl(lp)}`;
  mLabel.style.color     = lp >= 0 ? 'var(--green)' : 'var(--red)';
}

function renderRiskMeter(d) {
  const used  = Math.abs(Math.min(d.daily_pnl ?? 0, 0));
  // Get max_daily_loss from settings — fallback to 3000
  const limit = 3000;
  const pct   = Math.min((used / limit) * 100, 100);
  const fill  = document.getElementById('risk-fill');
  fill.style.width = pct + '%';
  fill.style.background = pct > 80 ? 'var(--red)' : pct > 50 ? 'var(--yellow)' : 'var(--green)';
  document.getElementById('risk-label').textContent =
    `₹${fmt(used,0)} used / ₹${fmt(limit,0)} limit`;
  document.getElementById('footer-risk').textContent = `Risk: ₹${fmt(used,0)} used`;
  if (pct > 80 && d.bot_running)
    showAlert(`⚠ Risk alert: ₹${fmt(used,0)} of ₹${fmt(limit,0)} daily limit used`, 'warn');
}

function renderTradeLog(trades) {
  const tbody = document.getElementById('trade-tbody');
  document.getElementById('log-count').textContent = `${trades.length} trade${trades.length===1?'':'s'}`;
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-row">No trades today</td></tr>'; return;
  }
  tbody.innerHTML = [...trades].reverse().map(t => {
    const pnl    = parseFloat(t.pnl ?? 0);
    const pnlCls = pnl > 0 ? 'pnl-pos' : pnl < 0 ? 'pnl-neg' : '';
    const sigCls = (t.signal||'').toUpperCase() === 'CALL' ? 'sig-call' : 'sig-put';
    return `<tr>
      <td>${t.time||'—'}</td>
      <td class="${sigCls}">${(t.signal||'').toUpperCase()}</td>
      <td style="font-size:.7rem">${t.symbol||'—'}</td>
      <td>${t.entry!=null?`₹${fmt(parseFloat(t.entry),0)}`:'—'}</td>
      <td>${t.exit_price!=null?`₹${fmt(parseFloat(t.exit_price),0)}`:'—'}</td>
      <td>${t.qty||'—'}</td>
      <td class="${pnlCls}">${fmtPnl(pnl)}</td>
      <td style="font-size:.65rem">${t.reason||'—'}</td>
    </tr>`;
  }).join('');
}

// ─────────────────────────────────────────────
//  CHART
// ─────────────────────────────────────────────
function initChart() {
  const ctx = document.getElementById('pnl-chart').getContext('2d');
  pnlChart  = new Chart(ctx, {
    type: 'line',
    data: { labels: [], datasets: [{
      label: 'Equity', data: [],
      borderColor: '#00d97e', backgroundColor: 'rgba(0,217,126,.07)',
      borderWidth: 2, pointRadius: 3, pointBackgroundColor: '#00d97e',
      fill: true, tension: 0.35,
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 250 },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#0d1117', borderColor: '#253044', borderWidth: 1,
          titleColor: '#5a7090', bodyColor: '#e2e8f0',
          callbacks: { label: c => `  ${fmtPnl(c.parsed.y)}` }
        }
      },
      scales: {
        x: { grid: { color: '#1a2233' }, ticks: { color: '#5a7090', font: { family: 'JetBrains Mono', size: 10 }}},
        y: { grid: { color: '#1a2233' }, ticks: { color: '#5a7090', font: { family: 'JetBrains Mono', size: 10 },
          callback: v => `₹${v}` }}
      }
    }
  });
}

function updateChart(d) {
  const trades = d.trade_history || [];
  let cum = 0;
  const labels = [], vals = [];
  trades.forEach(t => {
    const p = parseFloat(t.pnl ?? 0);
    if (p !== 0) { cum += p; labels.push(t.time||''); vals.push(parseFloat(cum.toFixed(2))); }
  });
  if (!vals.length) return;
  document.getElementById('chart-empty').style.display = 'none';
  const col = cum >= 0 ? '#00d97e' : '#ff4560';
  pnlChart.data.labels                           = labels;
  pnlChart.data.datasets[0].data                 = vals;
  pnlChart.data.datasets[0].borderColor          = col;
  pnlChart.data.datasets[0].backgroundColor      = col + '12';
  pnlChart.data.datasets[0].pointBackgroundColor = col;
  pnlChart.update('none');
}

// ─────────────────────────────────────────────
//  ALERTS
// ─────────────────────────────────────────────
function detectAlerts(d) {
  if (!d.trading_enabled && d.bot_running)
    showAlert('⚠ Trading DISABLED — daily loss limit or capital guard triggered', 'danger');
}

function showAlert(msg, type = 'info') {
  const el = document.getElementById('alert-banner');
  el.textContent = msg;
  el.className   = `alert-banner ${type}`;
  if (alertTimer) clearTimeout(alertTimer);
  alertTimer = setTimeout(() => el.classList.add('hidden'), 6000);
}

// ─────────────────────────────────────────────
//  API ACTIONS
// ─────────────────────────────────────────────
async function startBot() {
  const r = await post('/api/start');
  if (r) showAlert('Bot started ✓', 'info');
}

async function stopBot() {
  const r = await post('/api/stop');
  if (r) showAlert('Bot stopped', 'warn');
}

async function flattenPosition() {
  if (!confirm('⚡ Flatten open position NOW? This places a market SELL order.')) return;
  const r = await post('/api/trade/flatten');
  if (r?.status === 'flattened')
    showAlert(`Flattened ${r.symbol} qty=${r.qty}`, 'info');
  else
    showAlert(`Flatten result: ${JSON.stringify(r)}`, 'warn');
}

async function setMode(mode) {
  if (mode === 'LIVE' && !confirm('⚠ Switch to LIVE mode? Real orders will be placed!')) return;
  await post('/api/trading-mode', { mode });
  document.getElementById('btn-paper').classList.toggle('active', mode === 'PAPER');
  document.getElementById('btn-live').classList.toggle('active',  mode === 'LIVE');
  showAlert(`Mode set to ${mode}`, mode === 'LIVE' ? 'danger' : 'info');
}

async function setTrading(enabled) {
  await post('/api/trading-enabled', { enabled });
  document.getElementById('btn-enable').classList.toggle('active',  enabled);
  document.getElementById('btn-disable').classList.toggle('active', !enabled);
  showAlert(`Trading ${enabled ? 'ENABLED' : 'DISABLED'}`, enabled ? 'info' : 'warn');
}

async function post(url, body = {}) {
  try {
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return await r.json();
  } catch { return null; }
}

// ─────────────────────────────────────────────
//  HELPERS
// ─────────────────────────────────────────────
function setOffline() {
  const pill = document.getElementById('status-pill');
  pill.className = 'status-pill stopped';
  document.getElementById('status-text').textContent = 'OFFLINE';
}

function updateFooter(d) {
  document.getElementById('mode-display').textContent = (d.trading_mode||'PAPER').toUpperCase();
}

function set(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function fmt(n, dec = 2) {
  if (n == null || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-IN', { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

function fmtPnl(n) {
  if (n == null || isNaN(n)) return '₹0';
  return (n >= 0 ? '+' : '') + '₹' + fmt(Math.abs(n), 2);
}

function pnlCls(n) { return n > 0 ? 'positive' : n < 0 ? 'negative' : ''; }

function flash(el, cls) {
  el.classList.remove('flash-up', 'flash-dn');
  void el.offsetWidth;
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), 700);
}
