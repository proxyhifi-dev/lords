/**
 * Lords Bot v4.0 — Dashboard JavaScript
 * Real-time polling, ORB visualiser, analytics panel, trade log.
 */

const API = '';
const POLL_MS = 1500;

let prevSpot      = null;
let tradingEnabled = true;
let pollTimer     = null;
let lastData      = null;

// ── Bootstrap ──────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  poll();
  loadAnalytics();
  pollTimer = setInterval(poll, POLL_MS);
});

// ── Main Poll ──────────────────────────────────────────
async function poll() {
  try {
    const res = await fetch(`${API}/api/dashboard`);
    if (!res.ok) throw new Error(res.status);
    const d = await res.json();
    lastData = d;
    render(d);
  } catch {
    setOffline();
  }
}

function render(d) {
  updateHeader(d);
  updateKPIs(d);
  updateTrade(d);
  updateORBViz(d);
  updateLog(d.trade_history || []);
  updateFooter(d);
}

// ── Header ────────────────────────────────────────────
function updateHeader(d) {
  const spot = d.nifty_spot;

  // Status pill
  const pill   = document.getElementById('status-pill');
  const stText = document.getElementById('status-text');
  if (d.bot_running) {
    pill.className = 'pill pill--online';
    stText.textContent = 'LIVE';
  } else {
    pill.className = 'pill pill--offline';
    stText.textContent = 'OFFLINE';
  }

  // Mode badge
  const badge = document.getElementById('mode-badge');
  badge.textContent = d.trading_mode || 'PAPER';
  badge.style.color = d.trading_mode === 'LIVE' ? 'var(--red)' : 'var(--yellow)';

  // NIFTY spot
  if (spot) {
    const el    = document.getElementById('nifty-spot');
    const delta = document.getElementById('spot-change');
    el.textContent = formatNum(spot);

    if (prevSpot !== null) {
      const chg = spot - prevSpot;
      const pct = (chg / prevSpot * 100).toFixed(2);
      delta.textContent = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)} (${pct}%)`;
      delta.className = 'spot-delta ' + (chg >= 0 ? 'up' : 'down');

      el.style.color = chg >= 0 ? 'var(--green)' : 'var(--red)';
      setTimeout(() => { el.style.color = 'var(--accent)'; }, 600);
    }
    prevSpot = spot;
  }

  // Timestamp
  const ts = new Date(d.timestamp || Date.now());
  document.getElementById('last-update').textContent =
    ts.toLocaleTimeString('en-IN', { hour12: false });
}

// ── KPIs ──────────────────────────────────────────────
function updateKPIs(d) {
  // Daily P&L
  const dpnl  = d.daily_pnl || 0;
  const dpEl  = document.getElementById('daily-pnl');
  dpEl.textContent = fmtPnl(dpnl);
  dpEl.className   = 'kpi-value ' + (dpnl >= 0 ? 'positive' : 'negative');

  // P&L progress bar (vs MAX_DAILY_LOSS ~ 5000)
  const maxLoss = 5000;
  const pct = Math.min(Math.abs(dpnl) / maxLoss * 100, 100);
  const bar = document.getElementById('pnl-bar');
  bar.style.width = pct + '%';
  bar.style.background = dpnl >= 0 ? 'var(--green)' : 'var(--red)';

  document.getElementById('trade-count').textContent =
    `${d.trade_count || 0} trade${(d.trade_count || 0) !== 1 ? 's' : ''} today`;

  // Live P&L
  const lpnl = d.live_pnl || 0;
  const lpEl = document.getElementById('live-pnl');
  lpEl.textContent = fmtPnl(lpnl);
  lpEl.className   = 'kpi-value ' + (lpnl >= 0 ? 'positive' : 'negative');

  const trade = d.active_trade;
  document.getElementById('live-symbol').textContent =
    trade ? (trade.symbol || 'open trade') : 'no open trade';

  // ORB
  const orbRange  = document.getElementById('orb-range');
  const orbLevels = document.getElementById('orb-levels');
  if (d.orb_high && d.orb_low) {
    const range = (d.orb_high - d.orb_low).toFixed(1);
    orbRange.textContent  = `${range} pts`;
    orbLevels.textContent = `H: ${d.orb_high.toFixed(0)}  L: ${d.orb_low.toFixed(0)}`;
  } else {
    orbRange.textContent  = '—';
    orbLevels.textContent = 'building…';
  }

  // Signal
  const sigEl  = document.getElementById('signal-display');
  const sigSub = document.getElementById('trading-status');
  const sig    = d.signal;
  sigEl.textContent = sig || 'NONE';
  sigEl.className   = 'kpi-value signal-value' +
    (sig === 'CALL' ? ' call' : sig === 'PUT' ? ' put' : '');

  tradingEnabled = d.trading_enabled !== false;
  sigSub.textContent = !d.bot_running ? 'bot offline' :
    !tradingEnabled ? '⛔ trading paused' :
    sig ? `signal: ${sig}` : 'scanning…';

  // Sync pause button
  const btn = document.getElementById('btn-trading');
  btn.textContent = tradingEnabled ? 'PAUSE' : 'RESUME';
  btn.className   = tradingEnabled ? 'btn' : 'btn btn--yellow';
}

// ── Active Trade Panel ────────────────────────────────
function updateTrade(d) {
  const trade  = d.active_trade;
  const badge  = document.getElementById('trade-status-badge');
  const content= document.getElementById('trade-content');

  if (!trade) {
    badge.textContent = 'NONE';
    badge.className   = 'badge badge--none';
    content.innerHTML = '<div class="trade-empty">No active trade</div>';
    return;
  }

  badge.textContent = 'OPEN';
  badge.className   = 'badge badge--open';

  const ltp   = d.nifty_spot;
  const entry = trade.entry_price || 0;
  const sl    = trade.sl_price   || 0;
  const t1    = trade.t1_price   || 0;
  const t2    = trade.t2_price   || 0;
  const livePnl = d.live_pnl || 0;

  // Price progress bar: SL → Entry → T1 → T2
  let fillPct = 0, fillColor = 'var(--accent)';
  if (ltp && entry) {
    const range = t2 - sl;
    if (range > 0) {
      fillPct  = Math.max(0, Math.min((ltp - sl) / range * 100, 100));
      fillColor = ltp < entry ? 'var(--red)' : ltp < t1 ? 'var(--yellow)' : 'var(--green)';
    }
  }

  const entryTime = trade.entry_time
    ? new Date(trade.entry_time).toLocaleTimeString('en-IN', { hour12: false })
    : '—';

  content.innerHTML = `
    <div class="trade-field">
      <div class="trade-field-label">SYMBOL</div>
      <div class="trade-field-value">${trade.symbol || '—'}</div>
    </div>
    <div class="trade-field">
      <div class="trade-field-label">SIGNAL / QTY</div>
      <div class="trade-field-value ${trade.signal === 'CALL' ? 'positive' : 'negative'}">
        ${trade.signal || '—'} × ${trade.qty || 0}
      </div>
    </div>
    <div class="trade-field">
      <div class="trade-field-label">ENTRY PRICE</div>
      <div class="trade-field-value">₹${entry.toFixed(2)}</div>
    </div>
    <div class="trade-field">
      <div class="trade-field-label">ENTRY TIME</div>
      <div class="trade-field-value">${entryTime}</div>
    </div>
    <div class="trade-field">
      <div class="trade-field-label">LIVE P&L</div>
      <div class="trade-field-value ${livePnl >= 0 ? 'positive' : 'negative'}">
        ${fmtPnl(livePnl)}
      </div>
    </div>
    <div class="trade-field">
      <div class="trade-field-label">T1 STATUS</div>
      <div class="trade-field-value ${trade.t1_booked ? 'positive' : ''}">
        ${trade.t1_booked ? '✓ BOOKED' : trade.t1_hit ? '✓ HIT' : 'WAITING'}
      </div>
    </div>
    <div class="trade-progress">
      <div class="trade-field-label">SL ₹${sl.toFixed(0)} → ENTRY ₹${entry.toFixed(0)} → T1 ₹${t1.toFixed(0)} → T2 ₹${t2.toFixed(0)}</div>
      <div class="progress-track">
        <div class="progress-fill" style="width:${fillPct}%;background:${fillColor}"></div>
      </div>
      <div class="progress-markers">
        <span style="color:var(--red)">SL</span>
        <span>ENTRY</span>
        <span style="color:var(--yellow)">T1</span>
        <span style="color:var(--green)">T2</span>
      </div>
    </div>
  `;
}

// ── ORB Visualiser ────────────────────────────────────
function updateORBViz(d) {
  const viz = document.getElementById('orb-viz');
  const { orb_high, orb_low, nifty_spot } = d;

  if (!orb_high || !orb_low) {
    viz.innerHTML = '<div class="orb-placeholder">Building ORB range…</div>';
    return;
  }

  const range  = orb_high - orb_low;
  const pad    = range * 0.5;
  const vizMin = orb_low  - pad;
  const vizMax = orb_high + pad;
  const vizRange = vizMax - vizMin;

  const toY = (price) => Math.max(2, Math.min(96,
    (1 - (price - vizMin) / vizRange) * 100));

  const highPct = toY(orb_high);
  const lowPct  = toY(orb_low);
  const midPct  = toY((orb_high + orb_low) / 2);
  const zonePct = lowPct - highPct;

  let spotLine = '';
  if (nifty_spot) {
    const spotPct = toY(nifty_spot);
    const spotColor = nifty_spot > orb_high ? 'var(--green)' :
                      nifty_spot < orb_low  ? 'var(--red)' : 'var(--accent)';
    spotLine = `
      <div class="orb-spot-line" style="top:${spotPct}%;background:${spotColor}"></div>
      <div class="orb-label spot" style="top:${spotPct - 3}%">
        SPOT ${nifty_spot.toFixed(0)}
      </div>`;
  }

  // Breakout buffer lines (5pts)
  const buPct = toY(orb_high + 5);
  const bdPct = toY(orb_low  - 5);

  viz.innerHTML = `
    <div class="orb-chart">
      <div class="orb-zone" style="top:${highPct}%;height:${zonePct}%"></div>
      <div class="orb-high-line" style="top:${highPct}%"></div>
      <div class="orb-low-line"  style="top:${lowPct}%"></div>
      <div style="position:absolute;left:0;right:0;height:1px;top:${buPct}%;
                  background:var(--green);opacity:0.3;border-top:1px dashed var(--green)"></div>
      <div style="position:absolute;left:0;right:0;height:1px;top:${bdPct}%;
                  background:var(--red);opacity:0.3;border-top:1px dashed var(--red)"></div>
      <div class="orb-label high" style="top:${highPct - 4}%">H ${orb_high.toFixed(0)}</div>
      <div class="orb-label low"  style="top:${lowPct + 1}%">L ${orb_low.toFixed(0)}</div>
      ${spotLine}
    </div>
    <div style="display:flex;justify-content:space-between;margin-top:6px;font-size:9px;color:var(--text-muted)">
      <span style="color:var(--green)">▬ ORB HIGH</span>
      <span style="color:var(--accent)">RANGE: ${range.toFixed(1)} pts</span>
      <span style="color:var(--red)">▬ ORB LOW</span>
    </div>`;

  document.getElementById('orb-meta').textContent = `9:15 – 9:30 IST · Range: ${range.toFixed(1)}pts`;
}

// ── Trade Log ─────────────────────────────────────────
function updateLog(trades) {
  const body  = document.getElementById('log-body');
  const count = document.getElementById('log-count');

  count.textContent = `${trades.length} trade${trades.length !== 1 ? 's' : ''}`;

  if (!trades.length) {
    body.innerHTML = '<tr><td colspan="7" class="log-empty">No trades yet</td></tr>';
    return;
  }

  const rows = [...trades].reverse().map(t => {
    const pnl    = parseFloat(t.pnl || 0);
    const reason = (t.exit_reason || t.reason || '—').toUpperCase();
    const sig    = (t.signal || '').toUpperCase();
    const time   = t.time || t.entry_time || '—';

    const reasonClass = reason.includes('SL') ? 'sl' :
      reason.includes('T2') || reason.includes('TARGET') ? 't2' :
      reason.includes('T1') || reason.includes('TRAIL')  ? 't1' : 'eod';

    return `<tr>
      <td>${String(time).substring(11, 19) || time}</td>
      <td class="td-signal ${sig.toLowerCase()}">${sig}</td>
      <td>${t.symbol || '—'}</td>
      <td>₹${parseFloat(t.entry || t.entry_price || 0).toFixed(2)}</td>
      <td>₹${parseFloat(t.exit_price || t.sell_price || 0).toFixed(2)}</td>
      <td class="td-reason ${reasonClass}">${reason}</td>
      <td class="td-pnl ${pnl >= 0 ? 'positive' : 'negative'}">${fmtPnl(pnl)}</td>
    </tr>`;
  }).join('');

  body.innerHTML = rows;
}

// ── Analytics Panel ───────────────────────────────────
async function loadAnalytics() {
  const grid = document.getElementById('analytics-grid');
  grid.innerHTML = '<div class="analytics-placeholder">Loading…</div>';

  try {
    const res = await fetch(`${API}/api/analytics`);
    const a   = await res.json();

    if (a.status === 'error' || !a.total_trades) {
      grid.innerHTML = '<div class="analytics-placeholder">No trades yet — analytics available after first trade</div>';
      return;
    }

    const items = [
      { label: 'WIN RATE',       value: `${a.win_rate}%`,    cls: a.win_rate >= 55 ? 'positive' : a.win_rate >= 45 ? 'neutral' : 'negative' },
      { label: 'NET P&L',        value: fmtPnl(a.net_pnl),   cls: a.net_pnl >= 0 ? 'positive' : 'negative' },
      { label: 'PROFIT FACTOR',  value: `${a.profit_factor}x`, cls: a.profit_factor >= 1.5 ? 'positive' : a.profit_factor >= 1.0 ? 'neutral' : 'negative' },
      { label: 'SHARPE RATIO',   value: a.sharpe,             cls: a.sharpe >= 1.5 ? 'positive' : a.sharpe >= 0.5 ? 'neutral' : 'negative' },
      { label: 'SORTINO RATIO',  value: a.sortino,            cls: a.sortino >= 2 ? 'positive' : a.sortino >= 1 ? 'neutral' : 'negative' },
      { label: 'MAX DRAWDOWN',   value: fmtPnl(a.max_drawdown), cls: 'negative' },
      { label: 'REWARD / RISK',  value: `${a.reward_risk}x`, cls: a.reward_risk >= 1.5 ? 'positive' : 'neutral' },
      { label: 'KELLY FRACTION', value: `${a.half_kelly_pct}%`, cls: 'neutral' },
      { label: 'EV / TRADE',     value: fmtPnl(a.ev_per_trade), cls: a.ev_per_trade >= 0 ? 'positive' : 'negative' },
      { label: 'CALMAR RATIO',   value: a.calmar_ratio,       cls: a.calmar_ratio >= 1 ? 'positive' : 'neutral' },
      { label: 'CAPITAL MIN',    value: `₹${(a.capital_min/1000).toFixed(0)}K`, cls: 'neutral' },
      { label: 'CAPITAL REC.',   value: `₹${(a.capital_recommended/1000).toFixed(0)}K`, cls: 'neutral' },
    ];

    grid.innerHTML = items.map(item => `
      <div class="analytics-item">
        <div class="analytics-label">${item.label}</div>
        <div class="analytics-value ${item.cls}">${item.value}</div>
      </div>`
    ).join('');

  } catch {
    grid.innerHTML = '<div class="analytics-placeholder">Error loading analytics</div>';
  }
}

// ── Controls ──────────────────────────────────────────
async function startBot() {
  await apiPost('/api/start');
  showAlert('Bot started', 'success');
  setTimeout(poll, 500);
}

async function stopBot() {
  await apiPost('/api/stop');
  showAlert('Bot stopped', 'info');
  setTimeout(poll, 500);
}

async function setMode(mode) {
  await apiPost('/api/trading-mode', { mode });
  document.getElementById('btn-paper').className = mode === 'PAPER' ? 'btn btn--active' : 'btn';
  document.getElementById('btn-live').className  = mode === 'LIVE'  ? 'btn btn--active' : 'btn';
  showAlert(`Mode set to ${mode}`, mode === 'LIVE' ? 'error' : 'info');
}

async function toggleTrading() {
  const enabled = !tradingEnabled;
  await apiPost('/api/trading-enabled', { enabled });
  showAlert(enabled ? 'Trading resumed' : 'Trading paused', 'info');
  setTimeout(poll, 300);
}

async function flattenPosition() {
  if (!confirm('Flatten all open positions now?')) return;
  const res = await apiPost('/api/trade/flatten');
  showAlert(res.status === 'flattened' ? `Flattened ${res.symbol}` : res.status, 'info');
  setTimeout(poll, 500);
}

// ── Helpers ───────────────────────────────────────────
function setOffline() {
  document.getElementById('status-pill').className = 'pill pill--offline';
  document.getElementById('status-text').textContent = 'OFFLINE';
}

function updateFooter(d) {
  document.getElementById('footer-mode').textContent =
    `MODE: ${d.trading_mode || 'PAPER'} · BOT: ${d.bot_running ? 'RUNNING' : 'STOPPED'}`;
}

async function apiPost(endpoint, body = {}) {
  try {
    const res = await fetch(`${API}${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return res.json();
  } catch { return {}; }
}

function showAlert(msg, type = 'info') {
  const el = document.getElementById('alert-banner');
  el.textContent = msg;
  el.className = `alert-banner ${type}`;
  setTimeout(() => { el.className = 'alert-banner hidden'; }, 3000);
}

function formatNum(n) {
  return Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtPnl(n) {
  const v = parseFloat(n) || 0;
  return (v >= 0 ? '+' : '') + '₹' + Math.abs(v).toLocaleString('en-IN', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}
