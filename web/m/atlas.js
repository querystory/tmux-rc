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
// Range, average and the sessions-or-tools split are per browser; the goal is the daemon's.
const fleet = { average: '1d', by: 'session', metric: 'all' };
let chosenRange = null;
try { Object.assign(fleet, JSON.parse(localStorage.getItem('tmuxrc-fleet'))); chosenRange = localStorage.getItem('tmuxrc-fleet-range'); } catch {}
// The range is the viewer's explicit choice, else a week where the screen has room to keep
// a week of bars legible (336 half-hour bars), else a day. Stored apart so that saving any
// other setting never turns the default into a choice.
fleet.range = RANGES.includes(chosenRange) ? chosenRange : matchMedia('(min-width: 1400px)').matches ? '7d' : '24h';
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
    const { range, ...kept } = fleet;
    localStorage.setItem('tmuxrc-fleet', JSON.stringify(kept));
    if ('range' in patch) localStorage.setItem('tmuxrc-fleet-range', range);
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
// Split contiguous groups near half the pane count; stack when columns get narrow.
// Nested grids fill the rectangle while retaining session and keyboard order.
function packSessions(groups, width) {
  if (groups.length === 1) return groups[0].node;
  const total = groups.reduce((sum, group) => sum + group.weight, 0);
  let split = 1, weight = groups[0].weight, best = Math.abs(total / 2 - weight);
  for (let i = 1, sum = weight; i < groups.length - 1; i++) {
    sum += groups[i].weight;
    const distance = Math.abs(total / 2 - sum);
    if (distance < best) { best = distance; split = i + 1; weight = sum; }
  }
  const columns = width >= Math.min(groups.length, 4) * 180;
  const branch = el('div', `atlas-split ${columns ? 'atlas-split-columns' : 'atlas-split-rows'}`);
  const fraction = Math.max(160 / width, Math.min(1 - 160 / width, weight / total));
  if (columns) branch.style.gridTemplateColumns = `minmax(0, ${fraction}fr) minmax(0, ${1 - fraction}fr)`;
  branch.append(packSessions(groups.slice(0, split), columns ? (width - 12) * fraction : width),
    packSessions(groups.slice(split), columns ? (width - 12) * (1 - fraction) : width));
  return branch;
}

// One scope of the history: the whole fleet, or one tool's and/or one session's panes.
function scoped(scope) {
  if (!scope.tool && !scope.session) return history;
  return history.map(s => {
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

// Chart rows: one metric's counts over the chosen range, each with the trailing average of
// its running count. The average covers observed buckets only, and only where its whole
// window is on record, so a young database draws no line rather than a guess.
function rows(samples) {
  const span = ms(fleet.average), step = historyData?.step || 60000, first = historyData?.first ?? Infinity;
  // Older history has no background counts; under 'all' those buckets are gaps, not panes alone.
  const counts = samples.map(s => fleet.metric === 'panes' ? s.n : s.n && sum([s.n, s.background]));
  let oldest = 0, total = 0, seen = 0;
  const out = samples.map((s, i) => {
    if (counts[i]) { total += running(counts[i]); seen++; }
    for (; samples[oldest].t <= s.t - span; oldest++) if (counts[oldest]) { total -= running(counts[oldest]); seen--; }
    return { ...s, n: counts[i], ma: seen && s.t - span + step >= first ? total / seen : null };
  });
  const from = (samples.length ? samples[samples.length - 1].t : 0) - ms(fleet.range);
  return out.filter(r => r.t > from);
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
const rangeControl = () => segmented('History range', RANGES, fleet.range, range => setFleet({ range }, true));
function averageControl() {
  const control = el('span', 'fleet-control');
  control.append(el('span', 'fleet-line-key', 'Avg'),
    segmented('Moving average of the running count', AVERAGES, fleet.average, average => setFleet({ average }, true)));
  return control;
}

// "Goal 12" until clicked, then a stepper. The goal lives in the daemon, so every device
// draws the same line. Every part carries the key 'goal' so focus follows the swap.
let goalFailed = false;
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
    button.onclick = () => { draft = String(goal ?? ''); setFleet({}); };
    return button;
  }
  const box = el('span', 'fleet-goal editing');
  const input = el('input');
  Object.assign(input, { type: 'number', min: 1, max: 999, value: draft, placeholder: 'none' });
  input.setAttribute('aria-label', 'Running goal. Empty clears it.');
  input.setAttribute('aria-invalid', String(goalFailed));
  input.title = goalFailed ? 'Could not save the goal. Try again.' : '';
  input.dataset.key = 'goal';
  input.oninput = () => { draft = input.value; };
  // Half-typed input ("-", "1e") reads as empty; only a truly empty box clears the goal.
  const submit = () => { if (input.reportValidity()) saveGoal(); };
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
  const nudge = delta => () => { input.value = draft = String(Math.max(1, Math.min(999, (Number(input.value) || 0) + delta))); };
  box.append(el('span', '', 'Running goal'), button('minus', 'Lower the goal', nudge(-1)), input,
    button('plus', 'Raise the goal', nudge(1)), button('check', 'Save the goal', submit));
  return box;
}

// Replace a surface's contents without dropping keyboard focus: the node with the same
// data-key gets it back. Callers capture the key before building, since building can move
// a live node (a chart) out of the page.
const focusKey = root => root.contains(document.activeElement) ? document.activeElement?.dataset.key : null;
function rebuild(root, nodes, focus) {
  root.replaceChildren(...nodes);
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
  if (unchanged(root, [open, settings, history, historyData, historyError, goal, ...now])) return;
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

export function renderAtlas(root, panes, navigate, logos, searchTopic = () => {}, icon) {
  const allPanes = panes;
  const scope = root._scope ||= { tool: '', session: '' };
  const tools = [...new Set(['claude', 'codex', 'shell', ...allPanes.map(toolOf), ...history.flatMap(s => s.groups.map(g => g.tool))].filter(isAgent))];
  const sessions = [...new Set([...allPanes.map(p => p.session), ...history.flatMap(s => s.groups.map(g => g.session))].filter(Boolean))].sort();
  panes = allPanes.filter(p => (!scope.tool || toolOf(p) === scope.tool) && (!scope.session || p.session === scope.session));
  // Preserve focus and pointer targets across unchanged long polls.
  if (unchanged(root, [scope.tool, scope.session, history, historyData, historyError, settings, goal, ...allPanes.flatMap(p => [p.pane_id, p.session, paneName(p), p.activity,
    p.waiting_on, p.tool, p.model, p.session_summary, p.status_line, p.agents, JSON.stringify(p.subagents)])])) return;
  const focus = focusKey(root);
  const charts = root._charts ||= atlasCharts();
  const main = root._main ||= fleetChart(true);
  root._mapResize?.disconnect();
  const nodes = [];
  const controls = el('div', 'atlas-controls');
  const redraw = () => { root._signature = null; renderAtlas(root, allPanes, navigate, logos, searchTopic, icon); };
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
    button.onclick = () => { scope.tool = tool; redraw(); };
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
  heading.append(el('h3', '', 'Activity over time'), now, averageText(data), info, el('span', 'fleet-gap'),
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
  nodes.push(pulse);

  // Small multiples: which session or tool carries the running count, all on one scale.
  const by = fleet.by, keys = by === 'session' ? sessions : tools;
  const multiples = el('section', 'atlas-panel atlas-multiples');
  const multiplesHeading = el('div', 'atlas-panel-heading');
  multiplesHeading.append(el('h3', '', 'Running by'), segmented('Running by', ['session', 'tool'], by, value => setFleet({ by: value })),
    el('span', 'muted', 'Cards share one scale. Choose one to filter this page.'));
  const cards = root._cards ||= new Map();
  cards.forEach((chart, key) => { if (!keys.includes(key)) { chart.dispose(); cards.delete(key); } });
  const series = keys.map(key => [key, rows(scoped({ ...scope, [by]: key }))]);
  const stacked = n => STATES.reduce((total, state, i) => shownStates[state] === false ? total : total + (n[i] || 0), 0);
  // Folded, not spread: a long history across many sessions would pass Math.max too many arguments.
  const max = series.reduce((top, [, r]) => r.reduce((m, x) => Math.max(m, x.n ? stacked(x.n) : 0, x.ma || 0), top), 1);
  const grid = el('div', 'atlas-multiples-grid');
  // Charts paint once attached: a node moved out and back in one frame never reports a resize.
  const paints = [];
  series.forEach(([key, slice]) => {
    const chart = cards.get(key) || fleetChart(false);
    cards.set(key, chart);
    const cardScope = { ...scope, [by]: key }; // the same population the card's chart draws
    const members = allPanes.filter(p => toolOf(p) === (cardScope.tool || toolOf(p)) && p.session === (cardScope.session || p.session));
    const card = el('button', 'atlas-multiple');
    card.dataset.key = `card:${by}:${key}`;
    card.setAttribute('aria-pressed', String(scope[by] === key));
    const head = el('span', 'atlas-multiple-head');
    head.append(el('b', '', key), el('span', 'fleet-gap'), el('b', '', String(running(liveCounts(members)))), el('span', 'muted', ' running'));
    const avg = latestAverage(slice);
    card.append(head, chart.el, el('span', 'muted', avg == null ? 'No average yet' : `${fleet.average} avg ${avg.toFixed(1)}`));
    card.onclick = () => { scope[by] = scope[by] === key ? '' : key; redraw(); };
    grid.append(card);
    paints.push(() => chart.update(chartData(slice, { goal: null, max })));
  });
  multiples.append(multiplesHeading, grid);
  nodes.push(multiples);

  const legend = el('div', 'atlas-legend');
  STATES.forEach((label, i) => legend.append(el('span', `atlas-state s${i}`, `${panes.filter(p => stateOf(p) === i).length} ${label}`)));
  nodes.push(legend);

  const map = el('section', 'atlas-map');
  const groups = new Map();
  panes.forEach(p => { if (!groups.has(p.session)) groups.set(p.session, []); groups.get(p.session).push(p); });
  const islands = [];
  groups.forEach((members, session) => {
    const island = el('section', 'atlas-island');
    island.append(el('h3', '', session), el('p', 'muted', `${members.length} panes · ${members.filter(needsYou).length} need you`));
    const dots = el('div', 'atlas-dots');
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
    island.append(dots);
    islands.push({ node: island, weight: members.length + 2 });
  });
  // Attach tiles before the synchronous focus restoration below; ResizeObserver
  // runs later, after live updates would otherwise drop keyboard focus.
  map.append(...islands.map(island => island.node));
  nodes.push(map);
  if (!panes.length) nodes.push(el('p', 'muted', 'No current panes match these filters.'));
  const busy = panes.filter(p => stateOf(p) === 1).length, waiting = panes.filter(needsYou).length;
  nodes.push(el('p', 'atlas-insight muted', `${panes.length} panes across ${groups.size} sessions · ${panes.length ? Math.round(busy / panes.length * 100) : 0}% running · ${waiting} waiting for you`));

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
  nodes.push(topics);
  rebuild(root, nodes, focus);
  let mapWidth = 0;
  root._mapResize = new ResizeObserver(entries => {
    const width = Math.floor(entries[0].contentRect.width);
    if (!width || width === mapWidth || !islands.length) return;
    mapWidth = width;
    const focused = map.contains(document.activeElement) ? document.activeElement : null;
    map.replaceChildren(packSessions(islands, width));
    focused?.focus({ preventScroll: true });
  });
  root._mapResize.observe(map);
  paints.forEach(paint => paint());
  charts.update({ words: topWords.map(([word, members]) => [word, members.length]), selectWord: searchTopic });
  main.update(chartData(data, { zoomKey: JSON.stringify([fleet.range, fleet.average, scope]) }));
}
