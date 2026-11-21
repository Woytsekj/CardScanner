// Shared subtype-aware price-history dialog behavior for Inventory and Vending.
let historyContext = null;
let historyDays = 90;
let historySeries = [];
let inlineHistorySeries = [];
let inlineHistoryRequest = 0;

const historyDialog = document.getElementById('priceHistoryDialog');
const historyChart = document.getElementById('priceHistoryChart');
const historyStatus = document.getElementById('priceHistoryStatus');
const historyStats = document.getElementById('priceHistoryStats');
const historyTitle = document.getElementById('priceHistoryTitle');
const historySubtitle = document.getElementById('priceHistorySubtitle');
const historyMetric = document.getElementById('priceHistoryMetric');
const historyLegend = document.getElementById('priceHistoryLegend');

const PRICE_METRICS = ['market', 'low', 'mid', 'high', 'direct_low'];
const SERIES_COLORS = ['#35b7e6', '#f3c842', '#a78bfa', '#34d399', '#fb923c', '#f472b6', '#94a3b8', '#e879f9'];

function closePriceHistory() { if (historyDialog?.open) historyDialog.close(); }
function escHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  }[c]));
}
function localLabel(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {month:'short', day:'numeric'});
}
function formatMoney(v) {
  return v == null || Number.isNaN(Number(v)) ? '—' : '$' + Number(v).toFixed(2);
}
function metricLabel(metric) {
  return ({market:'Market', low:'Low', mid:'Mid', high:'High', direct_low:'Direct low'})[metric] || 'Market';
}

const expandedHistoryDialog = document.getElementById('cardDetailHistoryExpanded');
const expandedHistoryTitle = document.getElementById('cardDetailHistoryExpandedTitle');
const expandedHistoryStatus = document.getElementById('cardDetailExpandedHistoryStatus');
const expandedHistoryStats = document.getElementById('cardDetailExpandedHistoryStats');
const expandedHistoryChart = document.getElementById('cardDetailExpandedPriceHistoryChart');
const expandedHistoryLegend = document.getElementById('cardDetailExpandedHistoryLegend');
const expandedHistoryMetric = document.getElementById('cardDetailExpandedHistoryMetric');

function appendSvg(target, tag, attributes, text) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attributes || {}).forEach(([key, value]) => node.setAttribute(key, value));
  if (text != null) node.textContent = text;
  target.appendChild(node);
  return node;
}

// Change-event data is reconstructed into daily values by the API. Use a
// horizontal-then-vertical path so prices are shown as held/step values.
function seriesPath(points, metric, xFn, yFn) {
  let path = '';
  let drawing = false;
  points.forEach((point, index) => {
    const value = point[metric];
    if (!point.available || value == null || !Number.isFinite(Number(value))) {
      drawing = false;
      return;
    }
    const x = xFn(index), y = yFn(Number(value));
    if (!drawing) {
      path += `M ${x.toFixed(2)} ${y.toFixed(2)} `;
    } else {
      path += `H ${x.toFixed(2)} V ${y.toFixed(2)} `;
    }
    drawing = true;
  });
  return path.trim();
}

function renderHistoryChart(series, targetChart = historyChart, metric = 'market', targetLegend = historyLegend) {
  if (!targetChart) return;
  targetChart.innerHTML = '';
  if (targetLegend) targetLegend.innerHTML = '';
  const NS = 'http://www.w3.org/2000/svg';
  const W = 820, H = 360, left = 64, right = 18, top = 18, bottom = 48;
  const plotW = W-left-right, plotH = H-top-bottom;
  const labels = series[0]?.points || [];
  const values = series.flatMap(item => (item.points || [])
    .map(point => point[metric])
    .filter(value => value != null && Number.isFinite(Number(value)))
    .map(Number));
  if (!values.length) {
    appendSvg(targetChart, 'text', {
      x: W/2, y: H/2, 'text-anchor': 'middle', fill: '#888', 'font-size': '16'
    }, 'No price history for this metric');
    return;
  }

  let min = Math.min(...values), max = Math.max(...values);
  if (min === max) { min = Math.max(0, min - 1); max += 1; }
  const pad = (max-min)*0.08;
  min = Math.max(0, min-pad);
  max += pad;
  const count = labels.length;
  const xFn = index => left + (count <= 1 ? plotW/2 : (index/(count-1))*plotW);
  const yFn = value => top + (max-value)/(max-min)*plotH;

  for (let i=0;i<5;i++) {
    const value = min + (max-min)*(i/4);
    const y = yFn(value);
    appendSvg(targetChart, 'line', {
      x1:left, x2:W-right, y1:y, y2:y, stroke:'var(--border)', 'stroke-width':'1', opacity:'0.8'
    });
    appendSvg(targetChart, 'text', {
      x:left-8, y:y+4, 'text-anchor':'end', fill:'var(--muted)', 'font-size':'12'
    }, formatMoney(value));
  }

  const gridDates = [0, Math.floor((count-1)/2), Math.max(0,count-1)];
  [...new Set(gridDates)].forEach(index => {
    const x=xFn(index);
    appendSvg(targetChart, 'line', {
      x1:x, x2:x, y1:top, y2:H-bottom, stroke:'var(--border)', 'stroke-width':'1', opacity:'0.65'
    });
    appendSvg(targetChart, 'text', {
      x, y:H-17, 'text-anchor':'middle', fill:'var(--muted)', 'font-size':'12'
    }, labels[index] ? localLabel(labels[index].captured_at) : '');
  });

  series.forEach((item, index) => {
    const points = item.points || [];
    const color = SERIES_COLORS[index % SERIES_COLORS.length];
    const d = seriesPath(points, metric, xFn, yFn);
    if (d) {
      // A small marker with a native SVG title exposes the exact date and
      // price on hover/focus without adding a permanent tooltip over the line.
      let previousValue = null;
      points.forEach((point, pointIndex) => {
        const value = point[metric];
        if (!point.available || value == null || !Number.isFinite(Number(value))) {
          previousValue = null;
          return;
        }
        const numericValue = Number(value);
        const isLastPoint = pointIndex === points.length - 1;
        if (numericValue !== previousValue || isLastPoint) {
          const date = new Date(point.captured_at);
          const dateText = Number.isNaN(date.getTime()) ? String(point.captured_at || '') : date.toLocaleString();
          const pointTitle = `${item.subtype_name} · ${dateText} · ${metricLabel(metric)}: ${formatMoney(value)}`;
          const marker = appendSvg(targetChart, 'circle', {
            cx:xFn(pointIndex), cy:yFn(numericValue), r:'3.2', fill:color, opacity:'0.9'
          });
          appendSvg(marker, 'title', {}, pointTitle);
        }
        previousValue = numericValue;
      });
      appendSvg(targetChart, 'path', {
        d, fill:'none', stroke:color, 'stroke-width':'2.8',
        'stroke-linejoin':'round', 'stroke-linecap':'round'
      });
      const lastIndex = points.findLastIndex
        ? points.findLastIndex(point => point.available && point[metric] != null)
        : (() => {
            for (let i=points.length-1;i>=0;i--) {
              if (points[i].available && points[i][metric] != null) return i;
            }
            return -1;
          })();
      if (lastIndex >= 0) {
        appendSvg(targetChart, 'circle', {
          cx:xFn(lastIndex), cy:yFn(Number(points[lastIndex][metric])),
          r:'4', fill:color
        });
      }
    }
    if (targetLegend) {
      const label = document.createElement('span');
      label.className = 'price-history-legend-item';
      const swatch = document.createElement('i');
      swatch.style.backgroundColor = color;
      swatch.setAttribute('aria-hidden', 'true');
      label.appendChild(swatch);
      label.appendChild(document.createTextNode(item.subtype_name));
      targetLegend.appendChild(label);
    }
  });
}

function renderHistoryStats(series, targetStats, metric) {
  if (!targetStats) return;
  const label = metricLabel(metric);
  const cards = [];
  series.forEach(item => {
    const points = item.points || [];
    const available = points.filter(point => point.available && point[metric] != null);
    if (!available.length) {
      cards.push(`<div><span>${escHtml(item.subtype_name)}</span><strong>Unavailable</strong><small>No ${escHtml(label.toLowerCase())} price</small></div>`);
      return;
    }
    const first = Number(available[0][metric]);
    const last = Number(available[available.length-1][metric]);
    const delta = last-first;
    const percent = first > 0 ? (delta/first)*100 : null;
    const sign = delta >= 0 ? '+' : '';
    const changeText = percent == null ? 'No percentage baseline' :
      `${sign}${formatMoney(delta)} (${sign}${percent.toFixed(1)}%)`;
    const latest = points[points.length-1];
    const stateNote = latest && !latest.available ? ' · no longer listed by provider' : '';
    cards.push(`<div><span>${escHtml(item.subtype_name)}</span><strong>${formatMoney(last)}</strong><small>${escHtml(changeText + stateNote)}</small></div>`);
  });
  targetStats.innerHTML = cards.join('');
}

function renderHistoryData(series, targetChart, targetStats, targetLegend, metric) {
  renderHistoryStats(series, targetStats, metric);
  renderHistoryChart(series, targetChart, metric, targetLegend);
}

function refreshExpandedCardHistory() {
  if (!expandedHistoryDialog?.open) return;
  const currentName = document.getElementById('oracleTitle')?.textContent || 'Selected card';
  const currentStatus = document.getElementById('cardDetailHistoryStatus');
  const inlineMetric = document.getElementById('cardDetailHistoryMetric');
  const activeRange = document.querySelector('.card-detail-history-range.active');
  if (expandedHistoryTitle) expandedHistoryTitle.textContent = currentName + ' · Price History';
  if (expandedHistoryStatus) expandedHistoryStatus.textContent = currentStatus?.textContent || '';
  if (expandedHistoryMetric && inlineMetric) expandedHistoryMetric.value = inlineMetric.value;
  document.querySelectorAll('.card-detail-expanded-range').forEach(button => {
    button.classList.toggle('active', Number(button.dataset.days) === Number(activeRange?.dataset.days || 90));
  });
  if (inlineHistorySeries.length && expandedHistoryChart) {
    renderHistoryData(
      inlineHistorySeries, expandedHistoryChart, expandedHistoryStats,
      expandedHistoryLegend, inlineMetric?.value || 'market'
    );
  } else {
    if (expandedHistoryChart) expandedHistoryChart.innerHTML = '';
    if (expandedHistoryStats) expandedHistoryStats.innerHTML = '';
    if (expandedHistoryLegend) expandedHistoryLegend.innerHTML = '';
  }
}

async function loadPriceHistory(days) {
  historyDays = days;
  if (!historyContext || !historyStatus) return;
  historyStatus.textContent = `Loading ${days}-day history…`;
  historyStats.innerHTML = '';
  historyChart.innerHTML = '';
  if (historyLegend) historyLegend.innerHTML = '';
  try {
    const response = await fetch(`/api/price-history/${encodeURIComponent(historyContext.tcg)}?days=${days}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) {
      historyStatus.textContent = data.error || 'Could not load price history.';
      return;
    }
    historySeries = data.series || [];
    const dailyPoints = historySeries[0]?.points?.length || 0;
    historyStatus.textContent = dailyPoints
      ? `${dailyPoints} history points · ${historySeries.length} provider subtype(s) · timezone ${data.timezone}`
      : 'No historical observations have been recorded for this product yet.';
    if (historySeries.length) {
      renderHistoryData(historySeries, historyChart, historyStats, historyLegend, historyMetric?.value || 'market');
    }
  } catch (_) {
    historyStatus.textContent = 'Could not load price history.';
  }
}

document.querySelectorAll('.price-history-button').forEach(button => {
  button.addEventListener('click', () => {
    historyContext = {tcg: button.dataset.tcg, name: button.dataset.name};
    historyTitle.textContent = `${historyContext.name} · Price History`;
    historySubtitle.textContent = `TCGplayer #${historyContext.tcg}`;
    document.querySelectorAll('.history-range').forEach(item =>
      item.classList.toggle('active', Number(item.dataset.days) === 90)
    );
    if (historyMetric) historyMetric.value = 'market';
    historyDialog.showModal();
    loadPriceHistory(90);
  });
});
document.querySelectorAll('.history-range').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.history-range').forEach(item => item.classList.remove('active'));
    button.classList.add('active');
    loadPriceHistory(Number(button.dataset.days));
  });
});
historyMetric?.addEventListener('change', () => {
  if (historySeries.length) renderHistoryData(historySeries, historyChart, historyStats, historyLegend, historyMetric.value);
});

async function loadCardDetailHistory(tcg, days, requestId) {
  const status = document.getElementById('cardDetailHistoryStatus');
  const stats = document.getElementById('cardDetailHistoryStats');
  const chart = document.getElementById('cardDetailPriceHistoryChart');
  const legend = document.getElementById('cardDetailHistoryLegend');
  const metricSelect = document.getElementById('cardDetailHistoryMetric');
  if (!status || !stats || !chart) return;
  stats.innerHTML = '';
  chart.innerHTML = '';
  if (legend) legend.innerHTML = '';
  if (!tcg) {
    status.textContent = 'No TCGplayer product is linked to this printing, so price history is unavailable.';
    refreshExpandedCardHistory();
    return;
  }
  status.textContent = `Loading ${days}-day price history…`;
  refreshExpandedCardHistory();
  try {
    const response = await fetch(`/api/price-history/${encodeURIComponent(tcg)}?days=${days}`);
    const data = await response.json().catch(() => ({}));
    if (requestId !== inlineHistoryRequest) return;
    if (!response.ok || !data.success) {
      status.textContent = data.error || 'Could not load price history.';
      refreshExpandedCardHistory();
      return;
    }
    inlineHistorySeries = data.series || [];
    const dailyPoints = inlineHistorySeries[0]?.points?.length || 0;
    status.textContent = dailyPoints
      ? `${dailyPoints} history points · ${inlineHistorySeries.length} provider subtype(s) · timezone ${data.timezone}`
      : 'No historical observations have been recorded for this product yet.';
    if (inlineHistorySeries.length) {
      renderHistoryData(
        inlineHistorySeries, chart, stats, legend,
        metricSelect?.value || 'market'
      );
    }
    refreshExpandedCardHistory();
  } catch (_) {
    if (requestId === inlineHistoryRequest) {
      status.textContent = 'Could not load price history.';
      refreshExpandedCardHistory();
    }
  }
}

window.openCardDetailPriceHistory = function(tcg, name) {
  const requestId = ++inlineHistoryRequest;
  const status = document.getElementById('cardDetailHistoryStatus');
  const metricSelect = document.getElementById('cardDetailHistoryMetric');
  if (status) status.textContent = tcg
    ? 'Preparing price history…'
    : 'No TCGplayer product is linked to this printing, so price history is unavailable.';
  if (expandedHistoryDialog?.open) expandedHistoryDialog.close();
  document.querySelectorAll('.card-detail-history-range, .card-detail-expanded-range').forEach(button =>
    button.classList.toggle('active', Number(button.dataset.days) === 90)
  );
  if (metricSelect) metricSelect.value = 'market';
  if (expandedHistoryMetric) expandedHistoryMetric.value = 'market';
  if (tcg) void loadCardDetailHistory(tcg, 90, requestId);
};
function setCardDetailHistoryRange(days) {
  document.querySelectorAll('.card-detail-history-range, .card-detail-expanded-range').forEach(button =>
    button.classList.toggle('active', Number(button.dataset.days) === Number(days))
  );
  const link = document.getElementById('oracleTcgplayerLink');
  const tcg = link && !link.hidden ? link.href.split('/').pop() : '';
  void loadCardDetailHistory(tcg, Number(days), ++inlineHistoryRequest);
}
document.querySelectorAll('.card-detail-history-range, .card-detail-expanded-range').forEach(button =>
  button.addEventListener('click', () => setCardDetailHistoryRange(button.dataset.days))
);
function setCardDetailHistoryMetric(metric) {
  const inlineMetric = document.getElementById('cardDetailHistoryMetric');
  if (inlineMetric) inlineMetric.value = metric;
  if (expandedHistoryMetric) expandedHistoryMetric.value = metric;
  if (inlineHistorySeries.length) {
    renderHistoryData(
      inlineHistorySeries,
      document.getElementById('cardDetailPriceHistoryChart'),
      document.getElementById('cardDetailHistoryStats'),
      document.getElementById('cardDetailHistoryLegend'),
      metric
    );
  }
  refreshExpandedCardHistory();
}
document.getElementById('cardDetailHistoryMetric')?.addEventListener('change', event =>
  setCardDetailHistoryMetric(event.target.value)
);
expandedHistoryMetric?.addEventListener('change', event =>
  setCardDetailHistoryMetric(event.target.value)
);
document.getElementById('expandCardDetailHistory')?.addEventListener('click', () => {
  if (!expandedHistoryDialog || expandedHistoryDialog.open) return;
  expandedHistoryDialog.showModal();
  refreshExpandedCardHistory();
});
document.getElementById('closeCardDetailHistoryExpanded')?.addEventListener('click', () => {
  if (expandedHistoryDialog?.open) expandedHistoryDialog.close();
});
expandedHistoryDialog?.addEventListener('click', event => {
  if (event.target === expandedHistoryDialog) expandedHistoryDialog.close();
});
