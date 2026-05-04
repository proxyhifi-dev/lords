/**
 * Lords Bot v6.0 — Iron Condor Dashboard
 * Polls /api/dashboard and /api/iron-condor/stats
 * No ORB references.
 */

const API     = '';
const POLL_MS = 2000;
const IC_POLL_MS = 5000;

let prevSpot       = null;
let tradingEnabled = true;
let icData         = null;

// ── Bootstrap ──────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  poll();
  loadICStats();
  loadAnalytics();
  setInterval(poll,        POLL_MS);
  setInterval(loadICStats, IC_POLL_MS);
});

// ── Main Poll ──────────────────────────────────────────
async function poll() {
  try {
    const res = await fetch(`${API}/api/dashboard`);
    if (!res.ok) throw new Error(res.statusText);
    const d = await res.json();
    renderMain(d);
  } catch {
    setOffline();
  }
}

function renderMain(d) {
  updateHeader(d);
  updateKPIs(d);
  updateICPosition(d);
  updateLog(d.trades || []);
  updateFooter(d);
}

// ── Header ─────────────────────────────────────────────
function updateHeader(d) {
  const spot = d.nifty_spot;

  const pill   = document.getElementById('status-pill');
  const stText = document.getElementById('status-text');
  if (d.bot_running) {
    pill.className = 'pill pill--online';
    stText.textContent = 'LIVE';
  } else {
    pill.className = 'pill pill--offline';
    stText.textContent = 'OFFLINE';
  }

  const badge = document.getElementById('mode-badge');
  badge.textContent = d.trading_mode || 'PAPER';
  badge.style.color = d.trading_mode === 'LIVE' ? 'var(--red)' : 'var(--yellow)';

  if (spot) {
    const el    = document.getElementById('nifty-spot');
    const delta = document.getElementById('spot-change');
    el.textContent = fmtNum(spot);
    if (prevSpot !== null) {
      const chg = spot - prevSpot;
      const pct = (chg / prevSpot * 100).toFixed(2);
      delta.textContent = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)} (${pct}%)`;
      delta.className   = 'spot-delta ' + (chg >= 0 ? 'up' : 'down');
      el.style.color    = chg >= 0 ? 'var(--green)' : 'var(--red)';
      setTimeout(() => { el.style.color = 'var(--accent)'; }, 600);
    }
    prevSpot = spot;
  }

  document.getElementById('last-update').textContent =
    new Date(d.timestamp || Date.now()).toLocaleTimeString('en-IN', { hour12: false });
}

// ── KPIs ───────────────────────────────────────────────
function updateKPIs(d) {
  const dpnl = d.daily_pnl || 0;
  const dpEl = document.getElementById('daily-pnl');
  dpEl.textContent = fmtPnl(dpnl);
  dpEl.className   = 'kpi-value ' + (dpnl >= 0 ? 'positive' : 'negative');

  const maxLoss = 5000;
  const bar     = document.getElementById('pnl-bar');
  bar.style.width      = Math.min(Math.abs(dpnl) / maxLoss * 100, 100) + '%';
  bar.style.background = dpnl >= 0 ? 'var(--green)' : 'var(--red)';

  document.getElementById('trade-count').textContent =
    `${d.trade_count || 0} trade${(d.trade_count || 0) !== 1 ? 's' : ''} today`;

  const lpnl = d.live_pnl || 0;
  const lpEl = document.getElementById('live-pnl');
  lpEl.textContent = fmtPnl(lpnl);
  lpEl.className   = 'kpi-value ' + (lpnl >= 0 ? 'positive' : 'negative');

  const trade = d.active_trade;
  document.getElementById('live-symbol').textContent =
    trade ? (trade.strategy === 'IRON_CONDOR' ? 'IC active' : trade.symbol || 'open') : 'no open position';

  // IC Cycle status
  const cycleEl    = document.getElementById('cycle-status');
  const cycleMonth = document.getElementById('cycle-month');
  const lastMonth  = d.last_ic_month;
  const thisMonth  = new Date().getMonth() + 1;
  if (trade && trade.strategy === 'IRON_CONDOR') {
    cycleEl.textContent = 'ACTIVE';
    cycleEl.className   = 'kpi-value positive';
    cycleMonth.textContent = `position open`;
  } else if (lastMonth === thisMonth) {
    cycleEl.textContent = 'TRADED';
    cycleEl.className   = 'kpi-value neutral';
    cycleMonth.textContent = `already traded month ${thisMonth}`;
  } else {
    cycleEl.textContent = 'READY';
    cycleEl.className   = 'kpi-value';
    cycleMonth.textContent = `waiting for day 1-5`;
  }

  // Signal
  const sigEl  = document.getElementById('signal-display');
  const sigSub = document.getElementById('trading-status');
  const sig    = d.signal;
  sigEl.textContent = sig || 'NONE';
  sigEl.className   = 'kpi-value signal-value' + (sig === 'IRON_CONDOR' ? ' ic' : '');

  tradingEnabled = d.trading_enabled !== false;
  sigSub.textContent = !d.bot_running ? 'bot offline' :
    !tradingEnabled   ? '⛔ trading paused' :
    sig               ? `signal: ${sig}` : 'scanning…';

  const btn     = document.getElementById('btn-trading');
  btn.textContent = tradingEnabled ? 'PAUSE' : 'RESUME';
  btn.className   = tradingEnabled ? 'btn' : 'btn btn--yellow';
}

// ── Iron Condor Position Panel ─────────────────────────
function updateICPosition(d) {
  const trade  = d.active_trade;
  const badge  = document.getElementById('ic-status-badge');
  const content= document.getElementById('ic-content');

  if (!trade || trade.strategy !== 'IRON_CONDOR') {
    badge.textContent = 'INACTIVE';
    badge.className   = 'badge badge--none';
    content.innerHTML = `
      <div class="ic-empty">
        <span class="ic-empty-icon">⬡</span>
        <span>No active Iron Condor</span>
      </div>`;
    renderPayoff(null);
    return;
  }

  badge.textContent = 'OPEN';
  badge.className   = 'badge badge--open';

  const ep      = trade.entry_price || 0;
  const livePnl = d.live_pnl || 0;
  const strikes = trade.strikes || {};
  const qty     = trade.qty || 0;
  const entryT  = trade.entry_time
    ? new Date(trade.entry_time).toLocaleString('en-IN', { hour12: false })
    : '—';

  // Use IC stats if loaded
  const ic = icData && icData.status === 'active' ? icData : null;
  const curPrem = ic ? ic.current_premium : null;
  const estPnl  = ic ? ic.estimated_pnl  : null;
  const ttPeak  = ic ? ic.until_theta_peak : null;

  const premDecay = curPrem && ep
    ? Math.max(0, Math.min(100, (1 - curPrem / ep) * 100))
    : 0;

  content.innerHTML = `
    <div class="ic-grid">
      <div class="ic-field">
        <div class="ic-field-label">STRIKES (SC/SP)</div>
        <div class="ic-field-value accent">${trade.strike || '—'}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">ENTRY PREMIUM</div>
        <div class="ic-field-value">₹${ep.toFixed(2)}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">CURRENT PREMIUM</div>
        <div class="ic-field-value ${curPrem && curPrem < ep ? 'positive' : 'negative'}">
          ${curPrem !== null ? '₹' + curPrem.toFixed(2) : '—'}
        </div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">QTY</div>
        <div class="ic-field-value">${qty}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">ENTRY TIME</div>
        <div class="ic-field-value">${entryT}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">LIVE P&L</div>
        <div class="ic-field-value ${livePnl >= 0 ? 'positive' : 'negative'}">${fmtPnl(livePnl)}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">EST P&L (NET)</div>
        <div class="ic-field-value ${estPnl !== null && estPnl >= 0 ? 'positive' : 'negative'}">
          ${estPnl !== null ? fmtPnl(estPnl) : '—'}
        </div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">→ THETA PEAK</div>
        <div class="ic-field-value ${ttPeak !== null && ttPeak < 30 ? 'positive' : ''}">
          ${ttPeak !== null ? ttPeak + ' min' : '—'}
        </div>
      </div>
    </div>

    <div class="ic-decay-section">
      <div class="ic-field-label">PREMIUM DECAY PROGRESS</div>
      <div class="decay-track">
        <div class="decay-fill" style="width:${premDecay.toFixed(1)}%"></div>
        <span class="decay-label">${premDecay.toFixed(0)}% decayed</span>
      </div>
      <div class="decay-markers">
        <span>ENTRY ₹${ep.toFixed(0)}</span>
        <span class="positive">TARGET 50% = ₹${(ep * 0.5).toFixed(0)}</span>
        <span class="negative">SL 1.5× = ₹${(ep * 1.5).toFixed(0)}</span>
      </div>
    </div>

    <div class="ic-legs-section">
      <div class="ic-field-label">POSITION LEGS</div>
      <div class="ic-legs">
        <div class="leg sell">SELL ${strikes.short_call || '?'} CE</div>
        <div class="leg buy">BUY  ${strikes.long_call  || '?'} CE</div>
        <div class="leg sell">SELL ${strikes.short_put  || '?'} PE</div>
        <div class="leg buy">BUY  ${strikes.long_put   || '?'} PE</div>
      </div>
    </div>
  `;

  renderPayoff(trade, d.nifty_spot);
}

// ── IC Payoff Diagram ──────────────────────────────────
function renderPayoff(trade, spot) {
  const wrap = document.getElementById('payoff-wrap');
  const meta = document.getElementById('payoff-meta');

  if (!trade) {
    wrap.innerHTML = '<div class="payoff-placeholder">Activate Iron Condor to see payoff</div>';
    meta.textContent = '—';
    return;
  }

  const s   = trade.strikes || {};
  const sc  = s.short_call || 0;
  const lc  = s.long_call  || 0;
  const sp  = s.short_put  || 0;
  const lp  = s.long_put   || 0;
  const ep  = trade.entry_price || 0;
  const qty = trade.qty || 65;

  if (!sc || !lc || !sp || !lp) {
    wrap.innerHTML = '<div class="payoff-placeholder">Strike data not available</div>';
    return;
  }

  const maxProfit = ep * qty;
  const maxLoss   = ((lc - sc) * qty) - maxProfit;

  // Build SVG payoff
  const W = 420, H = 130, PAD = 30;
  const range  = lp * 0.85;
  const maxVal = lc * 1.15;
  const points = [];
  const steps  = 80;

  for (let i = 0; i <= steps; i++) {
    const price = range + (maxVal - range) * (i / steps);
    let pnl = ep;
    if (price < lp)       pnl = -(lc - sc - ep);
    else if (price < sp)  pnl = ep - (sp - price);
    else if (price <= sc) pnl = ep;
    else if (price < lc)  pnl = ep - (price - sc);
    else                  pnl = -(lc - sc - ep);
    points.push({ price, pnl });
  }

  const maxPnl = Math.max(...points.map(p => p.pnl));
  const minPnl = Math.min(...points.map(p => p.pnl));
  const pnlRange = maxPnl - minPnl || 1;

  const toX = p => PAD + (p.price - range) / (maxVal - range) * (W - PAD * 2);
  const toY = p => H - PAD - (p.pnl - minPnl) / pnlRange * (H - PAD * 2);

  const zero  = H - PAD - (0 - minPnl) / pnlRange * (H - PAD * 2);
  const pathD = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${toX(p).toFixed(1)},${toY(p).toFixed(1)}`).join(' ');

  // Fill above zero = green, below = red
  const fillAbove = `M${toX(points[0])},${zero} ` +
    points.map(p => `L${toX(p).toFixed(1)},${Math.min(toY(p), zero).toFixed(1)}`).join(' ') +
    ` L${toX(points[points.length - 1])},${zero} Z`;

  const fillBelow = `M${toX(points[0])},${zero} ` +
    points.map(p => `L${toX(p).toFixed(1)},${Math.max(toY(p), zero).toFixed(1)}`).join(' ') +
    ` L${toX(points[points.length - 1])},${zero} Z`;

  // Strike markers
  const strikeMarkers = [
    { price: lp, label: `LP ${lp}`, color: '#4fc3f7' },
    { price: sp, label: `SP ${sp}`, color: '#ef9a9a' },
    { price: sc, label: `SC ${sc}`, color: '#ef9a9a' },
    { price: lc, label: `LC ${lc}`, color: '#4fc3f7' },
  ].filter(m => m.price >= range && m.price <= maxVal);

  // Current spot
  let spotLine = '';
  if (spot && spot >= range && spot <= maxVal) {
    const sx = toX({ price: spot });
    spotLine = `
      <line x1="${sx}" y1="${PAD}" x2="${sx}" y2="${H - PAD}" stroke="var(--accent)" stroke-width="1.5" stroke-dasharray="4,2" opacity="0.8"/>
      <text x="${sx + 3}" y="${PAD + 10}" fill="var(--accent)" font-size="8" font-family="var(--font-mono)">SPOT</text>`;
  }

  wrap.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="payoff-svg">
      <defs>
        <clipPath id="cp"><rect x="${PAD}" y="${PAD}" width="${W - PAD * 2}" height="${H - PAD * 2}"/></clipPath>
      </defs>
      <!-- zero line -->
      <line x1="${PAD}" y1="${zero.toFixed(1)}" x2="${W - PAD}" y2="${zero.toFixed(1)}"
            stroke="var(--border-hi)" stroke-width="1"/>
      <!-- profit fill -->
      <path d="${fillAbove}" fill="var(--green)" opacity="0.15" clip-path="url(#cp)"/>
      <!-- loss fill -->
      <path d="${fillBelow}" fill="var(--red)" opacity="0.15" clip-path="url(#cp)"/>
      <!-- payoff line -->
      <path d="${pathD}" fill="none" stroke="var(--accent)" stroke-width="2" clip-path="url(#cp)"/>
      <!-- strike lines -->
      ${strikeMarkers.map(m => {
        const x = toX({ price: m.price });
        return `<line x1="${x}" y1="${PAD}" x2="${x}" y2="${H - PAD}"
                  stroke="${m.color}" stroke-width="1" stroke-dasharray="3,3" opacity="0.5"/>
                <text x="${x + 2}" y="${H - PAD - 4}" fill="${m.color}" font-size="7"
                  font-family="var(--font-mono)">${m.label}</text>`;
      }).join('')}
      ${spotLine}
      <!-- labels -->
      <text x="${PAD}" y="${zero - 3}" fill="var(--green)" font-size="7" font-family="var(--font-mono)">PROFIT</text>
      <text x="${PAD}" y="${zero + 10}" fill="var(--red)" font-size="7" font-family="var(--font-mono)">LOSS</text>
    </svg>`;

  meta.textContent = `Max Profit ₹${maxProfit.toFixed(0)} · Max Loss ₹${Math.abs(maxLoss).toFixed(0)}`;
}

// ── IC Stats Loader ────────────────────────────────────
async function loadICStats() {
  try {
    const res = await fetch(`${API}/api/iron-condor/stats`);
    if (!res.ok) return;
    icData = await res.json();
  } catch {
    icData = null;
  }
}

// ── Trade Log ──────────────────────────────────────────
function updateLog(trades) {
  const body  = document.getElementById('log-body');
  const count = document.getElementById('log-count');

  count.textContent = `${trades.length} trade${trades.length !== 1 ? 's' : ''}`;

  if (!trades.length) {
    body.innerHTML = '<tr><td colspan="8" class="log-empty">No trades yet</td></tr>';
    return;
  }

  const rows = [...trades].reverse().map(t => {
    const pnl     = parseFloat(t.net_pnl || t.pnl || 0);
    const charges = parseFloat(t.charges || t.total_charges || 0);
    const reason  = (t.exit_reason || '—').toUpperCase();
    const strat   = (t.strategy || t.signal || '—').toUpperCase();
    const strike  = t.strike || '—';
    const entryP  = parseFloat(t.entry_price || t.entry || 0);
    const exitP   = parseFloat(t.exit_premium || t.exit_price || 0);
    const date    = t.entry_time
      ? new Date(t.entry_time).toLocaleDateString('en-IN')
      : (t.date || '—');

    const reasonCls = reason.includes('TARGET') ? 't2' :
      reason.includes('STOP') || reason.includes('SL') ? 'sl' :
      reason.includes('THETA') ? 't1' : 'eod';

    return `<tr>
      <td>${date}</td>
      <td class="td-signal ic">${strat}</td>
      <td class="mono">${strike}</td>
      <td>₹${entryP.toFixed(2)}</td>
      <td>₹${exitP.toFixed(2)}</td>
      <td class="td-reason ${reasonCls}">${reason}</td>
      <td class="negative">₹${charges.toFixed(0)}</td>
      <td class="td-pnl ${pnl >= 0 ? 'positive' : 'negative'}">${fmtPnl(pnl)}</td>
    </tr>`;
  }).join('');

  body.innerHTML = rows;
}

// ── Analytics ──────────────────────────────────────────
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
      { label: 'WIN RATE',        value: `${a.win_rate}%`,        cls: a.win_rate >= 55 ? 'positive' : a.win_rate >= 45 ? 'neutral' : 'negative' },
      { label: 'NET P&L',         value: fmtPnl(a.net_pnl),       cls: a.net_pnl >= 0 ? 'positive' : 'negative' },
      { label: 'PROFIT FACTOR',   value: `${a.profit_factor}x`,   cls: a.profit_factor >= 1.5 ? 'positive' : a.profit_factor >= 1.0 ? 'neutral' : 'negative' },
      { label: 'SHARPE RATIO',    value: a.sharpe,                 cls: a.sharpe >= 1.5 ? 'positive' : a.sharpe >= 0.5 ? 'neutral' : 'negative' },
      { label: 'MAX DRAWDOWN',    value: fmtPnl(a.max_drawdown),  cls: 'negative' },
      { label: 'REWARD / RISK',   value: `${a.reward_risk}x`,     cls: a.reward_risk >= 1.5 ? 'positive' : 'neutral' },
      { label: 'EV / TRADE',      value: fmtPnl(a.ev_per_trade),  cls: a.ev_per_trade >= 0 ? 'positive' : 'negative' },
      { label: 'KELLY FRACTION',  value: `${a.half_kelly_pct}%`,  cls: 'neutral' },
      { label: 'CALMAR',          value: a.calmar_ratio,           cls: a.calmar_ratio >= 1 ? 'positive' : 'neutral' },
      { label: 'SORTINO',         value: a.sortino,                cls: a.sortino >= 2 ? 'positive' : 'neutral' },
      { label: 'CAPITAL MIN',     value: `₹${(a.capital_min/1000).toFixed(0)}K`, cls: 'neutral' },
      { label: 'CAPITAL REC.',    value: `₹${(a.capital_recommended/1000).toFixed(0)}K`, cls: 'neutral' },
    ];
    grid.innerHTML = items.map(item => `
      <div class="analytics-item">
        <div class="analytics-label">${item.label}</div>
        <div class="analytics-value ${item.cls}">${item.value}</div>
      </div>`).join('');
  } catch {
    grid.innerHTML = '<div class="analytics-placeholder">Error loading analytics</div>';
  }
}

// ── Controls ───────────────────────────────────────────
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
  if (!confirm('Flatten ALL open positions now?')) return;
  const res = await apiPost('/api/trade/flatten');
  showAlert(res.status === 'flattened' ? `Flattened ${res.symbol || 'position'}` : (res.message || res.status), 'info');
  setTimeout(poll, 500);
}

// ── Helpers ────────────────────────────────────────────
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
  setTimeout(() => { el.className = 'alert-banner hidden'; }, 3200);
}
function fmtNum(n) {
  return Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtPnl(n) {
  const v = parseFloat(n) || 0;
  return (v >= 0 ? '+' : '') + '₹' + Math.abs(v).toLocaleString('en-IN',
    { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}