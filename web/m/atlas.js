import { atlasCharts, fleetChart, latestAverage, shownStates } from './atlas-charts.js';
import { needsYou, isRunning, markWorking, modelProvider, paneName } from './pane-model.js';

const STATES = ['Needs you', 'Running', 'Idle', 'Unknown', 'Compacting', 'Waiting'];
const sum = rows => rows.some(row => row == null) ? null : STATES.map((_, i) => rows.reduce((n, row) => n + (row[i] || 0), 0));
const stateOf = (p) => needsYou(p) ? 0 : p.activity === 'compacting' ? 4 : p.activity === 'waiting' ? 5 : isRunning(p) ? 1 : p.activity === 'idle' ? 2 : 3;
const toolOf = p => p.tool || 'other';
// 'unknown' (classifier could not tell) and 'other' (no tool) are buckets, not agents to
// filter to: they still count under All, but get no chip of their own.
const isAgent = tool => tool && tool !== 'unknown' && tool !== 'other';
// The running count the goal is about, as the Running tab counts it (isRunning): Running,
// Compacting, and Waiting on external work.
const running = n => n[1] + (n[4] || 0) + (n[5] || 0);
const RANGES = ['24h', '3d', '7d', '30d'], AVERAGES = ['1d', '3d', '7d'];
// What every chart counts: each pane once plus its visible background agents (sub-agents,
// workers), or panes alone. One choice for every surface, so the strip, the split, the
// dashboard and the cards all count the same thing.
const METRICS = [['all', 'Panes + background agents'], ['panes', 'Panes only']];
const AGENT_TOOLS = new Set(['claude', 'codex', 'gemini', 'opencode', 'omp']); // history.py's AGENT_TOOLS

// Live counts by state in the chosen metric, as history.py's agent_counts records them: a
// worker's state like a pane's (its waits are external unless it says otherwise), and an
// agent's own count of busy workers covering any the parsed roster missed.
function liveCounts(panes) {
  const counts = STATES.map(() => 0);
  panes.forEach(p => {
    counts[stateOf(p)]++;
    // Omitted means none parsed (the agent's own count still applies); null means unmeasured.
    const subs = 'subagents' in p ? p.subagents : [];
    if (fleet.metric !== 'all' || !AGENT_TOOLS.has(p.tool) || !Array.isArray(subs)) return;
    const busy = counts[1] + counts[4];
    subs.forEach(a => { if (a && a.state !== 'done') counts[stateOf({ activity: a.state, waiting_on: a.waiting_on || 'external' })]++; });
    counts[1] += Math.max(0, (p.agents || 0) - (counts[1] + counts[4] - busy));
  });
  return counts;
}
const ms = span => parseInt(span, 10) * (span.endsWith('d') ? 86400000 : 3600000);
// Range and average are per browser; the goal is the daemon's.
const fleet = { average: '1d', metric: 'all' };
let chosenRange = null;
try { Object.assign(fleet, JSON.parse(localStorage.getItem('tmuxrc-fleet'))); chosenRange = localStorage.getItem('tmuxrc-fleet-range'); } catch {}
// The range is the viewer's explicit choice, else a week where the screen has room to keep
// a week of bars legible (336 half-hour bars), else a day. Stored apart so that saving any
// other setting never turns the default into a choice.
if (!RANGES.includes(chosenRange)) chosenRange = null;
const roomForAWeek = matchMedia('(min-width: 1400px)');
const defaultRange = () => chosenRange || (roomForAWeek.matches ? '7d' : '24h');
fleet.range = defaultRange();
if (!AVERAGES.includes(fleet.average)) fleet.average = '1d';
if (!METRICS.some(([value]) => value === fleet.metric)) fleet.metric = 'all';
let history = [], historyData = null, historyError = '', goal = null, draft = null;
let requestedAt = 0, controller, reloadHistory, changed = () => {}, request, settings = 0;

export async function refreshAtlasHistory(fetcher, onChange, force = false) {
  request = fetcher; changed = onChange;
  reloadHistory = () => refreshAtlasHistory(fetcher, onChange, true);
  if (!force && Date.now() - requestedAt < 30000) return;
  requestedAt = Date.now();
  controller?.abort();
  const current = controller = new AbortController();
  try {
    // `lead` fetches one average's worth of history before the range, at the same bucket size.
    const data = await request(`/api/history?window=${fleet.range}&lead=${fleet.average}`, { signal: current.signal });
    if (current.signal.aborted) return;
    historyData = data;
    history = data.samples;
    goal = data.goal;
    historyError = '';
  } catch {
    if (current.signal.aborted) return;
    historyError = 'History unavailable. Retrying automatically.';
  }
  changed();
}

// Every fleet setting change bumps `settings`, which every render signature includes.
function setFleet(patch, refetch = false) {
  Object.assign(fleet, patch);
  settings++;
  try {
    const { range: _, ...kept } = fleet; // the range is stored only as an explicit choice
    localStorage.setItem('tmuxrc-fleet', JSON.stringify(kept));
    if (chosenRange) localStorage.setItem('tmuxrc-fleet-range', chosenRange);
  } catch {}
  if (refetch) { history = []; historyData = null; historyError = ''; reloadHistory?.(); }
  changed();
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}
// One scope of the history: the whole fleet, or one tool's and/or one session's panes.
function scoped(scope, samples = history) {
  if (!scope.tool && !scope.session) return samples;
  return samples.map(s => {
    if (s.n === null) return s;
    const groups = (s.groups || []).filter(g => (!scope.tool || g.tool === scope.tool) && (!scope.session || g.session === scope.session));
    // A partial log reconstruction cannot prove a filtered fleet was empty. Keep
    // its timestamp as a gap, including at either end of the selected range.
    if (!s.groups || (s.source === 'logs' && !groups.length)) return { ...s, n: null, groups: [], source: 'gap' };
    return { ...s, groups, n: groups.reduce((counts, g) => counts.map((n, i) => n + (g.n[i] || 0)), STATES.map(() => 0)),
      foreground: s.foreground == null && !groups.length ? null : sum(groups.map(g => g.foreground)),
      background: s.background == null && !groups.length ? null : sum(groups.map(g => g.background)) };
  });
}

// A sample's (or group's) counts in the chosen metric. Older history has no background
// counts; under 'all' those buckets are gaps, not panes alone.
const countsOf = s => fleet.metric === 'panes' ? s.n : s.n && sum([s.n, s.background]);

// Chart rows: one metric's counts over a range (the chosen one by default), each with the
// trailing average of its running count. The average covers observed buckets only, and only
// where its whole window is on record, so a young database draws no line rather than a guess.
function rows(samples, range = fleet.range, step = historyData?.step || 60000) {
  const span = ms(fleet.average), first = historyData?.first ?? Infinity;
  const counts = samples.map(countsOf);
  let oldest = 0, total = 0, seen = 0;
  const out = samples.map((s, i) => {
    if (counts[i]) { total += running(counts[i]); seen++; }
    for (; samples[oldest].t <= s.t - span; oldest++) if (counts[oldest]) { total -= running(counts[oldest]); seen--; }
    return { ...s, n: counts[i], ma: seen && s.t - span + step >= first ? total / seen : null };
  });
  const from = (samples.length ? samples[samples.length - 1].t : 0) - ms(range);
  return out.filter(r => r.t > from);
}
// The heatmap pages back through history two weeks at a time, at 15-minute buckets: a 24h
// window with a 15d lead gets those from the query, where a long window buckets far coarser.
// The oldest day a chunk cuts into is held back, and the median wants 14 whole days even
// across a DST fall-back (361 hours), so the lead is a day over two weeks.
// `before` ends a chunk where the loaded days begin. The newest chunk is refetched at most
// every 15 minutes, and only while a heatmap is drawn.
let heat = null, heatAt = 0, heatLoading = false;
function loadHeat(older) {
  if (!request || heatLoading) return;
  heatLoading = true;
  const before = older && heat.samples[0].t;
  request(`/api/history?window=24h&lead=15d${older ? `&before=${before}` : ''}`).then(data => {
    const fresh = older ? data.samples.filter(s => s.t < before) : data.samples, start = fresh[0]?.t ?? Infinity;
    const kept = heat ? heat.samples.filter(s => older || s.t < start) : [];
    heat = { step: data.step, now: older ? heat.now : data.now, samples: older ? [...fresh, ...kept] : [...kept, ...fresh], done: !fresh.length || start <= data.first || (!older && !!heat?.done) };
  }, () => {
    // A failed older page retries in 10s: a fresh `heat` re-renders the view, whose foot re-arms.
    if (older) setTimeout(() => { heat = { ...heat }; changed(); }, 10000);
    else heat ||= false;
  }).finally(() => { heatLoading = false; changed(); });
}
function refreshHeat() {
  if (!request || heatLoading || Date.now() - heatAt < 900000) return;
  heatAt = Date.now();
  loadHeat(false);
}
const pad = h => String(h).padStart(2, '0');
const shade = (v, low, top) => v < 0.05 ? 'var(--raised)' : `color-mix(in oklab, var(--chart-s1) ${Math.round(12 + 88 * Math.max(0, Math.min(1, (v - low) / (top - low))))}%, var(--surface))`;
const fig = (value, label) => { const node = el('span', 'heat-fig'); node.append(el('b', '', value), el('span', 'muted', label)); return node; };
// The metric's population, as the activity chart's unit names it: every pane, shells too, plus agents.
const heatUnit = () => fleet.metric === 'panes' ? 'pane-hours' : 'pane+agent hours';
const heatWaiting = () => el('p', 'muted', heat === false ? 'History unavailable. Retrying automatically.' : 'Loading saved history…');
// Local days, today first, each 24 hours. A cell sums its buckets' running counts times the
// time each covers (the newest only up to the server's now): the running hours recorded in that hour, so a
// DST fall-back hour holds both of its hours and a half-observed hour counts what was seen.
// A day's total sums its cells.
function heatDays(scope) {
  const days = new Map();
  scoped(scope, heat.samples).forEach(s => {
    const t = new Date(s.t), day = new Date(s.t).setHours(0, 0, 0, 0), n = countsOf(s);
    if (!days.has(day)) days.set(day, []);
    if (!n) return;
    const cell = days.get(day)[t.getHours()] ||= { sum: 0, tools: {} };
    const hours = Math.max(0, Math.min(heat.step, heat.now - s.t)) / 3600000; // the newest bucket ends at the query's now
    cell.sum += running(n) * hours;
    (s.groups || []).forEach(g => { const c = countsOf(g); if (c) cell.tools[g.tool] = (cell.tools[g.tool] || 0) + running(c) * hours; });
  });
  // Until history runs out, the oldest day loaded may be cut by the chunk; it waits for the next.
  const all = [...days].reverse().slice(0, heat.done ? undefined : -1), value = c => c.sum;
  // One scale for the card and the view, from the 3rd to the 97th percentile of the latest
  // two weeks (the first chunk): a fleet that never idles still shows its rhythm, a spike does
  // not wash it out, and loading older days never recolours the newer ones.
  const values = all.slice(0, 14).flatMap(([, cells]) => cells.filter(Boolean).map(value)).sort((a, b) => a - b);
  const low = values[Math.floor(values.length * 0.03)] || 0, top = Math.max(low + 1, values[Math.floor(values.length * 0.97)] || 0);
  const totals = all.map(([, cells]) => cells.reduce((t, c) => t + (c ? value(c) : 0), 0));
  return { all, value, low, top, totals, most: totals.reduce((m, t) => Math.max(m, t), 0) };
}
const today = () => new Date().setHours(0, 0, 0, 0);
// A month's first day names its month, so a long scroll back stays anchored.
const dayName = day => day === today() ? 'Today' : new Date(day).toLocaleDateString([], new Date(day).getDate() === 1 ? { month: 'short', day: 'numeric' } : { weekday: 'short' })
  + (new Date(day).getDate() === 1 ? '' : ` ${new Date(day).getDate()}`);
// Hour of day across, days down, each row ending in its total as a meter, as the plan-usage
// meters draw a window. The card's compact grid is labelled every 12 hours, the view's every 6.
function heatGrid({ all, value, low, top, totals, most }, days, compact) {
  const unit = heatUnit(), grid = el('div', compact ? 'heat-grid heat-compact' : 'heat-grid');
  grid.setAttribute('role', compact ? 'img' : 'group');
  grid.setAttribute('aria-label', `Running ${unit} by hour for ${days} days; the busiest had ${Math.round(most)}.${compact ? '' : ' Arrow keys move between hours.'}`);
  grid.append(el('span'), ...Array.from({ length: 24 }, (_, h) => el('span', 'heat-hour', h % (compact ? 12 : 6) ? '' : `${h % 12 || 12}${h < 12 ? 'a' : 'p'}`)),
    el('span', 'heat-hour heat-total', compact ? '' : 'Hours'));
  all.slice(0, days).forEach(([day, cells], i) => {
    grid.append(el('span', day === today() ? 'heat-day today' : 'heat-day', dayName(day)));
    for (let h = 0; h < 24; h++) {
      // An hour a DST spring-forward skips is no hour at all; later today is not one yet.
      const c = cells[h], slot = new Date(day), at = slot.setHours(h), real = slot.getHours() === h;
      const cell = el('span', c ? 'heat-cell' : !real ? 'heat-cell heat-skip' : at > Date.now() ? 'heat-cell heat-future' : 'heat-cell heat-gap');
      if (c) cell.style.background = shade(value(c), low, top);
      if (c || (real && at <= Date.now())) cell.hour = [day, h, c];
      grid.append(cell);
    }
    const total = el('span', 'heat-total'), bar = el('i', 'usage-bar'), fill = el('i');
    fill.style.width = `${totals[i] / (most || 1) * 100}%`;
    bar.append(fill);
    total.append(bar, el('b', '', `${Math.round(totals[i])}h`));
    grid.append(total);
  });
  return grid;
}

// A dashboard card that opens its own view (#view=<page>, app.js): a heading ending in
// Details ›, and anywhere on the card opens it too. `panel` is the card to head (an
// existing one, or a new section); the view starts with pageHead.
export function detailCard(panel, title, sub, open) {
  const heading = el('div', 'atlas-panel-heading'), details = el('button', 'atlas-reset-zoom', 'Details ›');
  details.dataset.key = `details:${title}`;
  panel.classList.add('atlas-panel', 'atlas-detail-card');
  panel.onclick = open;
  heading.append(el('h3', '', title), el('span', 'muted', sub), el('span', 'fleet-gap'), details);
  panel.prepend(heading);
  return panel;
}
export function pageHead(title, icon, back) {
  const head = el('div', 'landing-head'), button = el('button', 'icon-button');
  button.innerHTML = icon('back', 18);
  button.setAttribute('aria-label', 'Back to the Dashboard');
  button.onclick = back;
  head.append(button, el('h2', '', title));
  return head;
}

// The Dashboard's heatmap card: the last week, compact.
function heatCard(scope, open) {
  const card = detailCard(el('section', 'atlas-heat-card'), 'Running by hour', `last 7 days · ${heatUnit()}`, open);
  card.append(heat ? heatGrid(heatDays(scope), 7, true) : heatWaiting());
  return card;
}

// The full view (#view=heat): headline figures, then every day loaded, newest first; older
// days load as the foot scrolls into sight, back to the start of saved history.
export function renderHeatView(root, scope, icon, back) {
  refreshHeat();
  if (unchanged(root, [heat, settings, scope.tool, scope.session])) return;
  const focus = focusKey(root); // a page of older days keeps the keyboard on the grid
  root._older?.disconnect();
  const head = pageHead('Running by hour', icon, back), panel = el('section', 'atlas-panel atlas-heat');
  if (!heat) return rebuild(root, [head, heatWaiting()], focus);
  const data = heatDays(scope), { all, totals, most, low, top } = data, unit = heatUnit();
  const isToday = all[0]?.[0] === today(), done = totals.slice(isToday ? 1 : 0, isToday ? 15 : 14).sort((a, b) => a - b);
  const half = done.length / 2, median = done.length ? (done[Math.floor(half)] + done[Math.ceil(half) - 1]) / 2 : 0;
  const figs = el('div', 'heat-figs'), legend = el('div', 'heat-legend muted');
  figs.append(fig(`${Math.round(isToday ? totals[0] : 0)}h`, 'today so far'), fig(`${Math.round(median)}h`, 'typical day (median, 14d)'),
    fig(`${Math.round(most)}h`, most ? `busiest, ${new Date(all[totals.indexOf(most)][0]).toLocaleDateString([], { month: 'short', day: 'numeric' })}` : 'busiest'));
  const key = (...parts) => { const node = el('span'); node.append(...parts); return node; }; // a swatch never wraps from its label
  legend.append(key(`${unit} an hour: ${+low.toFixed(1)}`, el('i', 'heat-ramp'), `${+top.toFixed(1)}+`),
    key(el('i', 'heat-cell heat-gap'), 'not recorded'), key(el('i', 'heat-cell heat-future'), 'still to come'));
  const grid = heatGrid(data, all.length, false);
  // Each hour on record is a button labelled with its detail, one tab stop for the grid (the
  // arrow keys move it), and one tip shows the detail of the cell a pointer, tap or focus is on.
  const tip = el('div', 'heat-tip'), cells = [...grid.querySelectorAll('.heat-cell')];
  const describe = ([day, h, c]) => {
    const tools = c ? Object.entries(c.tools).filter(([, n]) => n >= 0.05).sort((x, y) => y[1] - x[1]) : [];
    return [`${new Date(day).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })} · ${pad(h)}:00–${pad(h + 1)}:00`,
      c ? `${c.sum.toFixed(1)} ${unit}` : 'Not recorded', ...tools.map(([tool, n]) => `${tool} ${n.toFixed(1)}`)];
  };
  cells.forEach(cell => {
    if (!cell.hour) return;
    Object.assign(cell, { tabIndex: -1, role: 'button' });
    cell.dataset.key = `heat:${cell.hour[0]}:${cell.hour[1]}`; // paging older days keeps focus here
    cell.setAttribute('aria-label', describe(cell.hour).join(', '));
  });
  const first = cells.find(cell => cell.hour);
  if (first) first.tabIndex = 0;
  tip.hidden = true;
  const show = cell => {
    grid.querySelector('.sel')?.classList.remove('sel');
    if ((tip.hidden = !cell?.hour)) return;
    cell.classList.add('sel');
    tip.textContent = describe(cell.hour).join('\n');
    tip.style.top = `${cell.offsetTop + cell.offsetHeight + 4}px`;
    tip.style.left = `${Math.max(0, Math.min(grid.clientWidth - tip.offsetWidth, cell.offsetLeft - tip.offsetWidth / 2))}px`;
  };
  grid.onclick = grid.onpointerover = event => show(event.target);
  grid.addEventListener('focusin', event => show(event.target)); // no onfocusin property
  grid.onpointerleave = event => { if (event.pointerType === 'mouse') show(null); };
  grid.onkeydown = event => {
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -24, ArrowDown: 24 }[event.key];
    let i = cells.indexOf(event.target);
    if (!step || i < 0) return;
    event.preventDefault();
    do i += step; while (cells[i] && !cells[i].hour); // over hours not yet, or skipped by DST
    cells[i]?.focus();
  };
  grid.append(tip);
  const foot = el('p', 'muted heat-foot', heat.done ? 'Start of saved history' : 'Loading older days…');
  panel.append(figs, legend, grid, foot);
  rebuild(root, [head, panel], focus);
  if (heat.done) return;
  // aria-busy until the load re-renders the view with a new foot.
  root._older = new IntersectionObserver(entries => { if (entries[0].isIntersecting) { foot.setAttribute('aria-busy', 'true'); loadHeat(true); } }, { rootMargin: '400px' });
  root._older.observe(foot);
}

const chartData = (data, extra) => ({ rows: data, states: STATES, step: historyData?.step || 60000, goal,
  unit: fleet.metric === 'panes' ? 'Panes' : 'Panes + agents',
  average: fleet.average, zoomKey: JSON.stringify([fleet.range, fleet.average]), ...extra });

function averageText(data, target = goal) {
  const avg = latestAverage(data);
  if (historyError || !historyData) return el('span', 'fleet-average muted', historyError || 'Loading saved history…');
  if (avg == null) return el('span', 'fleet-average muted', `Not enough history yet for a ${fleet.average} average`);
  const text = el('span', `fleet-average${target ? avg >= target ? ' pos' : ' neg' : ''}`);
  text.append(el('span', 'muted', `${fleet.average} avg `), el('b', '', avg.toFixed(1)), el('span', 'muted', target ? ` vs goal ${target}` : ''));
  return text;
}

function segmented(label, options, value, pick) {
  const group = el('span', 'fleet-seg');
  group.setAttribute('role', 'group');
  group.setAttribute('aria-label', label);
  options.forEach(option => {
    const button = el('button', '', option);
    button.dataset.key = `${label}:${option}`;
    button.setAttribute('aria-pressed', String(option === value));
    button.onclick = () => pick(option);
    group.append(button);
  });
  return group;
}
const rangeControl = () => segmented('History range', RANGES, fleet.range, range => { chosenRange = range; setFleet({ range }, true); });
// Until the viewer picks one, the range follows the screen across the breakpoint.
const followScreen = () => { if (defaultRange() !== fleet.range) setFleet({ range: defaultRange() }, true); };
if (roomForAWeek.addEventListener) roomForAWeek.addEventListener('change', followScreen);
else roomForAWeek.addListener?.(followScreen); // older iOS Safari, as app.js does for WIDE
function averageControl() {
  const control = el('span', 'fleet-control');
  control.append(el('span', 'fleet-line-key', 'Avg'),
    segmented('Moving average of the running count', AVERAGES, fleet.average, average => setFleet({ average }, true)));
  return control;
}

// "Goal 12" until clicked, then a stepper. The goal lives in the daemon, so every device
// draws the same line. Every part carries the key 'goal' so focus follows the swap.
let goalFailed = false, draftInvalid = false; // the latter survives an editor rebuild, which empties the box
let saving = false;
async function saveGoal() {
  if (saving) return; // one PUT at a time, so an older save can never land last
  saving = true;
  const sent = draft, value = sent === '' ? null : Math.round(Number(sent));
  try {
    ({ goal } = await request('/api/history/goal', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ goal: value }) }));
    goalFailed = false;
    if (draft === sent) draft = null; // an edit made while this saved stays open for its own save
    reloadHistory?.(); // supersedes a GET that read the old goal before this save
  } catch { goalFailed = true; }
  saving = false;
  setFleet({});
}
function goalControl(icon) {
  if (draft == null) {
    const button = el('button', 'fleet-goal');
    button.dataset.key = 'goal';
    button.title = 'Change the running goal';
    button.innerHTML = `${icon('target', 14)}<span></span>${icon('pencil', 12)}`;
    button.querySelector('span').textContent = goal == null ? 'Set goal' : 'Goal ';
    if (goal != null) button.querySelector('span').append(el('b', '', String(goal)));
    button.onclick = () => { draft = String(goal ?? ''); draftInvalid = false; setFleet({}); };
    return button;
  }
  const box = el('span', 'fleet-goal editing');
  const input = el('input');
  Object.assign(input, { type: 'number', min: 1, max: 999, value: draft, placeholder: 'none' });
  input.setAttribute('aria-label', 'Running goal. Empty clears it.');
  input.setAttribute('aria-invalid', String(goalFailed || draftInvalid));
  input.title = goalFailed ? 'Could not save the goal. Try again.' : '';
  input.dataset.key = 'goal';
  input.oninput = () => { draft = input.value; draftInvalid = input.validity.badInput; };
  // Half-typed input ("-", "1e") reads as empty; only a truly empty box clears the goal.
  const submit = () => { if (!draftInvalid && input.reportValidity()) saveGoal(); else input.setAttribute('aria-invalid', 'true'); };
  input.onkeydown = event => { if (event.key === 'Enter') submit(); };
  box.onkeydown = event => { if (event.key === 'Escape') { draft = null; goalFailed = false; setFleet({}); } };
  const button = (name, label, onclick) => {
    const node = el('button', 'icon-button');
    node.innerHTML = icon(name, 14);
    node.setAttribute('aria-label', label);
    node.title = label;
    node.dataset.key = name === 'check' ? 'goal' : `goal:${name}`;
    node.onclick = onclick;
    return node;
  };
  const nudge = delta => () => { draftInvalid = false; input.value = draft = String(Math.max(1, Math.min(999, (Number(input.value) || 0) + delta))); };
  box.append(el('span', '', 'Running goal'), button('minus', 'Lower the goal', nudge(-1)), input,
    button('plus', 'Raise the goal', nudge(1)), button('check', 'Save the goal', submit));
  return box;
}

// Replace a surface's contents without dropping keyboard focus: the node with the same
// data-key gets it back. Callers capture the key before building, since building can move
// a live node (a chart) out of the page.
const focusKey = root => root.contains(document.activeElement) ? document.activeElement?.dataset.key : null;
// Nodes kept across rebuilds (the caller's `after`) stay attached rather than being pulled out
// and put back: Chrome anchors scrolling on a focused node, and one detached and reinserted
// scrolled the page away from it.
function rebuild(root, nodes, focus) {
  [...root.children].forEach(node => nodes.includes(node) || node.remove());
  nodes.forEach((node, i) => { if (root.children[i] !== node) root.insertBefore(node, root.children[i] || null); });
  if (focus) [...root.querySelectorAll('[data-key]')].find(n => n.dataset.key === focus)?.focus({ preventScroll: true });
}
const unchanged = (root, signature) => {
  if (root._signature?.length === signature.length && signature.every((value, i) => value === root._signature[i])) return true;
  root._signature = signature;
  return false;
};

// The split under an open pane on a wide screen: a strip, and, once its seam is pulled
// up, the chart with its legend. The seam itself belongs to the layout (app.js).
// Reset zoom, shown only while the chart is zoomed; it hands focus back to the chart.
function resetZoomButton(chart) {
  const reset = el('button', 'atlas-reset-zoom', 'Reset zoom');
  reset.dataset.key = 'reset-zoom';
  reset.onclick = () => { chart.resetZoom(); chart.el.focus({ preventScroll: true }); };
  chart.onZoom = () => { reset.hidden = !chart.zoomed(); };
  chart.onZoom();
  return reset;
}

// Legend chips that show or hide a state's layer in every fleet chart.
function stateChip(state, n) {
  const i = STATES.indexOf(state), node = el('button', 'fleet-chip');
  node.dataset.key = `layer:${state}`;
  node.title = `Show or hide ${state.toLowerCase()} in the chart`;
  node.style.setProperty('--state-color', `var(--chart-s${i})`);
  node.setAttribute('aria-pressed', String(shownStates[state] !== false));
  node.append(el('i', 'fleet-dot'), state, el('b', '', String(n)));
  node.onclick = () => { shownStates[state] = shownStates[state] === false; setFleet({}); };
  return node;
}

// The split under an open pane on a wide screen: one row (running now, the average, the
// goal; then, unfolded, the chart's own controls; Dashboard last) over the chart once its
// seam is pulled up. The seam itself belongs to the layout (app.js).
export function renderFleet(root, panes, { open, toggle, dashboard, icon }) {
  // The header counts everything running; each chip counts only the layer it toggles.
  const counts = liveCounts(panes), now = [running(counts), counts[0], counts[2], counts[1]];
  if (unchanged(root, [open, settings, history, historyData, historyError, goal, draft, draftInvalid, ...now])) return;
  const focus = focusKey(root);
  const spark = root._spark ||= fleetChart(false), chart = root._chart ||= fleetChart(true);
  const data = rows(history);
  const strip = el('div', open ? 'fleet-strip open' : 'fleet-strip');
  const fold = el('button', 'icon-button');
  fold.innerHTML = icon(open ? 'chevronDown' : 'chevronUp', 18);
  fold.dataset.key = 'fold';
  fold.setAttribute('aria-expanded', String(open));
  fold.setAttribute('aria-label', open ? 'Fold the fleet chart' : 'Unfold the fleet chart');
  fold.onclick = toggle;
  const count = el('span', 'fleet-now');
  count.append(el('b', '', String(now[0])), el('span', 'muted', ' running'));
  const board = el('button', 'fleet-chip');
  board.dataset.key = 'dashboard';
  board.innerHTML = `${icon('layers', 14)}<span>Dashboard</span>`;
  board.onclick = dashboard;
  strip.append(fold, count, averageText(data), goalControl(icon), ...(open
    ? [el('span', 'fleet-gap'), stateChip('Running', now[3]), stateChip('Needs you', now[1]), stateChip('Idle', now[2]),
      resetZoomButton(chart), averageControl(), rangeControl()]
    : [spark.el]), board);
  rebuild(root, open ? [strip, chart.el] : [strip], focus);
  spark.update(chartData(data));
  chart.update(chartData(data));
}

const STOP = new Set(('the a an and or to of in on for with from is are was were be been being this that it its as at by has have had not no into about after before all can will would should could their they them then than also currently successfully session agent task work working focused completed using updated implementation changes implemented new current which but while other now ready identified verified three two one these those there here more only already still through when where what how our your you we may any each both same').split(' '));

// `after` is the caller's nodes to place under the big chart (app.js: the row of Open loops
// and heatmap cards, then Plan usage); `foot`, the node in that row that gets the heatmap
// card, and `openHeat` opens the heatmap's own view.
export function renderAtlas(root, panes, navigate, logos, searchTopic = () => {}, icon, after, foot, openHeat) {
  const allPanes = panes;
  const scope = root._scope ||= { tool: '', session: '' };
  const tools = [...new Set(['claude', 'codex', 'shell', ...allPanes.map(toolOf), ...history.flatMap(s => s.groups.map(g => g.tool))].filter(isAgent))];
  const sessions = [...new Set([...allPanes.map(p => p.session), ...history.flatMap(s => s.groups.map(g => g.session))].filter(Boolean))].sort();
  panes = allPanes.filter(p => (!scope.tool || toolOf(p) === scope.tool) && (!scope.session || p.session === scope.session));
  // Preserve focus and pointer targets across unchanged long polls.
  // draft too: the goal editor is shared, so an edit on one surface refreshes the other.
  refreshHeat();
  if (unchanged(root, [heat, scope.tool, scope.session, history, historyData, historyError, settings, goal, draft, draftInvalid, ...allPanes.flatMap(p => [p.pane_id, p.session, paneName(p), p.activity,
    p.waiting_on, p.tool, p.model, p.session_summary, p.status_line, p.agents, JSON.stringify(p.subagents)])])) return;
  const focus = focusKey(root);
  const charts = root._charts ||= atlasCharts();
  const main = root._main ||= fleetChart(true);
  root._resize?.disconnect();
  const nodes = [];
  const controls = el('div', 'atlas-controls');
  controls.append(el('span', 'atlas-controls-label muted', 'Sessions'));
  const redraw = () => { root._signature = null; renderAtlas(root, allPanes, navigate, logos, searchTopic, icon, after, foot, openHeat); };
  ['', ...tools, ...(scope.tool && !tools.includes(scope.tool) ? [scope.tool] : [])].forEach(tool => {
    const count = allPanes.filter(p => (!tool || toolOf(p) === tool) && (!scope.session || p.session === scope.session)).length;
    const button = el('button', 'atlas-filter atlas-tool-filter');
    const label = `${tool || 'All tools'} · ${count} panes`;
    button.setAttribute('aria-label', label);
    button.title = label;
    if (tool) {
      const logo = el('img');
      logo.src = Object.prototype.hasOwnProperty.call(logos, tool) ? logos[tool] : '/tmux-logomark.svg';
      logo.alt = ''; logo.width = 22; logo.height = 22;
      button.append(logo);
    } else {
      button.append(el('span', '', 'All'));
    }
    button.append(el('span', 'count', String(count)));
    button.dataset.key = `tool:${tool}`;
    button.setAttribute('aria-pressed', String(scope.tool === tool));
    button.onclick = () => { scope.tool = tool; setFleet({}); }; // through app.js, whose Plan usage it filters too
    controls.append(button);
  });
  const sessionPicker = el('select', 'atlas-session-picker');
  sessionPicker.dataset.key = 'session-filter';
  sessionPicker.setAttribute('aria-label', 'Filter atlas by tmux session');
  sessionPicker.append(new Option('All tmux sessions', ''));
  [...new Set([...sessions, ...(scope.session ? [scope.session] : [])])].forEach(session => sessionPicker.append(new Option(session, session)));
  sessionPicker.value = scope.session;
  sessionPicker.onchange = () => { scope.session = sessionPicker.value; redraw(); };
  controls.append(sessionPicker); nodes.push(controls);

  // The big chart leads the page: the fleet over time, against the goal.
  const samples = scoped(scope), data = rows(samples);
  // One header row: title and current numbers on the left, every control on the right; the
  // notes hide behind an info toggle so the plot starts right under the controls.
  const pulse = el('section', 'atlas-panel atlas-pulse');
  const heading = el('div', 'atlas-panel-heading');
  const now = el('span', 'fleet-now');
  now.append(el('b', '', String(running(liveCounts(panes)))), el('span', 'muted', ' running'));
  const info = el('button', 'icon-button atlas-info');
  info.innerHTML = icon('info', 16);
  info.dataset.key = 'history-info';
  info.setAttribute('aria-label', 'About this chart');
  info.title = 'About this chart';
  info.setAttribute('aria-expanded', String(!!root._info));
  info.onclick = () => { root._info = !root._info; redraw(); };
  const metricControls = el('select', 'atlas-range');
  metricControls.setAttribute('aria-label', 'History population');
  metricControls.dataset.key = 'history-metric';
  METRICS.forEach(([value, label]) => metricControls.append(new Option(label, value)));
  metricControls.value = fleet.metric;
  metricControls.onchange = () => setFleet({ metric: metricControls.value });
  // The goal is fleet-wide: a page filtered to one tool or session draws none.
  const target = scope.tool || scope.session ? null : goal;
  heading.append(el('h3', '', 'Activity over time'), now, averageText(data, target), info, el('span', 'fleet-gap'),
    metricControls, rangeControl(), averageControl(), goalControl(icon));
  pulse.append(heading);
  if (root._info) {
    const notes = el('div', 'atlas-history-note muted');
    [historyData ? 'Saved by this machine’s daemon. Gaps mean no observation. The purple line is the trailing average of Running; the dashed one is the goal.' : '',
      'Drag the handles under the chart to zoom; Ctrl + scroll zooms, and on touch, pinch zooms.',
      fleet.metric === 'panes' ? 'Counts each pane once. Older history grouped compacting and external waits under Running.'
        : 'Counts each pane once plus the background agents (sub-agents, workers) its agent shows; hidden workers may be missed, and this is not CPU or token use. Older history has no background counts, so it shows as gaps here; choose Panes only to see it.',
      data.some(s => s.source === 'logs') ? historyData.backfill_note : '',
      scope.session && history.some(s => s.source === 'logs') ? 'Older log records have no tmux session identity; they are excluded from this session filter.' : '',
    ].filter(Boolean).forEach(note => notes.append(el('p', '', note)));
    pulse.append(notes);
  }
  const toggles = el('div', 'atlas-state-legend');
  toggles.setAttribute('role', 'group');
  toggles.setAttribute('aria-label', 'Visible history states');
  STATES.forEach((state, i) => {
    const button = el('button', 'atlas-state-toggle', state);
    button.style.setProperty('--state-color', `var(--chart-s${i})`);
    button.title = `Show or hide ${state.toLowerCase()} history`;
    button.dataset.key = `history-state:${state}`;
    button.setAttribute('aria-pressed', String(shownStates[state] !== false));
    button.onclick = () => { shownStates[state] = shownStates[state] === false; setFleet({}); };
    toggles.append(button);
  });
  toggles.append(el('span', 'fleet-gap'), resetZoomButton(main));
  pulse.append(toggles, main.el);
  nodes.push(pulse, ...after);

  // One card per tmux session: its running count, who needs you, its trend on the shared
  // scale, and its panes by state underneath. Sessions with live panes come first.
  const live = new Set(panes.map(p => p.session)), keys = (scope.session ? [scope.session] : sessions).sort((a, b) => live.has(b) - live.has(a));
  const multiples = el('section', 'atlas-panel atlas-multiples');
  const cards = root._cards ||= new Map();
  cards.forEach((chart, key) => { if (!keys.includes(key)) { chart.dispose(); cards.delete(key); } });
  // Slices depend on history and settings, not on pane state, so live updates reuse them.
  const slices = root._slices ||= {};
  // A fixed week whatever the chart's range, from the heatmap's 15-minute history.
  if (!unchanged(slices, [heat, historyData, settings, scope.tool, scope.session, ...keys])) slices.series = keys.map(key => [key, heat ? rows(scoped({ ...scope, session: key }, heat.samples), '7d', heat.step) : []]);
  const { series } = slices;
  const stacked = n => STATES.reduce((total, state, i) => shownStates[state] === false ? total : total + (n[i] || 0), 0);
  // Folded, not spread: a long history across many sessions would pass Math.max too many arguments.
  const max = series.reduce((top, [, r]) => r.reduce((m, x) => Math.max(m, x.n ? stacked(x.n) : 0, x.ma || 0), top), 1);
  const grid = el('div', 'atlas-multiples-grid');
  // Charts paint once attached: a node moved out and back in one frame never reports a resize.
  const paints = [];
  series.forEach(([key, slice]) => {
    const chart = cards.get(key) || fleetChart(false);
    cards.set(key, chart);
    const members = panes.filter(p => p.session === key), need = members.filter(needsYou).length;
    const card = el('div', 'atlas-multiple'), head = el('span', 'atlas-multiple-head'), dots = el('div', 'atlas-dots');
    head.append(el('b', '', key), el('span', 'fleet-gap'), el('b', '', String(running(liveCounts(members)))), el('span', 'muted', ' running'),
      ...(need ? [el('span', 'atlas-need', `${need} need you`)] : []));
    members.forEach(p => {
      const dot = el('button', `atlas-dot s${stateOf(p)}`);
      dot.dataset.key = p.pane_id;
      dot.setAttribute('aria-label', [paneName(p), modelProvider(p)?.[0], STATES[stateOf(p)]].filter(Boolean).join(' · ')); // its label hides the badge's alt
      dot.title = `${paneName(p)}\n${STATES[stateOf(p)]}\n${p.session_summary || p.status_line || ''}`;
      const logo = el('img', 'atlas-agent-icon');
      logo.alt = p.tool || 'tmux';
      const agent = el('span', 'atlas-agent');
      agent.append(logo);
      markWorking(logo, p, logos);
      dot.append(agent, el('span', 'atlas-dot-name', paneName(p)));
      dot.onclick = () => navigate(p.pane_id);
      dots.append(dot);
    });
    const avg = latestAverage(slice);
    // A slice with nothing on record draws no chart rather than a tall blank one.
    card.append(head, ...(slice.some(r => r.n) ? [chart.el] : []), el('span', 'muted', avg == null ? 'No average yet' : `${fleet.average} avg ${avg.toFixed(1)}`),
      ...(members.length ? [dots] : []));
    grid.append(card);
    paints.push(() => chart.update(chartData(slice, { goal: null, max, step: heat?.step, zoomKey: '7d' })));
  });
  const legend = el('div', 'atlas-legend');
  STATES.forEach((label, i) => legend.append(el('span', `atlas-state s${i}`, `${panes.filter(p => stateOf(p) === i).length} ${label}`)));
  const sessionsHead = el('div', 'atlas-panel-heading');
  sessionsHead.append(el('h3', '', 'Sessions'));
  multiples.append(sessionsHead, grid, legend);
  nodes.push(multiples);
  if (!panes.length) nodes.push(el('p', 'muted', 'No current panes match these filters.'));

  const topics = el('section', 'atlas-panel atlas-topics');
  topics.append(el('h3', '', 'What’s on the radar'), el('p', 'muted', 'Click a word to search your sessions.'));
  const words = new Map();
  panes.forEach(p => {
    const tokens = new Set(`${paneName(p)} ${p.session_summary || p.status_line || ''}`.toLowerCase().match(/[\p{L}][\p{L}\p{N}-]{2,24}/gu) || []);
    tokens.forEach(w => { if (!STOP.has(w)) { if (!words.has(w)) words.set(w, []); words.get(w).push(p); } });
  });
  const topWords = [...words].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0])).slice(0, 40);
  // Canvas words are clickable; expose the same topics when navigating by keyboard.
  const topicLinks = el('div', 'atlas-topic-links');
  topWords.forEach(([word, members]) => {
    const button = el('button', '', `${word} · ${members.length} panes`);
    button.dataset.key = `topic:${word}`;
    button.onclick = () => searchTopic(word);
    topicLinks.append(button);
  });
  topics.append(charts.cloud, topicLinks);
  if (!words.size) topics.append(el('p', 'muted', 'Topics appear as panes acquire titles and summaries.'));
  // The heatmap card closes the page; like the slices, it is rebuilt only when its inputs change.
  if (!unchanged(foot, [heat, settings, scope.tool, scope.session])) foot.replaceChildren(heatCard(scope, openHeat));
  nodes.push(topics);
  rebuild(root, nodes, focus);
  root._resize = new ResizeObserver(entries => {
    const width = Math.floor(entries[0].contentRect.width);
    // Balanced rows of cards: as many columns as fit, then evened out so no row is an orphan.
    const fit = Math.max(1, Math.floor(width / 230)); // one card a row on a phone, its icons across
    grid.style.gridTemplateColumns = `repeat(${Math.ceil(series.length / Math.ceil(series.length / fit)) || 1}, minmax(0, 1fr))`;
  });
  root._resize.observe(grid);
  paints.forEach(paint => paint());
  charts.update({ words: topWords.map(([word, members]) => [word, members.length]), selectWord: searchTopic });
  main.update(chartData(data, { goal: target, zoomKey: JSON.stringify([fleet.range, fleet.average, scope]) }));
}
