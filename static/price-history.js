// Shared price-history dialog behavior for Inventory and Vending.
let historyContext = null;
let historyDays = 90;
const historyDialog = document.getElementById('priceHistoryDialog');
const historyChart = document.getElementById('priceHistoryChart');
const historyStatus = document.getElementById('priceHistoryStatus');
const historyStats = document.getElementById('priceHistoryStats');
const historyTitle = document.getElementById('priceHistoryTitle');
const historySubtitle = document.getElementById('priceHistorySubtitle');

function closePriceHistory() { historyDialog.close(); }
function escHtml(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function localLabel(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {month:'short', day:'numeric'});
}

function formatMoney(v) { return v == null || Number.isNaN(Number(v)) ? '—' : '$' + Number(v).toFixed(2); }

function seriesPath(points, key, xFn, yFn) {
  let path = '';
  let drawing = false;
  points.forEach((p, i) => {
    const value = p[key];
    if (value == null || Number.isNaN(Number(value))) { drawing = false; return; }
    const command = drawing ? 'L' : 'M';
    path += `${command} ${xFn(i).toFixed(2)} ${yFn(Number(value)).toFixed(2)} `;
    drawing = true;
  });
  return path.trim();
}

function renderHistoryChart(points) {
  historyChart.innerHTML = '';
  const NS = 'http://www.w3.org/2000/svg';
  const W = 820, H = 360, left = 58, right = 18, top = 18, bottom = 48;
  const plotW = W-left-right, plotH = H-top-bottom;
  const values = points.flatMap(p => [p.normal, p.foil]).filter(v => v != null && Number.isFinite(Number(v))).map(Number);
  if (!values.length) {
    historyChart.innerHTML = '<text x="410" y="185" text-anchor="middle" fill="#888" font-size="16">No price history yet</text>';
    return;
  }
  let min = Math.min(...values), max = Math.max(...values);
  if (min === max) { min = Math.max(0, min - 1); max += 1; }
  const pad = (max-min)*0.08; min = Math.max(0, min-pad); max += pad;
  const xFn = i => left + (points.length <= 1 ? plotW/2 : (i/(points.length-1))*plotW);
  const yFn = v => top + (max-v)/(max-min)*plotH;
  for (let i=0;i<5;i++) {
    const v = min + (max-min)*(i/4);
    const y = yFn(v);
    const line = document.createElementNS(NS,'line'); line.setAttribute('x1',left); line.setAttribute('x2',W-right); line.setAttribute('y1',y); line.setAttribute('y2',y); line.setAttribute('stroke','#2f2f2f'); line.setAttribute('stroke-width','1'); historyChart.appendChild(line);
    const txt = document.createElementNS(NS,'text'); txt.setAttribute('x',left-8); txt.setAttribute('y',y+4); txt.setAttribute('text-anchor','end'); txt.setAttribute('fill','#888'); txt.setAttribute('font-size','12'); txt.textContent=formatMoney(v); historyChart.appendChild(txt);
  }
  const gridDates = [0, Math.floor((points.length-1)/2), Math.max(0,points.length-1)];
  [...new Set(gridDates)].forEach(i => {
    const x=xFn(i); const line=document.createElementNS(NS,'line'); line.setAttribute('x1',x); line.setAttribute('x2',x); line.setAttribute('y1',top); line.setAttribute('y2',H-bottom); line.setAttribute('stroke','#242424'); line.setAttribute('stroke-width','1'); historyChart.appendChild(line);
    const txt=document.createElementNS(NS,'text'); txt.setAttribute('x',x); txt.setAttribute('y',H-17); txt.setAttribute('text-anchor','middle'); txt.setAttribute('fill','#888'); txt.setAttribute('font-size','12'); txt.textContent=localLabel(points[i].captured_at); historyChart.appendChild(txt);
  });
  const pNormal=document.createElementNS(NS,'path'); pNormal.setAttribute('d',seriesPath(points,'normal',xFn,yFn)); pNormal.setAttribute('fill','none'); pNormal.setAttribute('stroke','#35b7e6'); pNormal.setAttribute('stroke-width','3'); pNormal.setAttribute('stroke-linejoin','round'); pNormal.setAttribute('stroke-linecap','round'); historyChart.appendChild(pNormal);
  const pFoil=document.createElementNS(NS,'path'); pFoil.setAttribute('d',seriesPath(points,'foil',xFn,yFn)); pFoil.setAttribute('fill','none'); pFoil.setAttribute('stroke','#f3c842'); pFoil.setAttribute('stroke-width','3'); pFoil.setAttribute('stroke-linejoin','round'); pFoil.setAttribute('stroke-linecap','round'); historyChart.appendChild(pFoil);
  [[...points.entries()].find(([_,p])=>p.normal!=null), [...points.entries()].find(([_,p])=>p.foil!=null)].filter(Boolean).forEach(([idx,p])=>{ const key=p.normal!=null?'normal':'foil'; const c=document.createElementNS(NS,'circle'); c.setAttribute('cx',xFn(idx)); c.setAttribute('cy',yFn(Number(p[key]))); c.setAttribute('r','4'); c.setAttribute('fill',key==='normal'?'#35b7e6':'#f3c842'); historyChart.appendChild(c); });
}

async function loadPriceHistory(days) {
  historyDays = days;
  if (!historyContext) return;
  historyStatus.textContent = `Loading ${days}-day history…`;
  historyStats.innerHTML = '';
  historyChart.innerHTML = '';
  const r = await fetch(`/api/price-history/${encodeURIComponent(historyContext.tcg)}?days=${days}`);
  const data = await r.json().catch(()=>({}));
  if (!r.ok || !data.success) { historyStatus.textContent = data.error || 'Could not load price history.'; return; }
  const points = data.points || [];
  historyStatus.textContent = points.length ? `${points.length} recorded price points · timezone ${data.timezone}` : 'No historical snapshots have been recorded yet. The updater will begin building history after the next successful daily price sync.';
  if (!points.length) return;
  const start = points[0], end = points[points.length-1];
  const changes=[];
  [['Normal','normal'],['Foil','foil']].forEach(([label,key])=>{
    if (start[key] != null && end[key] != null && Number(start[key]) > 0) {
      const delta=Number(end[key])-Number(start[key]); const pct=(delta/Number(start[key]))*100;
      changes.push(`<div><span>${label}</span><strong>${formatMoney(end[key])}</strong><small>${delta >= 0 ? '+' : ''}${formatMoney(delta)} (${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%)</small></div>`);
    }
  });
  historyStats.innerHTML=changes.join('');
  renderHistoryChart(points);
}

document.querySelectorAll('.price-history-button').forEach(button => {
  button.addEventListener('click', () => {
    historyContext = {tcg: button.dataset.tcg, name: button.dataset.name};
    historyTitle.textContent = `${historyContext.name} · Price History`;
    historySubtitle.textContent = `TCGplayer #${historyContext.tcg}`;
    document.querySelectorAll('.history-range').forEach(b => b.classList.toggle('active', Number(b.dataset.days) === 90));
    historyDialog.showModal();
    loadPriceHistory(90);
  });
});
document.querySelectorAll('.history-range').forEach(button => {
  button.addEventListener('click', () => { document.querySelectorAll('.history-range').forEach(b => b.classList.remove('active')); button.classList.add('active'); loadPriceHistory(Number(button.dataset.days)); });
});
