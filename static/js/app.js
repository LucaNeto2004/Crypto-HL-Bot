/* ═══════════════════════════════════════════════════════════
   HyperLiquid Commodities — Mission Control
   ═══════════════════════════════════════════════════════════ */

const $ = id => document.getElementById(id);

// ── Event delegation for row highlighting (survives DOM rebuilds) ──
document.addEventListener('click', (e) => {
  if (e.target.closest('.close-pos-btn')) return; // don't highlight on close button
  const row = e.target.closest('.pos-row, .trade-row');
  if (!row) return;

  // Clear previous highlights
  document.querySelectorAll('.highlight-row').forEach(el => el.classList.remove('highlight-row'));

  const sym = row.dataset.symbol;
  row.classList.add('highlight-row');

  if (row.classList.contains('pos-row')) {
    // Clicked a position → find matching trade
    const tradeRows = $('tradesTable').querySelectorAll('tr');
    for (const tr of tradeRows) {
      const cells = tr.querySelectorAll('td');
      if (cells.length >= 2 && cells[1].textContent.trim() === sym) {
        tr.classList.add('highlight-row');
        tr.scrollIntoView({ behavior: 'smooth', block: 'center' });
        break;
      }
    }
  } else {
    // Clicked a trade → find its counterpart (open↔close pair) for the same symbol
    const isClose = row.dataset.close === '1';
    const allSym = Array.from($('tradesTable').querySelectorAll(`.trade-row[data-symbol="${sym}"]`));
    const clickedIdx = allSym.indexOf(row);

    // Find nearest counterpart: if clicked close, find nearest open before it (and vice versa)
    let pair = null;
    if (isClose) {
      // Search backward for the nearest open
      for (let i = clickedIdx + 1; i < allSym.length; i++) {
        if (allSym[i].dataset.close === '0') { pair = allSym[i]; break; }
      }
    } else {
      // Search forward for the nearest close
      for (let i = clickedIdx - 1; i >= 0; i--) {
        if (allSym[i].dataset.close === '1') { pair = allSym[i]; break; }
      }
    }
    if (pair) pair.classList.add('highlight-row');

    // Also highlight matching open position if exists
    const posRow = document.querySelector(`.pos-row[data-symbol="${sym}"]`);
    if (posRow) {
      posRow.classList.add('highlight-row');
      posRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }

  setTimeout(() => {
    document.querySelectorAll('.highlight-row').forEach(el => el.classList.remove('highlight-row'));
  }, 5000);
});

function sideClass(s) {
  if (!s) return '';
  if (s.includes('close')) return 'side-close';
  if (s.includes('long')) return 'side-long';
  if (s.includes('short')) return 'side-short';
  return '';
}

function pnlClass(v) { return v > 0 ? 'positive' : v < 0 ? 'negative' : ''; }

function fmt(v, d = 2) {
  if (v == null) return '\u2014';
  return '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
}

function rsiColor(v) {
  if (v > 70) return 'var(--red)';
  if (v < 30) return 'var(--green)';
  return 'var(--text)';
}

function trendArrow(t) {
  if (t === 1) return '<span class="trend-up">\u25B2</span>';
  if (t === -1) return '<span class="trend-down">\u25BC</span>';
  return '<span class="trend-flat">\u2014</span>';
}

function regimeClass(r) {
  if (!r) return 'regime-transitional';
  if (r.includes('_up')) return 'regime-up';
  if (r.includes('_down')) return 'regime-down';
  if (r.startsWith('trending')) return 'regime-trending';
  if (r.startsWith('ranging')) return 'regime-ranging';
  return 'regime-transitional';
}

function regimeLabel(r) {
  if (!r) return '\u2014';
  return r.replace('_', ' ');
}

function toast(msg, type = 'info') {
  const c = $('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3500);
}

async function closePosition(posId) {
  const sym = posId.split('#')[0];
  const label = posId.includes('#') ? `${sym} entry #${posId.split('#')[1]}` : sym;
  if (!confirm(`Close ${label} position?`)) return;
  try {
    const r = await fetch('/api/close_position', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pos_id: posId }),
    });
    const d = await r.json();
    if (d.ok) {
      toast(`Closed ${sym}: ${d.pnl >= 0 ? '+' : ''}$${d.pnl.toFixed(2)}`, d.pnl >= 0 ? 'success' : 'error');
    } else {
      toast(d.error || 'Failed to close', 'error');
    }
  } catch (e) {
    toast('Failed to close position', 'error');
  }
}

// ── Crosshair Plugin ──────────────────────────────────
const crosshairPlugin = {
  id: 'crosshair',
  afterDraw(chart) {
    const active = chart.tooltip?.getActiveElements();
    if (!active || !active.length) return;
    const { ctx, chartArea } = chart;
    const x = active[0].element.x;
    ctx.save();
    ctx.beginPath();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = 'rgba(6,182,212,0.25)';
    ctx.lineWidth = 1;
    ctx.moveTo(x, chartArea.top);
    ctx.lineTo(x, chartArea.bottom);
    ctx.stroke();
    ctx.restore();
  }
};

// ── Zero-line Plugin (draws horizontal zero line) ────
const zeroLinePlugin = {
  id: 'zeroLine',
  afterDraw(chart) {
    const yScale = chart.scales.y;
    if (!yScale) return;
    const zero = yScale.getPixelForValue(0);
    const { ctx, chartArea } = chart;
    if (zero < chartArea.top || zero > chartArea.bottom) return;
    ctx.save();
    ctx.beginPath();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = 'rgba(107,119,133,0.3)';
    ctx.lineWidth = 1;
    ctx.moveTo(chartArea.left, zero);
    ctx.lineTo(chartArea.right, zero);
    ctx.stroke();
    ctx.restore();
  }
};

// ── Segment coloring helper (green above 0, red below) ──
function pnlSegmentColor(positive, negative) {
  return function(ctx) {
    const mid = (ctx.p0.parsed.y + ctx.p1.parsed.y) / 2;
    return mid >= 0 ? positive : negative;
  };
}
function pnlPointColor(ctx) {
  const v = ctx.parsed && ctx.parsed.y;
  return v != null && v < 0 ? '#ef4444' : '#10b981';
}

// ── P&L Charts ───────────────────────────────────────
let dailyChart = null;
let totalChart = null;
const dailyHistory = [];
const totalHistory = [];
let chartsSeeded = false;
let lastChartPush = 0;

function makePnlChartConfig(tooltipRefFn) {
  return {
    type: 'line',
    plugins: [crosshairPlugin, zeroLinePlugin],
    data: {
      labels: [],
      datasets: [{
        data: [],
        segment: {
          borderColor: pnlSegmentColor('#10b981', '#ef4444'),
        },
        borderColor: '#10b981',
        backgroundColor: 'transparent',
        fill: {
          target: 'origin',
          above: 'rgba(16,185,129,0.15)',
          below: 'rgba(239,68,68,0.15)',
        },
        cubicInterpolationMode: 'monotone',
        tension: 0.5,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHoverBackgroundColor: '#e2e8f0',
        pointHoverBorderColor: '#1a1f2b',
        pointHoverBorderWidth: 2,
        borderWidth: 1.5,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 800, easing: 'easeInOutQuart' },
      transitions: { active: { animation: { duration: 200 } } },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          enabled: true,
          backgroundColor: 'rgba(12,16,24,0.96)',
          borderColor: 'rgba(6,182,212,0.2)',
          borderWidth: 1,
          titleFont: { family: "'SF Mono', monospace", size: 10, weight: '400' },
          bodyFont: { family: "'SF Mono', monospace", size: 12, weight: '600' },
          footerFont: { family: "'SF Mono', monospace", size: 10, weight: '400' },
          titleColor: '#5a6b80',
          bodyColor: '#e2e8f0',
          footerColor: '#5a6b80',
          padding: { top: 8, bottom: 8, left: 12, right: 12 },
          cornerRadius: 4,
          displayColors: false,
          caretSize: 0,
          callbacks: {
            title: items => items.length ? items[0].label : '',
            label: function(item) {
              const v = item.parsed.y;
              const s = v >= 0 ? '+$' : '-$';
              return s + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            },
            footer: function(items) {
              return tooltipRefFn(items);
            }
          }
        }
      },
      scales: {
        x: {
          display: true,
          grid: { display: false },
          ticks: { color: '#3a4a5c', maxTicksLimit: 6, font: { size: 9, family: "'SF Mono', monospace" } },
          border: { display: false },
        },
        y: {
          display: true,
          position: 'right',
          grid: { color: 'rgba(26,35,50,0.3)', drawBorder: false },
          ticks: {
            color: '#3a4a5c',
            font: { size: 9, family: "'SF Mono', monospace" },
            callback: v => (v >= 0 ? '+$' : '-$') + Math.abs(v).toLocaleString(),
            maxTicksLimit: 5,
          },
          border: { display: false },
        }
      },
    }
  };
}

function initPnlCharts() {
  const dailyCtx = $('dailyPnlChart');
  const totalCtx = $('totalEquityChart');
  if (!dailyCtx || !totalCtx) return;

  dailyChart = new Chart(dailyCtx, makePnlChartConfig(function(items) {
    if (!items.length) return '';
    const startBal = _startingBalance || 1;
    const pct = (items[0].parsed.y / startBal * 100);
    return (pct >= 0 ? '+' : '') + pct.toFixed(2) + '% of balance';
  }));

  const equityConfig = makePnlChartConfig(function(items) {
    if (!items.length) return '';
    const startBal = _startingBalance || 1;
    const pct = (items[0].parsed.y / startBal * 100);
    return (pct >= 0 ? '+' : '') + pct.toFixed(2) + '% all-time';
  });
  // Equity chart: small clean dots, one per day
  const ds = equityConfig.data.datasets[0];
  ds.pointRadius = 3;
  ds.pointBorderWidth = 0;
  ds.pointBackgroundColor = pnlPointColor;
  ds.pointHoverRadius = 5;
  totalChart = new Chart(totalCtx, equityConfig);
}

let _startingBalance = 0;

function rebuildDailyChart(trades, unrealizedPnl) {
  // Rebuild daily P&L chart from trades — one point per trade event
  if (!dailyChart) return;
  const todayStr = new Date().toISOString().slice(0, 10);
  const todayTrades = trades.filter(t => t.time.startsWith(todayStr));
  if (todayTrades.length === 0 && !chartsSeeded) return;
  chartsSeeded = true;

  const rawPoints = [{ time: '00:00', pnl: 0 }];
  let cum = 0;
  for (const t of todayTrades) {
    cum = Math.round((cum + (t.pnl || 0)) * 100) / 100;
    const label = t.time.includes(' ') ? t.time.split(' ')[1].slice(0, 5) : t.time;
    rawPoints.push({ time: label, pnl: cum });
  }
  // Add current unrealized as "now" point
  const nowLabel = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
  const livePnl = Math.round((cum + unrealizedPnl) * 100) / 100;
  rawPoints.push({ time: nowLabel, pnl: livePnl });

  // Insert zero-crossing points
  dailyHistory.length = 0;
  for (let i = 0; i < rawPoints.length; i++) {
    if (i > 0) {
      const prev = rawPoints[i - 1].pnl;
      const curr = rawPoints[i].pnl;
      if ((prev < 0 && curr > 0) || (prev > 0 && curr < 0)) {
        dailyHistory.push({ time: '', pnl: 0, hidden: true });
      }
    }
    dailyHistory.push(rawPoints[i]);
  }
  dailyChart.data.datasets[0].pointRadius = 0;
  dailyChart.data.datasets[0].pointHoverRadius = 4;
  dailyChart.data.datasets[0].pointHoverBackgroundColor = dailyHistory.map(d => d.pnl >= 0 ? '#10b981' : '#ef4444');
  dailyChart.data.datasets[0].pointHoverBorderWidth = 0;
  pushChartData(dailyChart, dailyHistory, 'dailyLegendDot');
}

function pushChartData(chart, history, legendDotId) {
  if (!chart) return;
  chart.data.labels = history.map(d => d.time);
  chart.data.datasets[0].data = history.map(d => d.pnl);

  const latest = history.length > 0 ? history[history.length - 1].pnl : 0;
  const dotColor = latest > 0.01 ? '#10b981' : latest < -0.01 ? '#ef4444' : '#6b7785';
  const dot = document.getElementById(legendDotId);
  if (dot) dot.style.background = dotColor;

  chart.update();
}

function updateGoals(capital) {
  if (!capital || capital <= 0) return;
  const g = (lo, hi) => `$${(capital * lo).toFixed(2)}–$${(capital * hi).toFixed(2)}`;
  const g1 = (pct) => `$${(capital * pct).toFixed(0)}`;
  const gp = (pct) => `$${(capital * pct).toFixed(0)}+`;
  $('goalsCapital').textContent = `$${capital.toFixed(0)}`;
  // Moderate
  $('gModD').textContent = g(0.003, 0.005);
  $('gModW').textContent = g(0.015, 0.025);
  $('gModM').textContent = g(0.06, 0.10);
  $('gModY').textContent = '~' + g1(1.0);
  // Aggressive
  $('gAggD').textContent = g(0.005, 0.01);
  $('gAggW').textContent = g(0.025, 0.05);
  $('gAggM').textContent = g(0.10, 0.20);
  $('gAggY').textContent = '~' + gp(2.0);
}

function rebuildEquityChart(trades, unrealizedPnl) {
  // Rebuild day-by-day equity from ALL trades every refresh
  if (!totalChart) return;
  const dayPnl = {};
  const pnlTrades = trades.filter(t => t.pnl != null && t.pnl !== 0);
  for (const t of pnlTrades) {
    const day = t.time.slice(0, 10);
    dayPnl[day] = (dayPnl[day] || 0) + t.pnl;
  }
  const sortedDays = Object.keys(dayPnl).sort();
  // Build raw day points
  const rawPoints = [];
  let cum = 0;
  for (const day of sortedDays) {
    cum = Math.round((cum + dayPnl[day]) * 100) / 100;
    const d = new Date(day + 'T12:00:00');
    const label = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.getDay()] + ' ' + day.slice(5);
    rawPoints.push({ time: label, pnl: cum });
  }
  // Always show a "today" dot with unrealized P&L
  const todayStr = new Date().toISOString().slice(0, 10);
  const todayD = new Date(todayStr + 'T12:00:00');
  const todayLabel = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][todayD.getDay()] + ' ' + todayStr.slice(5);
  const lastDay = sortedDays.length > 0 ? sortedDays[sortedDays.length - 1] : null;
  if (lastDay === todayStr) {
    // Today already has closed trades — add unrealized on top
    rawPoints[rawPoints.length - 1].pnl = Math.round((rawPoints[rawPoints.length - 1].pnl + unrealizedPnl) * 100) / 100;
  } else {
    // No closed trades today — add a new dot with cumulative + unrealized
    const livePnl = Math.round((cum + unrealizedPnl) * 100) / 100;
    rawPoints.push({ time: todayLabel, pnl: livePnl });
  }
  // Insert zero-crossing points so segments split cleanly at the zero line
  totalHistory.length = 0;
  for (let i = 0; i < rawPoints.length; i++) {
    if (i > 0) {
      const prev = rawPoints[i - 1].pnl;
      const curr = rawPoints[i].pnl;
      if ((prev < 0 && curr > 0) || (prev > 0 && curr < 0)) {
        totalHistory.push({ time: '', pnl: 0, hidden: true });
      }
    }
    totalHistory.push(rawPoints[i]);
  }
  // Set per-point radius and colors
  totalChart.data.datasets[0].pointRadius = totalHistory.map(d => d.hidden ? 0 : 3);
  totalChart.data.datasets[0].pointBackgroundColor = totalHistory.map(d => d.hidden ? 'transparent' : d.pnl >= 0 ? '#10b981' : '#ef4444');
  totalChart.data.datasets[0].pointBorderWidth = 0;
  totalChart.data.datasets[0].pointStyle = 'circle';
  pushChartData(totalChart, totalHistory, 'totalLegendDot');
}

function updatePnlCharts(balance, startingBalance, trades, unrealizedPnl) {
  if (!dailyChart || !totalChart) return;
  _startingBalance = startingBalance || balance;

  // Daily chart: one point per trade + live unrealized
  rebuildDailyChart(trades, unrealizedPnl);

  // Equity chart: one point per day
  rebuildEquityChart(trades, unrealizedPnl);
}

// ── Main Refresh ─────────────────────────────────────
async function refresh() {
  try {
    const r = await fetch('/api/state');
    const s = await r.json();

    // Header
    $('botStatus').textContent = s.bot_status.toUpperCase();
    $('statusDot').className = 'status-dot ' + s.bot_status;
    $('cycle').textContent = s.cycle;
    $('lastCycle').textContent = s.last_cycle_time || '--:--:--';
    $('modeBadge').textContent = s.mode;
    $('modeBadge').className = 'badge badge-' + s.mode.toLowerCase();
    $('netBadge').textContent = s.network;
    $('netBadge').className = 'badge badge-' + s.network.toLowerCase();

    // Banners
    $('killBanner').style.display = s.kill_switch ? 'block' : 'none';
    const ddHalt = s.account_dd_halt || false;
    $('ddHaltBanner').style.display = ddHalt ? 'block' : 'none';
    if (ddHalt && s.account_peak_balance > 0) {
      const ddPct = ((s.account_peak_balance - s.balance) / s.account_peak_balance * 100).toFixed(1);
      $('ddPct').textContent = ddPct;
      $('ddPeak').textContent = s.account_peak_balance.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }
    const lossHalt = s.consecutive_loss_halt || false;
    $('lossHaltBanner').style.display = lossHalt ? 'block' : 'none';
    if (lossHalt) $('lossCount').textContent = s.consecutive_losses || 0;
    $('lossStreak').textContent = s.consecutive_losses || 0;

    // KPIs
    $('balance').textContent = fmt(s.balance);
    $('dailyPnl').textContent = fmt(s.daily_pnl);
    $('dailyPnl').className = 'kpi-value ' + pnlClass(s.daily_pnl);

    const startBal = s.starting_balance || s.balance;
    if (startBal > 0) {
      const pct = (s.daily_pnl / startBal * 100);
      $('dailyPnlPct').textContent = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
      $('dailyPnlPct').className = 'kpi-sub ' + pnlClass(pct);
    }

    const trades = s.trade_history || [];
    // Build pairs first — used for all trade counting
    const _openE = {};
    const _pairs = [];
    for (const t of trades) {
      const isClose = t.side && t.side.includes('close');
      const sym = t.symbol;
      if (!isClose) {
        if (!_openE[sym]) _openE[sym] = [];
        _openE[sym].push(t);
      } else {
        const pending = _openE[sym] || [];
        if (pending.length > 0) {
          // FIFO: one entry per exit for pyramid tracking
          const entry = pending.shift();
          _pairs.push({ entry, exit: t });
          if (pending.length === 0) delete _openE[sym];
        } else {
          _pairs.push({ entry: null, exit: t });
        }
      }
    }
    for (const sym in _openE) for (const e of _openE[sym]) _pairs.push({ entry: e, exit: null });

    const closedPairs = _pairs.filter(p => p.exit);
    let won = 0, lost = 0;
    closedPairs.forEach(p => { p.exit.pnl > 0 ? won++ : lost++; });
    $('tradesWon').textContent = won;
    $('tradesLost').textContent = lost;
    $('totalTrades').textContent = closedPairs.length;

    // Daily trades (today only)
    const today = new Date().toISOString().slice(0, 10);
    let dWon = 0, dLost = 0;
    closedPairs.forEach(p => {
      if (p.exit.time && p.exit.time.startsWith(today)) {
        p.exit.pnl > 0 ? dWon++ : dLost++;
      }
    });
    $('dailyTrades').textContent = dWon + dLost;
    $('dailyTradesWon').textContent = dWon;
    $('dailyTradesLost').textContent = dLost;
    const dTotal = dWon + dLost;
    $('dailyWinRate').textContent = dTotal > 0 ? (dWon / dTotal * 100).toFixed(0) + '%' : '\u2014';

    const posKeys = Object.keys(s.positions || {});
    $('openPosCount').textContent = posKeys.length;

    const realizedPnl = trades.reduce((a, t) => a + (t.pnl || 0), 0);
    const totalUnrealized = posKeys.reduce((a, k) => a + (s.positions[k].unrealized_pnl || 0), 0);
    const sessionPnl = realizedPnl + totalUnrealized;
    $('sessionPnl').textContent = fmt(sessionPnl);
    $('sessionPnl').className = 'kpi-value ' + pnlClass(sessionPnl);
    const startingBal = s.starting_balance || (s.balance - realizedPnl);
    if (startingBal > 0) {
      const totalPct = (sessionPnl / startingBal * 100);
      $('sessionPnlPct').textContent = (totalPct >= 0 ? '+' : '') + totalPct.toFixed(2) + '%';
      $('sessionPnlPct').style.color = totalPct > 0 ? 'var(--green)' : totalPct < 0 ? 'var(--red)' : 'var(--muted)';
    }
    updateGoals(startingBal);

    // Win rate
    const totalClosed = won + lost;
    $('winRate').textContent = totalClosed > 0 ? (won / totalClosed * 100).toFixed(0) + '%' : '\u2014';

    // P&L Charts — include unrealized PnL from open positions
    let unrealizedPnl = 0;
    for (const pos of Object.values(s.positions || {})) {
      unrealizedPnl += (pos.unrealized_pnl || 0);
    }
    updatePnlCharts(s.balance, s.starting_balance, trades, unrealizedPnl);

    // Instruments
    let instHtml = '';
    for (const [sym, info] of Object.entries(s.instruments || {})) {
      const price = (s.prices || {})[sym];
      const ind = (s.indicators || {})[sym] || {};
      const pos = (s.positions || {})[sym];
      const posTag = pos
        ? `<span class="pos-tag ${sideClass(pos.side)}">${pos.side.toUpperCase()}</span>`
        : '';

      instHtml += `
        <div class="inst-card${pos ? ' has-position' : ''}">
          <div class="inst-header">
            <div>
              <div class="inst-name-row"><span class="inst-name">${info.name}</span>${posTag}</div>
              <div class="inst-symbol">${sym}</div>
            </div>
            <div class="inst-price">${price != null ? fmt(price) : '\u2014'}</div>
          </div>
          <div class="inst-divider"></div>
          <div class="ind-grid">
            <div class="ind-item"><span class="ind-label">RSI</span><span class="ind-val" style="color:${rsiColor(ind.rsi)}">${ind.rsi ?? '\u2014'}</span></div>
            <div class="ind-item"><span class="ind-label">ADX</span><span class="ind-val">${ind.adx ?? '\u2014'}</span></div>
            <div class="ind-item"><span class="ind-label">MACD</span><span class="ind-val">${ind.macd_hist ?? '\u2014'}</span></div>
            <div class="ind-item"><span class="ind-label">ATR%</span><span class="ind-val">${ind.atr_pct ?? '\u2014'}%</span></div>
            <div class="ind-item full-width"><span class="ind-label">Regime</span><span class="regime-badge ${regimeClass(ind.regime)}">${regimeLabel(ind.regime)}</span> ${trendArrow(ind.trend)}</div>
          </div>
        </div>`;
    }
    $('instruments').innerHTML = instHtml;

    // Positions (supports pyramiding — multiple entries per symbol)
    const posTable = $('positionsTable');
    if (posKeys.length === 0) {
      posTable.innerHTML = '<tr class="empty-row"><td colspan="10">No open positions</td></tr>';
    } else {
      posTable.innerHTML = posKeys.map(posId => {
        const p = s.positions[posId];
        const sym = p.symbol || posId.split('#')[0];
        const entryNum = posId.includes('#') ? posId.split('#')[1] : '1';
        const pp = p.pnl_pct || 0;
        const ppStr = (pp >= 0 ? '+' : '') + pp.toFixed(2) + '%';
        const pnl = p.unrealized_pnl;
        const pStr = pnl != null ? ((pnl >= 0 ? '+' : '') + fmt(pnl)) : '\u2014';
        return `<tr class="pos-row" data-symbol="${sym}" data-posid="${posId}" style="cursor:pointer" title="Click to view entry trade">
          <td><strong>${sym}</strong> <span style="color:var(--muted);font-size:0.75em">#${entryNum}</span></td>
          <td class="${sideClass(p.side)}">${p.side.toUpperCase()}</td>
          <td>${fmt(p.size_usd)}</td>
          <td>${fmt(p.entry_price)}</td>
          <td>${p.current_price != null ? fmt(p.current_price) : '\u2014'}</td>
          <td class="sl-cell">${p.stop_loss != null ? fmt(p.stop_loss) : '\u2014'}</td>
          <td class="tp-cell">${p.take_profit != null ? fmt(p.take_profit) : '\u2014'}</td>
          <td class="${pnlClass(pnl)}">${pStr}</td>
          <td class="${pnlClass(pp)}"><strong>${ppStr}</strong></td>
          <td><button class="close-pos-btn" data-posid="${posId}" onclick="closePosition('${posId}')">Close</button></td>
        </tr>`;
      }).join('');
    }

    // Trades — TV-style paired Entry/Exit rows (reuse _pairs from above)
    const trTable = $('tradesTable');
    // Filter out orphaned exits (no entry) — these are duplicate close records
    // from combined pyramid exits, already accounted for in proportional split
    const pairs = _pairs.filter(p => p.entry || !p.exit);
    const openCount = pairs.filter(p => !p.exit).length;
    const displayedClosed = pairs.filter(p => p.exit).length;
    $('tradeCount').textContent = displayedClosed + ' closed' + (openCount > 0 ? ', ' + openCount + ' open' : '');

    if (pairs.length === 0) {
      trTable.innerHTML = '<tr class="empty-row"><td colspan="8">No trades yet</td></tr>';
    } else {
      // Pre-compute cumulative P&L using ALL exits (including filtered orphans)
      let cumPnl = 0;
      const cumPnlMap = new Map();
      // Use _pairs (unfiltered) to accumulate PnL, map to displayed pairs
      for (const p of _pairs) {
        if (p.exit) {
          cumPnl += (p.exit.pnl || 0);
          if (p.entry) {  // Only map displayed pairs (with entries)
            cumPnlMap.set(p, Math.round(cumPnl * 100) / 100);
          }
        }
      }
      trTable.innerHTML = pairs.slice().reverse().map((p, idx) => {
        const tradeNum = pairs.length - idx;
        const entry = p.entry;
        const exit = p.exit;
        const isBuy = entry && (entry.side.includes('long') || entry.side.toLowerCase().includes('buy'));
        const sideLabel = isBuy ? 'Long' : 'Short';
        const sideColor = isBuy ? '#10b981' : '#ef4444';
        const sizeUsd = entry ? (entry.size * entry.price) : 0;
        const pnl = exit ? exit.pnl : null;
        const pnlPct = (pnl != null && sizeUsd > 0) ? ((pnl / sizeUsd) * 100).toFixed(2) : null;
        const cumPnlVal = cumPnlMap.has(p) ? cumPnlMap.get(p) : null;
        const exitSignal = exit
          ? exit.exit_reason === 'stop_loss' ? sideLabel + ' Trail'
          : exit.exit_reason === 'take_profit' ? 'Take Profit'
          : exit.exit_reason && exit.exit_reason.startsWith('TV webhook:') ? exit.exit_reason.replace('TV webhook: ', '').replace('_', ' ').replace(/\b\w/g, c => c.toUpperCase())
          : exit.exit_reason === 'signal' ? 'Signal'
          : exit.exit_reason || 'Close'
          : 'Open';

        const exitRow = `<tr class="trade-row tv-exit" data-symbol="${(exit||entry).symbol}">
          <td rowspan="2" style="border-right:1px solid #333;vertical-align:middle">
            <div><span style="color:#888">${tradeNum}</span>
            <span style="color:${sideColor};margin-left:4px;font-weight:600">${sideLabel}</span></div>
            <div style="color:#aaa;font-size:0.85em">${(entry||exit).symbol.replace('xyz:','')}</div>
          </td>
          <td style="color:#aaa">Exit</td>
          <td>${exit ? exit.time : '\u2014'}</td>
          <td>${exitSignal}</td>
          <td>${exit ? fmt(exit.price) : '\u2014'}</td>
          <td rowspan="2" style="vertical-align:middle;text-align:right">
            <div>${entry ? entry.size.toFixed(4) : '\u2014'}</div>
            <div style="color:#888;font-size:0.85em">$${sizeUsd.toFixed(0)}</div>
          </td>
          <td rowspan="2" style="vertical-align:middle;text-align:right" class="${pnlClass(pnl)}">
            <div>${pnl != null ? (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2) : '\u2014'}</div>
            <div style="font-size:0.85em">${pnlPct != null ? (pnl >= 0 ? '+' : '') + pnlPct + '%' : ''}</div>
          </td>
          <td rowspan="2" style="vertical-align:middle;text-align:right;color:#888">
            ${cumPnlVal != null ? (cumPnlVal >= 0 ? '+' : '') + '$' + cumPnlVal.toFixed(2) : '\u2014'}
          </td>
        </tr>`;

        const entryRow = `<tr class="trade-row tv-entry" data-symbol="${(entry||exit).symbol}" style="border-bottom:1px solid #333">
          <td style="color:#aaa">Entry</td>
          <td>${entry ? entry.time : '\u2014'}</td>
          <td>${sideLabel}</td>
          <td>${entry ? fmt(entry.price) : '\u2014'}</td>
        </tr>`;

        return exitRow + entryRow;
      }).join('');
    }

    // Risk config
    const rc = s.risk_config || {};
    $('riskGauges').innerHTML = Object.entries(rc).map(([k, v]) => `
      <div class="risk-item">
        <div class="risk-label">${k.replace(/_/g, ' ')}</div>
        <div class="risk-value">${v}</div>
      </div>
    `).join('');

    // Correlation Matrix
    renderCorrelationMatrix(s.correlations || {}, Object.keys(s.instruments || {}));

    // Performance Attribution (fetch separately, less frequent)
    refreshAttribution();

  } catch (e) {
    console.error('Refresh failed', e);
  }
}

let lastAttrRefresh = 0;
let attrFilter = 'all';

function setAttrFilter(filter) {
  attrFilter = filter;
  $('attrAllTime').className = 'attr-toggle' + (filter === 'all' ? ' active' : '');
  $('attrToday').className = 'attr-toggle' + (filter === 'today' ? ' active' : '');
  lastAttrRefresh = 0;
  refreshAttribution();
}

async function refreshAttribution() {
  const now = Date.now();
  if (now - lastAttrRefresh < 10000) return;
  lastAttrRefresh = now;

  try {
    const r = await fetch('/api/attribution?date=' + attrFilter);
    const a = await r.json();

    $('attrTotal').textContent = fmt(a.total_pnl);
    $('attrTotal').className = 'card-header-count ' + pnlClass(a.total_pnl);

    const strats = Object.entries(a.by_strategy || {});
    if (strats.length) {
      $('attrStrategy').innerHTML = strats
        .sort((a, b) => b[1].pnl - a[1].pnl)
        .map(([name, d]) => {
          const pfClass = d.profit_factor > 1.5 ? 'pf-good' : d.profit_factor < 1 ? 'pf-bad' : 'pf-neutral';
          const pfVal = d.profit_factor >= 999 ? 100 : d.profit_factor;
          return `<tr>
            <td><strong>${name}</strong></td>
            <td class="${pnlClass(d.pnl)}">${fmt(d.pnl)}</td>
            <td>${d.trades}</td>
            <td>${d.win_rate}%</td>
            <td class="${pfClass}">${pfVal}</td>
            <td class="positive">${fmt(d.avg_win)}</td>
            <td class="negative">${fmt(d.avg_loss)}</td>
          </tr>`;
        }).join('');
    }

    const syms = Object.entries(a.by_symbol || {});
    if (syms.length) {
      $('attrSymbol').innerHTML = syms
        .sort((a, b) => b[1].pnl - a[1].pnl)
        .map(([name, d]) => `<tr>
          <td><strong>${name}</strong></td>
          <td class="${pnlClass(d.pnl)}">${fmt(d.pnl)}</td>
          <td>${d.trades}</td>
          <td>${d.win_rate}%</td>
        </tr>`).join('');
    }

    const exits = Object.entries(a.by_exit_reason || {});
    if (exits.length) {
      $('attrExit').innerHTML = exits
        .sort((a, b) => b[1].count - a[1].count)
        .map(([name, d]) => `<tr>
          <td><strong>${name}</strong></td>
          <td class="${pnlClass(d.pnl)}">${fmt(d.pnl)}</td>
          <td>${d.count}</td>
        </tr>`).join('');
    }

    const days = Object.entries(a.by_day || {});
    if (days.length) {
      $('attrDay').innerHTML = days.reverse().map(([date, d]) => `<tr>
        <td>${date}</td>
        <td class="${pnlClass(d.pnl)}">${fmt(d.pnl)}</td>
        <td>${d.trades}</td>
        <td>${d.won}W / ${d.lost}L</td>
      </tr>`).join('');
    }

  } catch (e) {
    console.error('Attribution fetch failed', e);
  }
}

// ── Correlation Matrix ───────────────────────────────
function corrColor(v) {
  if (v > 0.7) return { bg: 'rgba(239,68,68,0.25)', border: 'rgba(239,68,68,0.3)', color: '#ef4444' };
  if (v > 0.4) return { bg: 'rgba(245,158,11,0.15)', border: 'rgba(245,158,11,0.2)', color: '#f59e0b' };
  if (v > 0.0) return { bg: 'rgba(107,119,133,0.08)', border: 'rgba(107,119,133,0.15)', color: 'var(--text-dim)' };
  if (v > -0.4) return { bg: 'rgba(107,119,133,0.08)', border: 'rgba(107,119,133,0.15)', color: 'var(--text-dim)' };
  return { bg: 'rgba(16,185,129,0.15)', border: 'rgba(16,185,129,0.2)', color: '#10b981' };
}

function shortName(sym) {
  return sym.replace('xyz:', '');
}

function renderCorrelationMatrix(correlations, symbols) {
  const el = $('corrMatrix');
  if (!symbols || symbols.length < 2 || Object.keys(correlations).length === 0) {
    el.innerHTML = '<span style="color:var(--muted);font-size:11px">Waiting for data...</span>';
    return;
  }

  const n = symbols.length;
  el.style.gridTemplateColumns = `80px repeat(${n}, 1fr)`;

  let html = '<div class="corr-header"></div>';
  for (const sym of symbols) {
    html += `<div class="corr-header">${shortName(sym)}</div>`;
  }

  for (let i = 0; i < n; i++) {
    html += `<div class="corr-header">${shortName(symbols[i])}</div>`;
    for (let j = 0; j < n; j++) {
      if (i === j) {
        html += `<div class="corr-cell" style="background:var(--cyan-bg);color:var(--cyan)">1.00</div>`;
      } else {
        const key1 = `${symbols[i]}|${symbols[j]}`;
        const key2 = `${symbols[j]}|${symbols[i]}`;
        const val = correlations[key1] ?? correlations[key2] ?? null;
        if (val !== null) {
          const c = corrColor(val);
          html += `<div class="corr-cell" style="background:${c.bg};border:1px solid ${c.border};color:${c.color}">${val.toFixed(2)}</div>`;
        } else {
          html += `<div class="corr-cell" style="color:var(--muted)">\u2014</div>`;
        }
      }
    }
  }

  el.innerHTML = html;
}

// ── Clock ────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  const t = now.toLocaleTimeString('en-US', { hour12: false });
  if ($('clock')) $('clock').textContent = t;
  if ($('footerClock')) $('footerClock').textContent = t;
}

// ── Boot ─────────────────────────────────────────────
initPnlCharts();
refresh();
updateClock();
setInterval(refresh, 1000);
setInterval(updateClock, 1000);
