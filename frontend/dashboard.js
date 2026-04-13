/**
 * Lords Bot — Dashboard JS
 * Polls /api/dashboard every 2 seconds.
 * Updates all UI elements, chart, trade log.
 */

const API = '';
const POLL_MS = 2000;

let pnlChart = null;
let pnlHistory = [];
let prevSpot = null;
let prevDailyPnl = null;

// ══════════════════════════════════════════════
//  INIT
// ══════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  initChart();
  poll();
  setInterval(poll, POLL_MS);
});

// ══════════════════════════════════════════════
//  POLL
// ══════════════════════════════════════════════
async function poll() {
  try {
    const res = await fetch(`${API}/api/dashboard`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    render(data);
  } catch (err) {
    setStatusOffline();
    console.warn('Poll error:', err);
  }
}

// ══════════════════════════════════════════════
//  RENDER
// ══════════════════════════════════════════════
function render(d) {
  renderHeader(d);
  renderPnl(d);
  renderOrb(d);
  renderSignal(d);
  renderActiveTrade(d);
  renderTradeLog(d.trade_history || []);
  updateChart(d);
  updateFooter(d);
}

function renderHeader(d) {
  // Status pill
  const pill = document.getElementById('status-pill');
  const dot  = pill.querySelector('.status-dot');
  const txt  = document.getElementById('status-text');

  if (d.bot_running) {
    pill.className = 'status-pill running';
    txt.textContent = 'RUNNING';
  } else {
    pill.className = 'status-pill stopped';
    txt.textContent = 'STOPPED';
  }

  // Mode badge
  const badge = document.getElementById('mode-badge');
  const mode  = (d.trading_mode || 'PAPER').toUpperCase();
  badge.textContent = mode;
  badge.className   = 'mode-badge' + (mode === 'LIVE' ? ' live' : '');

  // NIFTY spot
  const spotEl = document.getElementById('nifty-spot');
  if (d.nifty_spot != null) {
    const formatted = fmt(d.nifty_spot, 2);
    if (prevSpot !== null && d.nifty_spot !== prevSpot) {
      flash(spotEl, d.nifty_spot > prevSpot ? 'flash-up' : 'flash-down');
    }
    spotEl.textContent = formatted;
    prevSpot = d.nifty_spot;
  }

  // Last update
  const ts = d.timestamp ? new Date(d.timestamp).toLocaleTimeString('en-IN') : '—';
  document.getElementById('last-update').textContent = ts;
}

function renderPnl(d) {
  // Daily PnL
  const dpEl   = document.getElementById('daily-pnl');
  const dpCard = document.getElementById('card-daily-pnl');
  const dp     = d.daily_pnl ?? 0;

  if (prevDailyPnl !== null && dp !== prevDailyPnl) {
    flash(dpEl, dp > prevDailyPnl ? 'flash-up' : 'flash-down');
  }
  dpEl.textContent = fmtPnl(dp);
  dpCard.className = 'card card--pnl ' + pnlClass(dp);
  prevDailyPnl = dp;

  document.getElementById('trade-count').textContent =
    `${d.trade_count ?? 0} trade${d.trade_count === 1 ? '' : 's'} today`;

  // Live PnL
  const lpEl   = document.getElementById('live-pnl');
  const lpCard = document.getElementById('card-live-pnl');
  const lp     = d.live_pnl ?? 0;
  lpEl.textContent = fmtPnl(lp);
  lpCard.className = 'card card--pnl ' + pnlClass(lp);
}

function renderOrb(d) {
  const high = d.orb_high;
  const low  = d.orb_low;
  document.getElementById('orb-high').textContent = high != null ? fmt(high, 2) : '—';
  document.getElementById('orb-low').textContent  = low  != null ? fmt(low,  2) : '—';

  if (high != null && low != null) {
    const range = (high - low).toFixed(1);
    document.getElementById('orb-range-pts').textContent = `Range: ${range} pts`;
  } else {
    document.getElementById('orb-range-pts').textContent = 'Range: —';
  }
}

function renderSignal(d) {
  const el  = document.getElementById('signal-display');
  const sub = document.getElementById('signal-sub');
  const sig = d.signal;

  if (sig === 'CALL') {
    el.textContent  = '▲ CALL';
    el.className    = 'card-value signal-display signal-call';
    sub.textContent = 'breakout above ORB high';
  } else if (sig === 'PUT') {
    el.textContent  = '▼ PUT';
    el.className    = 'card-value signal-display signal-put';
    sub.textContent = 'breakdown below ORB low';
  } else {
    el.textContent  = 'NONE';
    el.className    = 'card-value signal-display signal-none';
    sub.textContent = 'waiting for breakout';
  }
}

function renderActiveTrade(d) {
  const trade   = d.active_trade;
  const badge   = document.getElementById('trade-badge');
  const noTrade = document.getElementById('no-trade');
  const details = document.getElementById('trade-details');
  const flatBtn = document.getElementById('btn-flatten');

  if (!trade) {
    badge.textContent = 'NO POSITION';
    badge.className   = 'trade-status-badge';
    noTrade.classList.remove('hidden');
    details.classList.add('hidden');
    flatBtn.classList.add('hidden');
    return;
  }

  badge.textContent = '● OPEN';
  badge.className   = 'trade-status-badge open';
  noTrade.classList.add('hidden');
  details.classList.remove('hidden');
  flatBtn.classList.remove('hidden');

  set('td-symbol', trade.symbol || '—');
  set('td-entry',  trade.entry_price != null ? `₹${fmt(trade.entry_price, 2)}` : '—');
  set('td-qty',    trade.qty ?? '—');
  set('td-sl',     trade.sl_price   != null ? `₹${fmt(trade.sl_price,   2)}` : '—');
  set('td-t1',     trade.t1_price   != null ? `₹${fmt(trade.t1_price,   2)}` : '—');
  set('td-t2',     trade.t2_price   != null ? `₹${fmt(trade.t2_price,   2)}` : '—');
  set('td-t1hit',  trade.t1_booked ? '✓ BOOKED' : (trade.t1_hit ? '✓ HIT' : '—'));
  set('td-ltp',    d.live_pnl != null && trade.entry_price
    ? `₹${fmt(trade.entry_price + d.live_pnl / (trade.qty || 1), 2)}`
    : '—'
  );
}

function renderTradeLog(trades) {
  const tbody = document.getElementById('trade-tbody');
  if (!trades || trades.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-row">No trades today</td></tr>';
    return;
  }

  // Most recent first
  const rows = [...trades].reverse().map(t => {
    const pnl    = parseFloat(t.pnl ?? 0);
    const pnlCls = pnl > 0 ? 'pnl-pos' : pnl < 0 ? 'pnl-neg' : 'pnl-zer';
    const sigCls = (t.signal || '').toUpperCase() === 'CALL' ? 'sig-call' : 'sig-put';
    return `
      <tr>
        <td>${t.time || '—'}</td>
        <td class="${sigCls}">${(t.signal || '').toUpperCase()}</td>
        <td>${t.symbol || '—'}</td>
        <td>${t.entry   != null ? `₹${fmt(parseFloat(t.entry),      2)}` : '—'}</td>
        <td>${t.exit_price != null ? `₹${fmt(parseFloat(t.exit_price), 2)}` : '—'}</td>
        <td>${t.qty || '—'}</td>
        <td class="${pnlCls}">${fmtPnl(pnl)}</td>
        <td>${t.reason || '—'}</td>
      </tr>
    `;
  });
  tbody.innerHTML = rows.join('');
}

// ══════════════════════════════════════════════
//  CHART
// ══════════════════════════════════════════════
function initChart() {
  const ctx = document.getElementById('pnl-chart').getContext('2d');
  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels:   [],
      datasets: [{
        label:           'Daily P&L',
        data:            [],
        borderColor:     '#00d97e',
        backgroundColor: 'rgba(0,217,126,0.08)',
        borderWidth:     2,
        pointRadius:     3,
        pointBackgroundColor: '#00d97e',
        fill:            true,
        tension:         0.3,
      }]
    },
    options: {
      responsive:          true,
      maintainAspectRatio: false,
      animation:           { duration: 300 },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#0f1218',
          borderColor:     '#1e2433',
          borderWidth:     1,
          titleColor:      '#64748b',
          bodyColor:       '#e2e8f0',
          callbacks: {
            label: ctx => `  ₹${ctx.parsed.y.toFixed(2)}`
          }
        }
      },
      scales: {
        x: {
          grid:   { color: '#1e2433' },
          ticks:  { color: '#475569', font: { family: 'JetBrains Mono', size: 10 } },
        },
        y: {
          grid:   { color: '#1e2433' },
          ticks:  {
            color: '#475569',
            font:  { family: 'JetBrains Mono', size: 10 },
            callback: v => `₹${v}`
          }
        }
      }
    }
  });
}

function updateChart(d) {
  const trades = d.trade_history || [];
  if (!trades.length) return;

  // Build cumulative P&L series from trade log
  let cumPnl  = 0;
  const labels = [];
  const values = [];

  trades.forEach(t => {
    const pnl = parseFloat(t.pnl ?? 0);
    if (pnl !== 0) {
      cumPnl += pnl;
      labels.push(t.time || '');
      values.push(parseFloat(cumPnl.toFixed(2)));
    }
  });

  if (!values.length) return;

  document.getElementById('chart-empty').style.display = 'none';

  const color = cumPnl >= 0 ? '#00d97e' : '#ff4d6d';
  pnlChart.data.labels                            = labels;
  pnlChart.data.datasets[0].data                  = values;
  pnlChart.data.datasets[0].borderColor           = color;
  pnlChart.data.datasets[0].backgroundColor       = `${color}15`;
  pnlChart.data.datasets[0].pointBackgroundColor  = color;
  pnlChart.update('none');
}

// ══════════════════════════════════════════════
//  API ACTIONS
// ══════════════════════════════════════════════
async function startBot() {
  await apiPost('/api/start');
}

async function stopBot() {
  await apiPost('/api/stop');
}

async function flattenPosition() {
  if (!confirm('Flatten position now?')) return;
  const res = await apiPost('/api/trade/flatten');
  alert(res?.status === 'flattened'
    ? `Flattened ${res.symbol} qty=${res.qty}`
    : `Result: ${JSON.stringify(res)}`
  );
}

async function setMode(mode) {
  await apiPost('/api/trading-mode', { mode });
  document.getElementById('btn-paper').classList.toggle('active', mode === 'PAPER');
  document.getElementById('btn-live').classList.toggle('active',  mode === 'LIVE');
}

async function setTrading(enabled) {
  await apiPost('/api/trading-enabled', { enabled });
  document.getElementById('btn-enable').classList.toggle('active',  enabled);
  document.getElementById('btn-disable').classList.toggle('active', !enabled);
}

async function apiPost(endpoint, body = {}) {
  try {
    const res = await fetch(`${API}${endpoint}`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    return await res.json();
  } catch (err) {
    console.error(`POST ${endpoint} failed:`, err);
    return null;
  }
}

// ══════════════════════════════════════════════
//  HELPERS
// ══════════════════════════════════════════════
function setStatusOffline() {
  const pill = document.getElementById('status-pill');
  pill.className = 'status-pill stopped';
  document.getElementById('status-text').textContent = 'OFFLINE';
}

function updateFooter(d) {
  const mode = (d.trading_mode || 'PAPER').toUpperCase();
  document.getElementById('footer-mode').textContent = `Mode: ${mode}`;
}

function set(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function fmt(n, decimals = 2) {
  if (n == null || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function fmtPnl(n) {
  if (n == null || isNaN(n)) return '₹0';
  const sign = n >= 0 ? '+' : '';
  return `${sign}₹${fmt(Math.abs(n), 2)}`;
}

function pnlClass(n) {
  if (n > 0) return 'positive';
  if (n < 0) return 'negative';
  return '';
}

function flash(el, cls) {
  el.classList.remove('flash-up', 'flash-down');
  void el.offsetWidth; // reflow
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), 700);
}
