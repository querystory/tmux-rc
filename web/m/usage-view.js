// The usage view (#view=usage): every meter as a sawtooth over time, one chart per window
// type (5-hour, Weekly, Weekly <model>), each account a series in its chip's colour. Past
// windows are the earlier teeth, muted; the current one is bold, dashed on to its reset at
// the fitted pace. One range control over all of them, and they zoom and pan together.
// See docs/design/plan-usage.md.
import { load } from './atlas-charts.js';
import { segmented } from './atlas.js';
import { dismissable } from './header-picker.js';
import { countdown } from './usage.js';

const H = 3600e3, D = 24 * H, NARROW = 700;
const RANGES = { '1d': D, '7d': 7 * D, '30d': 30 * D, All: Infinity };
const SPAN = { '1d': 'last 24 hours', '7d': 'last 7 days', '30d': 'last 30 days', All: 'all history' };
const NAMES = { claude: 'Claude', codex: 'Codex' };
const esc = s => String(s).replace(/[&<>"]/g, c => `&#${c.charCodeAt(0)};`);
const title = name => name === '5h' ? '5-hour' : name.replace(/^7d\b/, 'Weekly');
const order = name => name === '5h' ? 0 : name === '7d' ? 1 : 2;
const day = t => new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric' });
const stamp = t => new Date(t).toLocaleString([], { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
// A clock time: the weekday as well once it is not today.
const clock = (t, now) => new Date(t).toLocaleString([], { ...(new Date(t).toDateString() !== new Date(now).toDateString() && { weekday: 'short' }), hour: 'numeric', minute: '2-digit' });

// The view's choices outlive a visit: the range (a week until one is picked: enough 5h teeth
// to compare days, and this weekly window whole), and the span someone zoomed or panned to (null: the range's).
let picked = null, zoom = null, view = null;
// The account each chart's bands follow, by window type: the one picked by its chip, else the fullest.
const focusBy = {};
// The accounts this view shows, by id (provider and dirs); none is all. Its own, per browser:
// the list's account filter scopes the list, and narrowing the charts should not hide panes.
const KEY = 'tmuxrc-usage-accounts', idOf = a => `${a.provider} ${a.dirs}`;
let chosen = [];
try { chosen = JSON.parse(localStorage.getItem(KEY)) || []; } catch {}
const rangeOf = () => picked || '7d';
// How many days of ended windows the view asks the server for: a month covers every range
// but All (every one, however old), so picking one does not wait on a fetch.
export const usageDays = () => picked === 'All' ? Infinity : 30;

// One meter's sawtooth, in ms: the ended windows (each from 0% at its start, to where it
// stood at its reset, back to 0%), the current one to now, and its forecast to the reset,
// or to 100% and along it if it gets there first. Hits are the times windows reached 100%.
function teeth(w, now) {
  const past = [], hits = [];
  // An ended window: from 0% at its start, to where it stood at its reset, back to 0%.
  const ended = (p, hit) => {
    // Within the window's final bounds: a reset re-read up to an hour off groups samples
    // from either side, and the points must run forward in time.
    const pts = p.samples.filter(([t]) => t >= p.start && t <= p.resets_at), last = pts.length ? pts[pts.length - 1][1] : p.pct;
    past.push([p.start, 0], ...pts, [p.resets_at, last], [p.resets_at, 0]);
    if (hit >= p.start && hit <= p.resets_at) hits.push(hit);
  };
  for (const p of w.past || []) ended(p, p.limit_at);
  // The current window, unless it has none yet, or reset since this reading (an ended one till the next).
  if (w.resets_at != null && w.resets_at <= now) ended(w, w.hit_at);
  if (!(w.resets_at > now)) return { past, cur: [], fc: [], hits, drawn: past };
  const held = [Math.max(now, w.start), w.pct];
  past.push([w.start, 0]);
  if (w.hit_at) hits.push(w.hit_at);
  const cur = [[w.start, 0], ...w.samples, held];
  return { past, hits, cur, drawn: [...past, ...cur], // drawn: what the tooltip reads, built once
    fc: [held, ...(w.limit_at > held[0] ? [[w.limit_at, 100], [w.resets_at, 100]] : [[w.resets_at, Math.min(100, w.projected ?? w.pct)]])] };
}

// Usage at t, read off the points: the tooltip's one exact number per meter, rather than
// echarts' nearest sample per series.
function valueAt(points, t) {
  if (!points.length || t < points[0][0] || t > points[points.length - 1][0]) return null;
  let lo = 0, hi = points.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (points[mid][0] <= t) lo = mid; else hi = mid; }
  const [a, b] = [points[lo], points[hi]];
  return b[0] === a[0] ? b[1] : a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0]);
}

// The focused meter's windows as background bands: ended ones alternate grey and clear back
// from the current one, which is amber. A window ends at its reset, at the next one's start
// if that comes first, or where its usage fell to near 0 before either: a quota reset early,
// and what follows is a fresh one, banded on its own. Stretches with no window stay clear.
function bands(m, now) {
  const past = m.w.past || [], out = [];
  past.forEach((p, i) => {
    const next = Math.min(p.resets_at, past[i + 1]?.start ?? m.w.start ?? Infinity), pts = p.samples;
    const drop = pts.find(([t, v], k) => k && t > p.start && t < next && v <= 5 && pts[k - 1][1] - v >= 15)?.[0];
    if (next <= p.start) return;
    out.push([p.start, drop || next]);
    if (drop) out.push([drop, next]);
  });
  const grey = out.map(([a, b], k) => [a, b, (out.length - k) % 2 ? 'grey' : null]).filter(b => b[2]);
  return m.w.resets_at > now ? [...grey, [m.w.start, m.w.resets_at, 'now']] : grey;
}

// A meter's current window as numbers, for its chart's heading: the chip, the %, any "full
// at", and the reset's countdown and clock time.
function readout(m, now, type, on) {
  const w = m.w, level = w.limit_at || w.pct >= 100 ? 'full' : (w.projected ?? 0) >= 90 ? 'warn' : '', live = w.resets_at > now;
  // Both, when it fills first: "full" says the room runs out, "resets" when it comes back. On a
  // phone each account is one row (chip, %, status), so its times are short ("Thu 10:10p") and
  // the reset's clock time is left to the row's tooltip.
  const at = t => view.narrow ? clock(t, now).replace(/ ([AP])M$/, (_, x) => x.toLowerCase()) : clock(t, now);
  const reset = live ? `resets ${countdown(w.resets_at - now)}<span class="uv-at">, ${clock(w.resets_at, now)}</span>` : 'not started';
  const tip = [live && `resets ${stamp(w.resets_at)}`, m.as_of && `as of ${countdown(now - m.as_of)} ago`].filter(Boolean).join(' · ');
  // The chip picks the account the chart's bands follow (on a phone, anywhere on its row).
  return `<span class="uv-read" title="${esc(tip)}"><button type="button" class="uv-pick" data-type="${esc(type)}" data-id="${esc(m.id)}" aria-pressed="${on}" aria-label="Shade ${esc(m.name)}'s windows (${esc(m.dirs)})">${m.chip}</button><b class="${level}">${Math.round(w.pct)}%</b>`
    + `<span class="uv-status">${w.limit_at > now && live ? `<span class="out">full ${at(w.limit_at)}</span> · ` : ''}${reset}</span></span>`;
}

function option(card, extent, now, slider, css) {
  const color = n => css.getPropertyValue(n).trim(), muted = color('--muted'), line = color('--line');
  const fg = color('--fg'), surface = color('--surface'), red = color('--red'), accent = color('--accent');
  const height = card.chartEl.clientHeight, narrow = view.narrow, bottom = slider ? (narrow ? 66 : 70) : 28;
  const focused = card.meters.find(m => m.id === focusBy[card.type]);
  const fill = { grey: [color('--raised'), 0.6], now: [color('--chart-s5'), 0.16] };
  const series = card.meters.flatMap(m => {
    const base = { name: m.name, type: 'line', showSymbol: false, emphasis: { disabled: true }, itemStyle: { color: m.color } }, on = m === focused;
    return [
      { ...base, z: 2, data: m.past, lineStyle: { color: m.color, width: on ? 2 : 1.5, opacity: on ? 0.5 : 0.2 } },
      { ...base, z: on ? 5 : 4, data: m.cur, lineStyle: { color: m.color, width: on ? 3.5 : 2, opacity: on ? 1 : 0.5 } },
      { ...base, z: on ? 5 : 4, data: m.fc, lineStyle: { color: m.color, width: on ? 2.5 : 1.5, opacity: on ? 1 : 0.5, type: [6, 5] } },
      // Clipped as a scatter clips: a mark off the visible span is left out, never drawn past the axis.
      { name: m.name, type: 'scatter', z: 6, symbol: 'triangle', symbolRotate: 180, symbolSize: 11, symbolOffset: [0, -9],
        itemStyle: { color: red, borderColor: surface, borderWidth: 1.5 }, tooltip: { show: false },
        data: [...m.hits.map(t => [t, 100]), ...(m.w.limit_at > now ? [{ value: [m.w.limit_at, 100], itemStyle: { color: surface, borderColor: red, borderWidth: 2 } }] : [])] },
    ];
  });
  series.push({ type: 'line', data: [], silent: true, z: 0, markArea: { silent: true, data: (focused ? bands(focused, now) : [])
    .map(([a, b, kind]) => [{ xAxis: a, itemStyle: { color: fill[kind][0], opacity: fill[kind][1] } }, { xAxis: b }]) } });
  series.push({ type: 'line', data: [], silent: true, markLine: { silent: true, symbol: 'none', data: [{ yAxis: 100 }],
    lineStyle: { color: red, type: 'dashed', width: 1.5, opacity: 0.8 }, label: { show: false } } }); // the limit: 100% says it
  const left = narrow ? 42 : 52, right = narrow ? 14 : 48;
  const range = zoom || windowOf(extent, now);
  return {
    animation: false, textStyle: { color: muted, fontFamily: 'sans-serif', fontSize: 12 },
    grid: { left, right, top: 22, bottom },
    tooltip: { trigger: 'axis', renderMode: 'html', appendTo: 'body', axisPointer: { type: 'line', lineStyle: { color: muted } },
      backgroundColor: surface, borderColor: line, textStyle: { color: fg, fontSize: 13 },
      formatter: items => {
        const t = items[0].axisValue;
        return [`<b>${stamp(t)}</b>${t > now ? ' · forecast' : ''}`, ...card.meters.map(m => {
          const v = valueAt(t > now ? m.fc : m.drawn, t);
          return v == null ? '' : `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${m.color};margin-right:6px"></span>${esc(m.name)}: <b>${Math.round(v)}%</b>`
            + (t >= m.w.start && t <= m.w.resets_at ? ` <span style="color:${muted}">this window, resets ${clock(m.w.resets_at, now)}</span>` : '');
        }).filter(Boolean)].join('<br>');
      } },
    dataZoom: [
      // Not on a phone: a drag on a chart there is the page scrolling, not a pan (a stray one
      // had panned a phone's charts off into the empty forecast).
      { type: 'inside', xAxisIndex: 0, ...range, filterMode: 'none', disabled: narrow, zoomOnMouseWheel: true, moveOnMouseMove: true, moveOnMouseWheel: false },
      { type: 'slider', xAxisIndex: 0, show: slider, ...range, filterMode: 'none', height: narrow ? 32 : 28, bottom: narrow ? 6 : 10, left, right, handleSize: narrow ? '140%' : '100%', moveHandleSize: narrow ? 10 : 7, // a thumb's worth on a phone
        borderColor: line, fillerColor: color('--accent-bg'), textStyle: { color: muted, fontSize: 12 }, showDetail: !narrow, handleLabel: { show: !narrow }, labelFormatter: day, // dated ends: it spans all the history loaded, not the axis above it
        handleStyle: { color: accent, borderColor: accent }, moveHandleStyle: { color: accent },
        dataBackground: { lineStyle: { color: muted, width: 1 }, areaStyle: { color: muted, opacity: 0.25 } },
        selectedDataBackground: { lineStyle: { color: accent, width: 1 }, areaStyle: { color: accent, opacity: 0.3 } } },
    ],
    // One extent for every chart, so a shared zoom lines them up.
    xAxis: { type: 'time', ...extent, axisLine: { lineStyle: { color: line } }, axisTick: { show: false },
      splitLine: { show: true, lineStyle: { color: line, opacity: 0.6 } },
      axisLabel: { color: muted, fontSize: 12, hideOverlap: true, formatter: { month: '{MMM} {d}', day: '{ee} {MMM} {d}', hour: '{h} {A}', minute: '{h}:{mm} {A}' } } },
    yAxis: { type: 'value', min: 0, max: 100, interval: height - bottom - 22 < 160 ? 50 : 25,
      axisLabel: { color: muted, fontSize: 12, formatter: '{value}%' }, splitLine: { lineStyle: { color: line, opacity: 0.7 } } },
    series,
  };
}

// The range back from now, and on past now by a fifth of it (at least six hours), so a
// forecast shows without a short range spending most of its width on the future.
const windowOf = ({ min, max }, now) => {
  const span = RANGES[rangeOf()];
  return { startValue: Math.max(min, now - span), endValue: Math.min(max, now + Math.max(6 * H, Math.min(span, 30 * D) / 5)) };
};

// `all`: every account, named as app.js names them (acct, chip); the view shows `chosen`.
export function renderUsageView(root, all, options) {
  const { now = Date.now(), refetch, icon } = options;
  if (!view) view = setup(root, refetch, icon);
  view.again = () => renderUsageView(root, all, options);
  const ids = [...new Set(all.map(idOf))], on = chosen.filter(k => ids.includes(k));
  const accounts = on.length ? all.filter(a => on.includes(idOf(a))) : all;
  // The menu, drawn as the list's account filter draws its own (accounts.js), on this view's state.
  view.picker.hidden = ids.length < 2; // one account: nothing to pick
  const menu = '<div role="group" aria-label="Account"><div class="menu-sec">Account</div>'
    + '<button type="button" class="acct-opt" data-id=""><i class="acct-dot none"></i><span>All accounts</span></button>'
    + ids.map(k => { const a = all.find(x => idOf(x) === k);
      return `<button type="button" class="acct-opt" data-id="${esc(k)}"><i class="acct-dot" style="--a:var(--acct-${a.acct?.color ?? 0})"></i><span>${esc(a.acct?.name || NAMES[a.provider] || a.provider)}</span></button>`; }).join('') + '</div>';
  // Redrawn only when the accounts change, and the picks marked in place: a click on a redrawn
  // button would read as outside the menu, and close it.
  if (view.menuHtml !== menu) view.menu.innerHTML = view.menuHtml = menu;
  view.picker.firstChild.classList.toggle('on', on.length > 0); // a pick in force shows on the button
  view.menu.querySelectorAll('[data-id]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.id ? on.includes(b.dataset.id) : !on.length)));
  view.now = now;
  fit();
  const css = getComputedStyle(document.documentElement);
  // A meter per account and window, grouped by window type: Codex's week sits in Weekly.
  const byType = {};
  accounts.forEach((a, i) => (a.windows || []).forEach(w => (byType[w.window] ||= []).push({
    w, as_of: a.as_of, chip: a.chip, id: idOf(a), dirs: (a.dirs || []).join(', '), name: a.acct?.name || NAMES[a.provider] || a.provider,
    color: css.getPropertyValue(`--acct-${a.acct?.color ?? i}`).trim(), ...teeth(w, now) })));
  // Every window type of every account, picked or not, so a chart the pick empties says so.
  const types = [...new Set([...all.flatMap(a => (a.windows || []).map(w => w.window)), ...Object.keys(byType)])].sort((a, b) => order(a) - order(b) || a.localeCompare(b));
  if (types.join() !== view.types.join()) build(types);
  const times = Object.values(byType).flat().flatMap(m => [m.past[0]?.[0], m.cur[0]?.[0], m.w.resets_at]).filter(Boolean);
  // At least the days loaded, so a new account's 30d still spans 30 days; All, what there is.
  view.extent = { min: Math.min(now - (usageDays() === Infinity ? D : usageDays() * D), ...times), max: Math.max(now + 6 * H, ...times) + H };
  if (zoom) zoomTo(zoom.startValue, zoom.endValue - zoom.startValue); // within the new extent: a filter can narrow it
  // An account that cannot be read has no windows to draw; it says so rather than vanish.
  view.errs.innerHTML = accounts.filter(a => a.error).map(a => `<span class="uv-read">${a.chip || esc(NAMES[a.provider] || a.provider)}<span>${esc(a.error)}</span></span>`).join('');
  for (const card of view.cards) {
    card.meters = byType[card.type] || []; // none: the account filter hides every account with this window
    card.el.classList.toggle('empty', !card.meters.length);
    if (card.meters.length && !card.meters.some(m => m.id === focusBy[card.type])) // the fullest shown, until a chip is picked
      focusBy[card.type] = card.meters.reduce((a, b) => b.w.pct > a.w.pct ? b : a).id; // by its dirs: names need not be unique
    const head = `<h3>${esc(title(card.type))} <small>${esc(card.type)}</small></h3>${card.meters.map(m => readout(m, now, card.type, m.id === focusBy[card.type])).join('')
      || '<span class="uv-none">No selected account has this window</span>'}`;
    if (card.headEl.innerHTML !== head) card.headEl.innerHTML = head;
    // The chart in words: what the canvas shows past the heading's numbers.
    card.chartEl.setAttribute('aria-label', `${title(card.type)} usage over time. ${card.meters.map(m => `${m.name}: ${(m.w.past || []).length} earlier windows loaded, `
      + `${m.hits.length ? `${m.hits.length} hit the limit, the last ${stamp(Math.max(...m.hits))}` : 'none hit the limit'}; now ${Math.round(m.w.pct)}%`
      + (m.w.projected != null ? `, ${Math.round(m.w.projected)}% by the reset at this pace` : '')).join('. ')}. Plus and minus zoom, left and right arrows pan.`);
  }
  paint();
}

function setup(root, refetch, icon) {
  root.innerHTML = '<div class="uv-bar"><div class="uv-errs"></div><span class="uv-span"></span></div><div class="uv-stack"></div>';
  const v = { root, types: [], cards: [], bar: root.firstChild, span: root.querySelector('.uv-span'), stack: root.querySelector('.uv-stack'), errs: root.querySelector('.uv-errs') };
  v.range = segmented('Time range', Object.keys(RANGES), rangeOf(), r => {
    const more = usageDays();
    picked = r; zoom = null;
    if (usageDays() > more) refetch();
    paint();
  });
  // Which accounts to chart: the header-picker menu the list's filter uses, several at once,
  // open while picking. None ticked is all.
  v.picker = document.createElement('details');
  v.picker.className = 'header-picker uv-filter';
  v.picker.innerHTML = `<summary title="Accounts to chart" aria-label="Accounts to chart">${icon}</summary><div class="header-picker-menu"></div>`;
  v.menu = v.picker.lastChild;
  v.menu.addEventListener('click', e => {
    const k = e.target.closest('[data-id]')?.dataset.id;
    if (k == null) return;
    chosen = !k ? [] : chosen.includes(k) ? chosen.filter(x => x !== k) : [...chosen, k];
    try { localStorage.setItem(KEY, JSON.stringify(chosen)); } catch {}
    view.again();
  });
  dismissable(v.picker);
  v.bar.append(v.range, v.picker);
  root.addEventListener('click', e => {
    const pick = e.target.closest('.uv-pick') || (view.narrow ? e.target.closest('.uv-read')?.querySelector('.uv-pick') : null);
    if (!pick) return;
    focusBy[pick.dataset.type] = pick.dataset.id;
    pick.closest('.uv-head').querySelectorAll('.uv-pick').forEach(b => b.setAttribute('aria-pressed', String(b === pick)));
    paint();
  });
  new MutationObserver(paint).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  new ResizeObserver(() => { if (root.isConnected && root.clientWidth) fit() ? view.again() : paint(); }).observe(root); // not while away: a detached view measures 0
  return v;
}

// A phone's layout below NARROW; crossing it starts the zoom over, as the layout changes.
function fit() {
  if (!view.root.clientWidth) return false;
  const narrow = view.root.clientWidth < NARROW, crossed = view.narrow != null && narrow !== view.narrow;
  if (crossed) zoom = null;
  view.narrow = narrow;
  return crossed; // the headings change too (readout), so they are redrawn
}

function build(types) {
  view.chart = null;
  for (const c of view.cards) { c.sized?.disconnect(); c.chart?.dispose(); }
  view.types = types;
  view.cards = types.map(type => {
    const el = document.createElement('section');
    el.className = 'uv-card';
    el.innerHTML = '<div class="uv-head"></div><div class="uv-chart" role="img" tabindex="0" title="Scroll to zoom, drag to pan, hover for the exact %. Keyboard: + or − to zoom, arrows to pan."></div>';
    el.lastChild.addEventListener('keydown', keys);
    return { type, el, headEl: el.firstChild, chartEl: el.lastChild };
  });
  view.stack.replaceChildren(...view.cards.map(c => c.el));
}

// The keyboard's zoom and pan, as the Activity chart's: + and − halve and double the span
// about its middle, the arrows move it by half, all within the loaded history.
function keys(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const { startValue: s, endValue: t } = zoom || windowOf(view.extent, view.now), span = t - s, mid = (s + t) / 2;
  const next = { '+': [mid - span / 4, span / 2], '=': [mid - span / 4, span / 2], '-': [mid - span, span * 2],
    ArrowLeft: [s - span / 2, span], ArrowRight: [s + span / 2, span] }[e.key];
  if (!next) return;
  e.preventDefault();
  zoomTo(...next);
  paint();
}
// The span on screen: at least an hour, within the loaded history, and starting at least an
// hour before now. Past that it would be all forecast, an empty chart: a slider dragged to the
// end once left a phone showing two days ahead and "0 hours shown".
function zoomTo(from, span) {
  const { min, max } = view.extent, width = Math.min(max - min, Math.max(H, span));
  const start = Math.max(min, Math.min(max - width, view.now - H, from));
  zoom = { startValue: start, endValue: start + width };
}

// Draw every chart: stacked to fill the view, or on a phone stacked to scroll, each its own height.
let painting = null;
async function paint() {
  if (!view?.types.length) return;
  const narrow = view.narrow;
  view.range.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.textContent === rangeOf())));
  view.stack.style.gridTemplateRows = narrow ? '' : view.cards.map(c => c.meters?.length ? 'minmax(0, 1fr)' : 'auto').join(' '); // an emptied chart is its one line
  let echarts;
  try { echarts = await (painting ||= load('echarts', '/m/vendor/echarts.min.js')); } catch { painting = null; view.stack.textContent = 'Chart unavailable. Reload to retry.'; return; }
  const css = getComputedStyle(document.documentElement), on = view.cards.filter(c => c.chartEl.clientWidth);
  for (const c of on) {
    if (!c.chart) {
      c.chart = echarts.init(c.chartEl);
      c.chart.group = 'usage'; // one zoom and one hover line for every chart (echarts.connect)
      c.chart.on('datazoom', () => {
        const z = c.chart.getOption().dataZoom[0];
        zoomTo(z.startValue, z.endValue - z.startValue);
        if (zoom.startValue !== z.startValue) paint(); else spanText(); // held back from all-forecast
      });
      (c.sized = new ResizeObserver(() => c.chart.resize())).observe(c.chartEl);
    }
    c.el.classList.toggle('slid', c === on[on.length - 1]); // the slider's chart: a phone gives it the room
    c.chart.setOption(option(c, view.extent, view.now, c === on[on.length - 1], css), true);
    c.chart.resize();
  }
  echarts.connect('usage');
  spanText();
}

// The range in words, beside its control: the dates shown, and how far back that is.
function spanText() {
  const { startValue: s, endValue: e } = zoom || windowOf(view.extent, view.now), short = e - s < 2 * D;
  const when = t => short ? `${day(t)}, ${new Date(t).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` : day(t);
  const back = Math.max(0, Math.min(e, view.now) - s), n = back >= 1.5 * D ? `${Math.round(back / D)} days` : `${Math.round(back / H)} hours`;
  view.span.innerHTML = `<b>${when(s)} – ${when(e)}</b> · ${zoom ? `${n} shown` : SPAN[rangeOf()]}`;
}
