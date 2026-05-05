// frontend/dashboard.js
const API = '';
const POLL_MS = 2000;
const IC_POLL_MS = 5000;

let prevSpot = null;
let tradingEnabled = true;
let icData = null;

document.addEventListener('DOMContentLoaded', () => {
  poll();
  loadICStats();
  loadAnalytics();
  setInterval(poll, POLL_MS);
  setInterval(loadICStats, IC_POLL_MS);
});

async function poll() {
  try {
    const res = await fetch(`${API}/api/dashboard`);
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    renderMain(data);
  } catch {
    setOffline();
  }
}

function renderMain(data) {
  updateHeader(data);
  updateKPIs(data);
  updateICPosition(data);
  updateLog(data.trades || []);
  updateFooter(data);
}

function updateHeader(data) {
  const spot = data.nifty_spot;
  const pill = document.getElementById('status-pill');
  const stText = document.getElementById('status-text');

  if (data.bot_running) {
    pill.className = 'pill pill--online';
    stText.textContent = 'LIVE';
  } else {
    pill.className = 'pill pill--offline';
    stText.textContent = 'OFFLINE';
  }

  const badge = document.getElementById('mode-badge');
  const mode = String(data.trading_mode || 'PAPER').toUpperCase();
  badge.textContent = mode;
  badge.style.color = mode === 'LIVE' ? 'var(--red)' : 'var(--yellow)';

  if (spot !== null && spot !== undefined) {
    const el = document.getElementById('nifty-spot');
    const delta = document.getElementById('spot-change');
    el.textContent = fmtNum(spot);

    if (prevSpot !== null) {
      const change = Number(spot) - Number(prevSpot);
      const pct = Number(prevSpot) !== 0 ? ((change / Number(prevSpot)) * 100).toFixed(2) : '0.00';
      delta.textContent = `${change >= 0 ? '+' : ''}${change.toFixed(2)} (${pct}%)`;
      delta.className = `spot-delta ${change >= 0 ? 'up' : 'down'}`;
      el.style.color = change >= 0 ? 'var(--green)' : 'var(--red)';

      setTimeout(() => {
        el.style.color = 'var(--accent)';
      }, 600);
    }

    prevSpot = spot;
  }

  document.getElementById('last-update').textContent =
    new Date(data.timestamp || Date.now()).toLocaleTimeString('en-IN', { hour12: false });
}

function updateKPIs(data) {
  const dailyPnl = Number(data.daily_pnl || 0);
  const dailyEl = document.getElementById('daily-pnl');
  dailyEl.textContent = fmtPnl(dailyPnl);
  dailyEl.className = `kpi-value ${dailyPnl >= 0 ? 'positive' : 'negative'}`;

  const maxLoss = 5000;
  const bar = document.getElementById('pnl-bar');
  bar.style.width = `${Math.min((Math.abs(dailyPnl) / maxLoss) * 100, 100)}%`;
  bar.style.background = dailyPnl >= 0 ? 'var(--green)' : 'var(--red)';

  const displayTradeCount = Number.isFinite(Number(data.display_trade_count))
    ? Number(data.display_trade_count)
    : fallbackDisplayTradeCount(data);

  document.getElementById('trade-count').textContent =
    `${displayTradeCount} trade${displayTradeCount !== 1 ? 's' : ''} today`;

  const livePnl = Number(data.live_pnl || 0);
  const liveEl = document.getElementById('live-pnl');
  liveEl.textContent = fmtPnl(livePnl);
  liveEl.className = `kpi-value ${livePnl >= 0 ? 'positive' : 'negative'}`;

  const trade = data.active_trade;
  document.getElementById('live-symbol').textContent =
    trade ? (trade.strategy === 'IRON_CONDOR' ? 'IC active' : (trade.symbol || 'open')) : 'no open position';

  const cycleEl = document.getElementById('cycle-status');
  const cycleMonth = document.getElementById('cycle-month');
  const thisMonth = new Date().getMonth() + 1;
  const lastMonth = data.last_ic_month;

  if (trade && trade.strategy === 'IRON_CONDOR') {
    cycleEl.textContent = 'ACTIVE';
    cycleEl.className = 'kpi-value positive';
    cycleMonth.textContent = 'position open';
  } else if (lastMonth === thisMonth) {
    cycleEl.textContent = 'TRADED';
    cycleEl.className = 'kpi-value neutral';
    cycleMonth.textContent = 'already traded this month';
  } else {
    cycleEl.textContent = 'READY';
    cycleEl.className = 'kpi-value';
    cycleMonth.textContent = 'waiting for next entry';
  }

  const signalEl = document.getElementById('signal-display');
  const signalSub = document.getElementById('trading-status');
  const signal = data.signal;

  signalEl.textContent = signal || 'NONE';
  signalEl.className = `kpi-value signal-value${signal === 'IRON_CONDOR' ? ' ic' : ''}`;

  tradingEnabled = data.trading_enabled !== false;
  signalSub.textContent = !data.bot_running
    ? 'bot offline'
    : !tradingEnabled
      ? '⛔ trading paused'
      : signal
        ? `signal: ${signal}`
        : 'scanning…';

  const btn = document.getElementById('btn-trading');
  btn.textContent = tradingEnabled ? 'PAUSE' : 'RESUME';
  btn.className = tradingEnabled ? 'btn' : 'btn btn--yellow';
}

function fallbackDisplayTradeCount(data) {
  const trades = Array.isArray(data.trades) ? data.trades : [];
  const activeTrade = data.active_trade && data.active_trade.strategy === 'IRON_CONDOR'
    ? data.active_trade
    : null;

  const closedStrategyTrades = trades.filter((trade) => {
    const strategy = String(trade.strategy || trade.signal || '').toUpperCase();
    const isClosed =
      String(trade.status || '').toUpperCase() === 'CLOSED' ||
      Boolean(trade.exit_time) ||
      trade.exit_reason !== undefined && trade.exit_reason !== null ||
      trade.exit_price !== undefined && trade.exit_price !== null ||
      trade.exit_premium !== undefined && trade.exit_premium !== null;
    return strategy === 'IRON_CONDOR' && isClosed;
  });

  return closedStrategyTrades.length + (activeTrade ? 1 : 0);
}

function updateICPosition(data) {
  const trade = data.active_trade;
  const badge = document.getElementById('ic-status-badge');
  const content = document.getElementById('ic-content');

  if (!trade || trade.strategy !== 'IRON_CONDOR') {
    badge.textContent = 'INACTIVE';
    badge.className = 'badge badge--none';
    content.innerHTML = `
      <div class="ic-empty">
        <span class="ic-empty-icon">⬡</span>
        <span>No active Iron Condor</span>
      </div>
    `;
    renderPayoff(null);
    return;
  }

  badge.textContent = 'OPEN';
  badge.className = 'badge badge--open';

  const entryPremium = Number(trade.entry_price || 0);
  const livePnl = Number(data.live_pnl || 0);
  const strikes = trade.strikes || {};
  const premiums = trade.premiums || {};
  const qty = Number(trade.qty || 0);
  const entryTime = trade.entry_time
    ? new Date(trade.entry_time).toLocaleString('en-IN', { hour12: false })
    : '—';

  const pricingSource = resolveTradePricingSource(trade, data.trading_mode);
  const pricingLabel = formatPricingSource(pricingSource);

  const ic = icData && icData.status === 'active' ? icData : null;
  const currentPremium = ic && Number.isFinite(Number(ic.current_premium))
    ? Number(ic.current_premium)
    : null;
  const estimatedPnl = ic && Number.isFinite(Number(ic.estimated_pnl))
    ? Number(ic.estimated_pnl)
    : null;
  const thetaPeak = ic && Number.isFinite(Number(ic.until_theta_peak))
    ? Number(ic.until_theta_peak)
    : null;
  const targetPnl = ic && Number.isFinite(Number(ic.target_pnl))
    ? Number(ic.target_pnl)
    : entryPremium * 0.13;
  const stopLossPrem = ic && Number.isFinite(Number(ic.stop_loss_prem))
    ? Number(ic.stop_loss_prem)
    : entryPremium * 2.10;

  const premiumDecayPct = currentPremium !== null && entryPremium > 0
    ? Math.max(0, Math.min(100, ((entryPremium - currentPremium) / entryPremium) * 100))
    : 0;

  const shortCall = strikes.short_call || '';
  const shortPut = strikes.short_put || '';
  const longCall = strikes.long_call || '';
  const longPut = strikes.long_put || '';
  const strikeLabel = shortCall && shortPut ? `${shortCall}/${shortPut}` : (trade.strike || '—');
  const underlying = trade.underlying || trade.symbol || 'NIFTY';
  const expiry = trade.expiry || '—';

  const legsByName = buildLegMap(trade.legs || []);
  const shortCallLeg = legsByName.short_call || null;
  const longCallLeg = legsByName.long_call || null;
  const shortPutLeg = legsByName.short_put || null;
  const longPutLeg = legsByName.long_put || null;

  const shortCallPrem = getLegDisplayPrice(shortCallLeg, premiums.short_call);
  const longCallPrem = getLegDisplayPrice(longCallLeg, premiums.long_call);
  const shortPutPrem = getLegDisplayPrice(shortPutLeg, premiums.short_put);
  const longPutPrem = getLegDisplayPrice(longPutLeg, premiums.long_put);

  content.innerHTML = `
    <div class="ic-grid">
      <div class="ic-field">
        <div class="ic-field-label">UNDERLYING</div>
        <div class="ic-field-value accent">${escapeHtml(underlying)}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">EXPIRY</div>
        <div class="ic-field-value accent">${escapeHtml(expiry)}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">PRICE SOURCE</div>
        <div class="ic-field-value accent">${escapeHtml(pricingLabel)}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">STRIKES (SC/SP)</div>
        <div class="ic-field-value accent">${escapeHtml(strikeLabel)}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">ENTRY PREMIUM</div>
        <div class="ic-field-value">₹${entryPremium.toFixed(2)}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">CURRENT PREMIUM</div>
        <div class="ic-field-value ${currentPremium !== null && currentPremium <= entryPremium ? 'positive' : 'negative'}">
          ${currentPremium !== null ? `₹${currentPremium.toFixed(2)}` : '—'}
        </div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">QTY</div>
        <div class="ic-field-value">${qty}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">ENTRY TIME</div>
        <div class="ic-field-value">${entryTime}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">LIVE P&L</div>
        <div class="ic-field-value ${livePnl >= 0 ? 'positive' : 'negative'}">${fmtPnl(livePnl)}</div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">EST P&L (NET)</div>
        <div class="ic-field-value ${estimatedPnl !== null && estimatedPnl >= 0 ? 'positive' : 'negative'}">
          ${estimatedPnl !== null ? fmtPnl(estimatedPnl) : '—'}
        </div>
      </div>
      <div class="ic-field">
        <div class="ic-field-label">→ THETA PEAK</div>
        <div class="ic-field-value ${thetaPeak !== null && thetaPeak < 30 ? 'positive' : ''}">
          ${thetaPeak !== null ? `${thetaPeak} min` : '—'}
        </div>
      </div>
    </div>

    <div class="ic-decay-section">
      <div class="ic-field-label">PREMIUM DECAY PROGRESS</div>
      <div class="decay-track">
        <div class="decay-fill" style="width:${premiumDecayPct.toFixed(1)}%"></div>
        <span class="decay-label">${premiumDecayPct.toFixed(0)}% decayed</span>
      </div>
      <div class="decay-markers">
        <span>ENTRY ₹${entryPremium.toFixed(0)}</span>
        <span class="positive">TARGET = ₹${targetPnl.toFixed(0)}</span>
        <span class="negative">SL = ₹${stopLossPrem.toFixed(0)}</span>
      </div>
    </div>

    <div class="ic-legs-section">
      <div class="ic-field-label">POSITION LEGS</div>
      <div class="ic-legs">
        ${renderLegCard(shortCallLeg, 'SELL', shortCall || '?', 'CE', shortCallPrem)}
        ${renderLegCard(longCallLeg, 'BUY', longCall || '?', 'CE', longCallPrem)}
        ${renderLegCard(shortPutLeg, 'SELL', shortPut || '?', 'PE', shortPutPrem)}
        ${renderLegCard(longPutLeg, 'BUY', longPut || '?', 'PE', longPutPrem)}
      </div>
    </div>
  `;

  renderPayoff(trade, data.nifty_spot);
}

function buildLegMap(legs) {
  const result = {};
  for (const leg of legs) {
    if (leg && leg.name) {
      result[leg.name] = leg;
    }
  }
  return result;
}

function getLegDisplayPrice(leg, fallbackPremium) {
  const value = leg?.entry_price ?? leg?.fill_price ?? fallbackPremium ?? 0;
  return Number(value || 0);
}

function resolveTradePricingSource(trade, mode) {
  if (trade?.pricing_source) {
    return trade.pricing_source;
  }

  const orderIds = Array.isArray(trade?.order_ids) ? trade.order_ids : [];
  const singleOrderId = trade?.order_id ? [trade.order_id] : [];
  const allOrderIds = [...orderIds, ...singleOrderId].filter(Boolean);

  if (allOrderIds.some((id) => String(id).startsWith('PAPER-'))) {
    return 'broker_quote_snapshot';
  }

  if (String(mode || '').toUpperCase() === 'LIVE') {
    return 'broker_fill';
  }

  return inferPricingSource(mode);
}

function formatPricingSource(source) {
  const normalized = String(source || '').toLowerCase();
  if (normalized === 'broker_fill') return 'BROKER FILL';
  if (normalized === 'broker_quote_snapshot') return 'BROKER QUOTE SNAPSHOT';
  if (normalized === 'model_fallback') return 'MODEL FALLBACK';
  return normalized ? normalized.toUpperCase().replaceAll('_', ' ') : '—';
}

function inferPricingSource(mode) {
  return String(mode || '').toUpperCase() === 'LIVE' ? 'broker_fill' : 'broker_quote_snapshot';
}

function renderLegCard(leg, side, strike, optionType, entryPrice) {
  const bid = Number(leg?.entry_bid || 0);
  const ask = Number(leg?.entry_ask || 0);
  const ltp = Number(leg?.entry_ltp || 0);
  const source = formatPricingSource(leg?.price_source || '');
  const displaySymbol = leg?.display_symbol || `${strike} ${optionType}`;

  return `
    <div class="leg ${side.toLowerCase()}">
      <div><strong>${side} ${escapeHtml(String(strike))} ${escapeHtml(optionType)}</strong> @ ₹${Number(entryPrice || 0).toFixed(2)}</div>
      <div class="leg-sub mono">${escapeHtml(displaySymbol)}</div>
      <div class="leg-sub">SRC: ${escapeHtml(source || '—')}</div>
      <div class="leg-sub">BID ₹${bid.toFixed(2)} · ASK ₹${ask.toFixed(2)} · LTP ₹${ltp.toFixed(2)}</div>
    </div>
  `;
}

function renderPayoff(trade, spot) {
  const wrap = document.getElementById('payoff-wrap');
  const meta = document.getElementById('payoff-meta');

  if (!trade) {
    wrap.innerHTML = '<div class="payoff-placeholder">Activate Iron Condor to see payoff</div>';
    meta.textContent = '—';
    return;
  }

  const strikes = trade.strikes || {};
  const shortCall = Number(strikes.short_call || 0);
  const longCall = Number(strikes.long_call || 0);
  const shortPut = Number(strikes.short_put || 0);
  const longPut = Number(strikes.long_put || 0);
  const entryPremium = Number(trade.entry_price || 0);
  const qty = Number(trade.qty || 65);

  if (!shortCall || !longCall || !shortPut || !longPut) {
    wrap.innerHTML = '<div class="payoff-placeholder">Strike data not available</div>';
    meta.textContent = '—';
    return;
  }

  const spreadWidth = Math.max(longCall - shortCall, shortPut - longPut);
  const maxProfit = entryPremium * qty;
  const maxLoss = Math.max((spreadWidth - entryPremium) * qty, 0);

  const width = 720;
  const height = 260;
  const padLeft = 54;
  const padRight = 22;
  const padTop = 20;
  const padBottom = 46;

  const focusPad = Math.max(spreadWidth * 2.5, 80);
  const rangeMin = longPut - focusPad;
  const rangeMax = longCall + focusPad;

  const points = [];
  const steps = 140;

  for (let i = 0; i <= steps; i += 1) {
    const price = rangeMin + ((rangeMax - rangeMin) * i) / steps;
    let pnlPerUnit = entryPremium;

    if (price < longPut) {
      pnlPerUnit = -(spreadWidth - entryPremium);
    } else if (price < shortPut) {
      pnlPerUnit = entryPremium - (shortPut - price);
    } else if (price <= shortCall) {
      pnlPerUnit = entryPremium;
    } else if (price < longCall) {
      pnlPerUnit = entryPremium - (price - shortCall);
    } else {
      pnlPerUnit = -(spreadWidth - entryPremium);
    }

    points.push({ price, pnl: pnlPerUnit * qty });
  }

  const maxPnl = Math.max(...points.map((p) => p.pnl), 0);
  const minPnl = Math.min(...points.map((p) => p.pnl), 0);
  const pnlRange = Math.max(maxPnl - minPnl, 1);

  const toX = (price) =>
    padLeft + ((price - rangeMin) / (rangeMax - rangeMin)) * (width - padLeft - padRight);

  const toY = (pnl) =>
    height - padBottom - ((pnl - minPnl) / pnlRange) * (height - padTop - padBottom);

  const zeroY = toY(0);

  const pathD = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${toX(p.price).toFixed(1)},${toY(p.pnl).toFixed(1)}`)
    .join(' ');

  const fillAbove = `M${toX(points[0].price)},${zeroY} ${
    points.map((p) => `L${toX(p.price).toFixed(1)},${Math.min(toY(p.pnl), zeroY).toFixed(1)}`).join(' ')
  } L${toX(points[points.length - 1].price)},${zeroY} Z`;

  const fillBelow = `M${toX(points[0].price)},${zeroY} ${
    points.map((p) => `L${toX(p.price).toFixed(1)},${Math.max(toY(p.pnl), zeroY).toFixed(1)}`).join(' ')
  } L${toX(points[points.length - 1].price)},${zeroY} Z`;

  const markers = [
    { price: longPut, label: `LP ${longPut}`, color: '#4fc3f7', offset: 0 },
    { price: shortPut, label: `SP ${shortPut}`, color: '#ef9a9a', offset: 14 },
    { price: shortCall, label: `SC ${shortCall}`, color: '#ef9a9a', offset: 0 },
    { price: longCall, label: `LC ${longCall}`, color: '#4fc3f7', offset: 14 },
  ];

  let spotLine = '';
  if (spot && spot >= rangeMin && spot <= rangeMax) {
    const sx = toX(Number(spot));
    spotLine = `
      <line x1="${sx}" y1="${padTop}" x2="${sx}" y2="${height - padBottom}"
            stroke="var(--accent)" stroke-width="2" stroke-dasharray="5,3" opacity="0.9"/>
      <text x="${sx + 6}" y="${padTop + 12}" fill="var(--accent)" font-size="11" font-family="var(--font-mono)">SPOT</text>
    `;
  }

  wrap.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid meet" class="payoff-svg">
      <defs>
        <clipPath id="cp">
          <rect x="${padLeft}" y="${padTop}" width="${width - padLeft - padRight}" height="${height - padTop - padBottom}"></rect>
        </clipPath>
      </defs>
      <rect x="${padLeft}" y="${padTop}" width="${width - padLeft - padRight}" height="${height - padTop - padBottom}"
            fill="transparent" stroke="var(--border-hi)" stroke-width="1" opacity="0.35"></rect>
      <line x1="${padLeft}" y1="${zeroY.toFixed(1)}" x2="${width - padRight}" y2="${zeroY.toFixed(1)}"
            stroke="var(--border-hi)" stroke-width="1.2"></line>
      <path d="${fillAbove}" fill="var(--green)" opacity="0.16" clip-path="url(#cp)"></path>
      <path d="${fillBelow}" fill="var(--red)" opacity="0.16" clip-path="url(#cp)"></path>
      <path d="${pathD}" fill="none" stroke="var(--accent)" stroke-width="3" clip-path="url(#cp)"></path>
      ${markers.map((m) => {
        const x = toX(m.price);
        return `
          <line x1="${x}" y1="${padTop}" x2="${x}" y2="${height - padBottom}"
                stroke="${m.color}" stroke-width="1.2" stroke-dasharray="4,4" opacity="0.7"></line>
          <text x="${x}" y="${height - padBottom + 16 + m.offset}" fill="${m.color}" font-size="11"
                font-family="var(--font-mono)" text-anchor="middle">${m.label}</text>
        `;
      }).join('')}
      ${spotLine}
      <text x="${padLeft}" y="${Math.max(padTop + 14, zeroY - 8)}" fill="var(--green)" font-size="12" font-family="var(--font-mono)">PROFIT</text>
      <text x="${padLeft}" y="${Math.min(height - padBottom - 6, zeroY + 16)}" fill="var(--red)" font-size="12" font-family="var(--font-mono)">LOSS</text>
    </svg>
  `;

  meta.textContent = `Max Profit ₹${maxProfit.toFixed(0)} · Max Loss ₹${Math.abs(maxLoss).toFixed(0)}`;
}

async function loadICStats() {
  try {
    const res = await fetch(`${API}/api/iron-condor/stats`);
    if (!res.ok) return;
    icData = await res.json();
  } catch {
    icData = null;
  }
}

function updateLog(trades) {
  const body = document.getElementById('log-body');
  const count = document.getElementById('log-count');

  count.textContent = `${trades.length} trade${trades.length !== 1 ? 's' : ''}`;

  if (!trades.length) {
    body.innerHTML = '<tr><td colspan="12" class="log-empty">No trades yet</td></tr>';
    return;
  }

  body.innerHTML = [...trades].reverse().map((trade) => {
    const pnl = Number(trade.net_pnl || trade.pnl || 0);
    const grossPnl = Number(trade.gross_pnl || trade.pnl || trade.net_pnl || 0);
    const charges = Number(trade.total_charges || trade.charges || 0);

    const rawReason = trade.exit_reason ?? trade.reason;
    const reason = typeof rawReason === 'string' && rawReason.trim()
      ? rawReason.toUpperCase()
      : (trade.status === 'OPEN' ? 'OPEN' : 'CLOSED');

    const strategy = String(trade.strategy || trade.signal || '—').toUpperCase();

    let symbol = String(trade.underlying || trade.symbol || trade.trade_type || 'NIFTY');
    if (symbol.toUpperCase() === 'IRON_CONDOR') {
      symbol = 'NIFTY 50';
    }

    const expiry = String(trade.expiry || '—');
    const strike = trade.strike || formatStrikeFromTrade(trade);
    const qty = Number(trade.qty || 0);
    const entryPrice = Number(trade.entry_price || trade.entry || 0);

    const exitPriceRaw = trade.exit_premium ?? trade.exit_price;
    const exitPrice = exitPriceRaw !== undefined && exitPriceRaw !== null && exitPriceRaw !== ''
      ? Number(exitPriceRaw)
      : null;

    const date = trade.entry_time
      ? new Date(trade.entry_time).toLocaleDateString('en-IN')
      : (trade.date || '—');

    const pricingSource = formatPricingSource(resolveTradePricingSource(trade, trade.trading_mode || ''));

    const reasonCls = reason.includes('TARGET')
      ? 't2'
      : reason.includes('STOP') || reason.includes('SL')
        ? 'sl'
        : reason.includes('THETA')
          ? 't1'
          : reason === 'OPEN'
            ? 'neutral'
            : 'eod';

    return `
      <tr>
        <td>${date}</td>
        <td class="td-signal ic">${escapeHtml(strategy)}</td>
        <td class="mono">${escapeHtml(symbol)}</td>
        <td class="mono">${escapeHtml(expiry)}</td>
        <td class="mono">${escapeHtml(strike || '—')}<div class="mono" style="font-size:11px;opacity:.7">${escapeHtml(pricingSource)}</div></td>
        <td>${qty || '—'}</td>
        <td>₹${entryPrice.toFixed(2)}</td>
        <td>${exitPrice !== null ? `₹${exitPrice.toFixed(2)}` : 'OPEN'}</td>
        <td class="${grossPnl >= 0 ? 'positive' : 'negative'}">${fmtPnl(grossPnl)}</td>
        <td class="negative">₹${charges.toFixed(2)}</td>
        <td class="td-pnl ${pnl >= 0 ? 'positive' : 'negative'}">${fmtPnl(pnl)}</td>
        <td class="td-reason ${reasonCls}">${escapeHtml(reason)}</td>
      </tr>
    `;
  }).join('');
}

function formatStrikeFromTrade(trade) {
  const strikes = trade.strikes || {};
  if (strikes.short_call && strikes.short_put) {
    return `${strikes.short_call}/${strikes.short_put}`;
  }
  return trade.strike || '';
}

async function loadAnalytics() {
  const grid = document.getElementById('analytics-grid');
  grid.innerHTML = '<div class="analytics-placeholder">Loading…</div>';

  try {
    const res = await fetch(`${API}/api/analytics`);
    if (!res.ok) throw new Error('analytics request failed');

    const analytics = await res.json();
    if (analytics.status === 'error' || !analytics.total_trades) {
      grid.innerHTML = '<div class="analytics-placeholder">No trades yet — analytics available after first trade</div>';
      return;
    }

    const items = [
      { label: 'WIN RATE', value: formatPercent(analytics.win_rate), cls: metricClass(analytics.win_rate, 55, 45) },
      { label: 'NET P&L', value: formatMaybePnl(analytics.net_pnl), cls: numberOrZero(analytics.net_pnl) >= 0 ? 'positive' : 'negative' },
      { label: 'PROFIT FACTOR', value: formatMultiple(analytics.profit_factor), cls: metricClass(analytics.profit_factor, 1.5, 1.0) },
      { label: 'SHARPE RATIO', value: formatPlainNumber(analytics.sharpe_ratio ?? analytics.sharpe), cls: metricClass(analytics.sharpe_ratio ?? analytics.sharpe, 1.5, 0.5) },
      { label: 'MAX DRAWDOWN', value: formatMaybePnl(analytics.max_drawdown), cls: 'negative' },
      { label: 'REWARD / RISK', value: formatMultiple(analytics.reward_risk), cls: metricClass(analytics.reward_risk, 1.5, 1.0) },
      { label: 'EV / TRADE', value: formatMaybePnl(analytics.ev_per_trade), cls: numberOrZero(analytics.ev_per_trade) >= 0 ? 'positive' : 'negative' },
      { label: 'KELLY FRACTION', value: formatPercent(analytics.half_kelly_pct), cls: 'neutral' },
      { label: 'CALMAR', value: formatPlainNumber(analytics.calmar_ratio), cls: metricClass(analytics.calmar_ratio, 1, 0.5) },
      { label: 'SORTINO', value: formatPlainNumber(analytics.sortino_ratio ?? analytics.sortino), cls: metricClass(analytics.sortino_ratio ?? analytics.sortino, 2, 1) },
      { label: 'CAPITAL MIN', value: formatCapitalK(analytics.capital_min), cls: 'neutral' },
      { label: 'CAPITAL REC.', value: formatCapitalK(analytics.capital_recommended), cls: 'neutral' },
    ];

    grid.innerHTML = items.map((item) => `
      <div class="analytics-item">
        <div class="analytics-label">${item.label}</div>
        <div class="analytics-value ${item.cls}">${item.value}</div>
      </div>
    `).join('');
  } catch {
    grid.innerHTML = '<div class="analytics-placeholder">Error loading analytics</div>';
  }
}

function numberOrZero(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function formatPercent(value) {
  const n = Number(value);
  return Number.isFinite(n) ? `${n}%` : '—';
}

function formatMultiple(value) {
  const n = Number(value);
  return Number.isFinite(n) ? `${n}x` : '—';
}

function formatPlainNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? String(n) : '—';
}

function formatCapitalK(value) {
  const n = Number(value);
  return Number.isFinite(n) ? `₹${(n / 1000).toFixed(0)}K` : '—';
}

function formatMaybePnl(value) {
  const n = Number(value);
  return Number.isFinite(n) ? fmtPnl(n) : '—';
}

function metricClass(value, good, neutral) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 'neutral';
  if (n >= good) return 'positive';
  if (n >= neutral) return 'neutral';
  return 'negative';
}

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
  document.getElementById('btn-live').className = mode === 'LIVE' ? 'btn btn--active' : 'btn';
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
  showAlert(
    res.status === 'flattened'
      ? `Flattened ${res.symbol || 'position'}`
      : (res.message || res.status || 'Flatten request sent'),
    'info',
  );
  setTimeout(poll, 500);
}

function setOffline() {
  document.getElementById('status-pill').className = 'pill pill--offline';
  document.getElementById('status-text').textContent = 'OFFLINE';
}

function updateFooter(data) {
  document.getElementById('footer-mode').textContent =
    `MODE: ${data.trading_mode || 'PAPER'} · BOT: ${data.bot_running ? 'RUNNING' : 'STOPPED'}`;
}

async function apiPost(endpoint, body = {}) {
  try {
    const res = await fetch(`${API}${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return await res.json();
  } catch {
    return {};
  }
}

function showAlert(msg, type = 'info') {
  const el = document.getElementById('alert-banner');
  el.textContent = msg;
  el.className = `alert-banner ${type}`;
  setTimeout(() => {
    el.className = 'alert-banner hidden';
  }, 3200);
}

function fmtNum(n) {
  return Number(n).toLocaleString('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function fmtPnl(n) {
  const value = Number(n || 0);
  return `${value >= 0 ? '+' : '-'}₹${Math.abs(value).toLocaleString('en-IN', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}