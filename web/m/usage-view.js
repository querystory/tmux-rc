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

// The view's choices outlive a visit: the range (until picked, 30 days wide, and on a phone
// the one that suits the chart on screen: a day of 5h teeth, a month of weekly ones), the
// chart a phone shows, and the span someone zoomed or panned to (null: the range's).
let picked = null, shown = 0, zoom = null, view = null;
// The account each chart's bands follow, by window type: the one picked by its chip, else the fullest.
const focusBy = {};
const rangeOf = () => picked || (view?.narrow && view.types[shown] === '5h' ? '1d' : '30d');
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
  const w = m.w, level = w.limit_at || w.pct >= 100 ? 'full' : (w.projected ?? 0) >= 90 ? 'warn' : '';
  const reset = w.resets_at > now ? `resets ${countdown(w.resets_at - now)}, ${clock(w.resets_at, now)}` : 'not started';
  // A reading kept through a failed read says how old it is on hover; the sidebar says it in words.
  // The chip picks the account the chart's bands follow.
  return `<span class="uv-read"${m.as_of ? ` title="as of ${countdown(now - m.as_of)} ago"` : ''}><button type="button" class="uv-pick" data-type="${esc(type)}" data-id="${esc(m.id)}" aria-pressed="${on}" aria-label="Shade ${esc(m.name)}'s windows (${esc(m.dirs)})">${m.chip}</button><b class="${level}">${Math.round(w.pct)}%</b>`
    + (w.limit_at > now && w.resets_at > now ? `<span class="out">full ${clock(w.limit_at, now)}</span>` : '') + `<span>${reset}</span></span>`;
}

function option(card, extent, now, slider, css) {
  const color = n => css.getPropertyValue(n).trim(), muted = color('--muted'), line = color('--line');
  const fg = color('--fg'), surface = color('--surface'), red = color('--red'), accent = color('--accent');
  const height = card.chartEl.clientHeight, narrow = view.narrow, bottom = slider ? (narrow ? 60 : 70) : 28;
  const focused = card.meters.find(m => m.id === focusBy[card.type]);
  const fill = { grey: [color('--raised'), 0.6], now: [color('--amber-bg'), 1] };
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
      { type: 'inside', xAxisIndex: 0, ...range, filterMode: 'none', zoomOnMouseWheel: true, moveOnMouseMove: true, moveOnMouseWheel: false },
      { type: 'slider', xAxisIndex: 0, show: slider, ...range, filterMode: 'none', height: narrow ? 24 : 28, bottom: narrow ? 6 : 10, left, right,
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

// `accounts` as app.js names them (accounts.usage); `chip(a)` is an account's chip.
export function renderUsageView(root, accounts, { now = Date.now(), chip, refetch, types: read = [], filter }) {
  if (!view) view = setup(root, refetch, filter);
  view.picker.hidden = !filter.shown; // one account: nothing to pick
  view.now = now;
  fit();
  const css = getComputedStyle(document.documentElement);
  // A meter per account and window, grouped by window type: Codex's week sits in Weekly.
  const byType = {};
  accounts.forEach((a, i) => (a.windows || []).forEach(w => (byType[w.window] ||= []).push({
    w, as_of: a.as_of, chip: chip(a), id: `${a.provider} ${a.dirs}`, dirs: (a.dirs || []).join(', '), name: a.acct?.name || NAMES[a.provider] || a.provider,
    color: css.getPropertyValue(`--acct-${a.acct?.color ?? i}`).trim(), ...teeth(w, now) })));
  const types = [...new Set([...read, ...Object.keys(byType)])].sort((a, b) => order(a) - order(b) || a.localeCompare(b));
  if (types.join() !== view.types.join()) build(types);
  shown = Math.max(0, Math.min(shown, types.length - 1));
  const all = Object.values(byType).flat(), times = all.flatMap(m => [m.past[0]?.[0], m.cur[0]?.[0], m.w.resets_at]).filter(Boolean);
  // At least the days loaded, so a new account's 30d still spans 30 days; All, what there is.
  view.extent = { min: Math.min(now - (usageDays() === Infinity ? D : usageDays() * D), ...times), max: Math.max(now + 6 * H, ...times) + H };
  if (zoom) zoomTo(zoom.startValue, zoom.endValue - zoom.startValue); // within the new extent: a filter can narrow it
  // An account that cannot be read has no windows to draw; it says so rather than vanish.
  view.errs.innerHTML = accounts.filter(a => a.error).map(a => `<span class="uv-read">${chip(a) || esc(NAMES[a.provider] || a.provider)}<span>${esc(a.error)}</span></span>`).join('');
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
  // A phone on a chart the filter just emptied moves to one with something to show.
  if (!view.cards[shown]?.meters.length) {
    shown = Math.max(0, view.cards.findIndex(c => c.meters.length));
    if (!picked) zoom = null; // as show() does: the range follows the chart
  }
  paint();
}

function setup(root, refetch, filter) {
  root.innerHTML = '<div class="uv-bar"><div class="uv-errs"></div><span class="uv-span"></span></div><div class="uv-stack"></div>';
  const v = { root, types: [], cards: [], bar: root.firstChild, span: root.querySelector('.uv-span'), stack: root.querySelector('.uv-stack'), errs: root.querySelector('.uv-errs') };
  v.range = segmented('Time range', Object.keys(RANGES), rangeOf(), r => {
    const more = usageDays();
    picked = r; zoom = null;
    if (usageDays() > more) refetch();
    paint();
  });
  // The account filter: the list's own (app.js lends it while this menu is open), so one acct=
  // in the URL scopes both, and picking here narrows the list too.
  v.picker = document.createElement('details');
  v.picker.className = 'header-picker uv-filter';
  v.picker.innerHTML = `<summary title="Filter accounts" aria-label="Filter accounts">${filter.icon}</summary><div class="header-picker-menu"></div>`;
  v.picker.addEventListener('toggle', () => filter.place(v.picker.open ? v.picker.lastChild : null));
  dismissable(v.picker);
  v.bar.append(v.range, v.picker);
  // A sideways swipe on a heading or the tabs moves a phone to the next chart; one
  // on the chart pans it.
  let from = null;
  root.addEventListener('click', e => {
    const pick = e.target.closest('.uv-pick');
    if (!pick) return;
    focusBy[pick.dataset.type] = pick.dataset.id;
    pick.closest('.uv-head').querySelectorAll('.uv-pick').forEach(b => b.setAttribute('aria-pressed', String(b === pick)));
    paint();
  });
  root.addEventListener('pointerdown', e => { from = e.target.closest('.uv-head, .uv-tabs') && e; });
  root.addEventListener('pointerup', e => {
    const dx = from ? e.clientX - from.clientX : 0;
    if (view.narrow && Math.abs(dx) > 50 && Math.abs(dx) > 2 * Math.abs(e.clientY - from.clientY)) show(shown + (dx < 0 ? 1 : -1));
    from = null;
  });
  new MutationObserver(paint).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  new ResizeObserver(() => {
    if (!root.isConnected && v.picker.open) { v.picker.open = false; filter.place(null); } // left with the menu open (Back, say): the filter goes home
    else if (root.clientWidth) { fit(); paint(); } // not while away: a detached view measures 0
  }).observe(root);
  return v;
}

// One chart at a time below NARROW; crossing it starts the zoom over, as the layout changes.
function fit() {
  if (!view.root.clientWidth) return;
  const narrow = view.root.clientWidth < NARROW;
  if (narrow !== view.narrow && view.narrow != null) zoom = null;
  view.narrow = narrow;
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
  view.tabs?.remove();
  view.tabs = segmented('Window', types.map(title), title(types[shown] ?? types[0] ?? ''), t => show(types.map(title).indexOf(t)));
  view.tabs.classList.add('uv-tabs');
  view.bar.append(view.tabs);
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
// The span on screen, at least an hour and held within the loaded history.
function zoomTo(from, span) {
  const { min, max } = view.extent, width = Math.min(max - min, Math.max(H, span)), start = Math.max(min, Math.min(max - width, from));
  zoom = { startValue: start, endValue: start + width };
}

function show(i) {
  shown = (i + view.types.length) % view.types.length;
  if (!picked) zoom = null; // the range follows the chart until one is picked
  paint();
}

// Draw every chart on screen; a phone shows one at a time.
let painting = null;
async function paint() {
  if (!view?.types.length) return;
  const narrow = view.narrow;
  view.cards.forEach((c, k) => { c.el.hidden = narrow && k !== shown; });
  view.tabs.hidden = !narrow;
  view.tabs.querySelectorAll('button').forEach((b, k) => b.setAttribute('aria-pressed', String(k === shown)));
  view.range.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.textContent === rangeOf())));
  view.stack.style.gridTemplateRows = narrow ? 'minmax(0, 1fr)' : view.cards.map(c => c.meters?.length ? 'minmax(0, 1fr)' : 'auto').join(' '); // an emptied chart is its one line
  let echarts;
  try { echarts = await (painting ||= load('echarts', '/m/vendor/echarts.min.js')); } catch { painting = null; view.stack.textContent = 'Chart unavailable. Reload to retry.'; return; }
  const css = getComputedStyle(document.documentElement), on = view.cards.filter(c => !c.el.hidden && c.chartEl.clientWidth);
  for (const c of on) {
    if (!c.chart) {
      c.chart = echarts.init(c.chartEl);
      c.chart.group = 'usage'; // one zoom and one hover line for every chart (echarts.connect)
      c.chart.on('datazoom', () => { const z = c.chart.getOption().dataZoom[0]; zoom = { startValue: z.startValue, endValue: z.endValue }; spanText(); });
      (c.sized = new ResizeObserver(() => c.chart.resize())).observe(c.chartEl);
    }
    c.chart.setOption(option(c, view.extent, view.now, narrow || c === on[on.length - 1], css), true);
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
