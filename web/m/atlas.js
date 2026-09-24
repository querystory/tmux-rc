import { atlasCharts } from './atlas-charts.js';
import { needsYou, isRunning, paneName } from './pane-model.js';

const STATES = ['Needs you', 'Running', 'Idle', 'Unknown', 'Compacting', 'Waiting'];
const sum = rows => rows.some(row => row == null) ? null : STATES.map((_, i) => rows.reduce((n, row) => n + (row[i] || 0), 0));
const stateOf = (p) => needsYou(p) ? 0 : p.activity === 'compacting' ? 4 : p.activity === 'waiting' ? 5 : isRunning(p) ? 1 : p.activity === 'idle' ? 2 : 3;
const toolOf = p => p.tool || 'other';
let history = [], historyData = null, historyWindow = '24h', historyError = '';
let requestedAt = 0, controller, reloadHistory;

export async function refreshAtlasHistory(request, changed, force = false) {
  reloadHistory = () => refreshAtlasHistory(request, changed, true);
  if (!force && Date.now() - requestedAt < 30000) return;
  requestedAt = Date.now();
  controller?.abort();
  const current = controller = new AbortController();
  try {
    const data = await request(`/api/history?window=${historyWindow}`, { signal: current.signal });
    if (current.signal.aborted) return;
    historyData = data;
    history = data.samples;
    historyError = '';
  } catch {
    if (current.signal.aborted) return;
    historyError = 'History unavailable. Retrying automatically.';
  }
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

const STOP = new Set(('the a an and or to of in on for with from is are was were be been being this that it its as at by has have had not no into about after before all can will would should could their they them then than also currently successfully session agent task work working focused completed using updated implementation changes implemented new current which but while other now ready identified verified three two one these those there here more only already still through when where what how our your you we may any each both same').split(' '));

export function renderAtlas(root, panes, navigate, logos, searchTopic = () => {}) {
  const allPanes = panes;
  const scope = root._scope ||= { tool: '', session: '' };
  const tools = [...new Set(['claude', 'codex', 'shell', ...allPanes.map(toolOf), ...history.flatMap(s => s.groups.map(g => g.tool))].filter(Boolean))];
  const sessions = [...new Set([...allPanes.map(p => p.session), ...history.flatMap(s => s.groups.map(g => g.session))].filter(Boolean))].sort();
  panes = allPanes.filter(p => (!scope.tool || toolOf(p) === scope.tool) && (!scope.session || p.session === scope.session));
  const metric = root._metric || 'panes';
  const filtered = scope.tool || scope.session;
  const samples = history.map(s => {
    if (!filtered || s.n === null) return s;
    const groups = (s.groups || []).filter(g => (!scope.tool || g.tool === scope.tool) && (!scope.session || g.session === scope.session));
    // A partial log reconstruction cannot prove a filtered fleet was empty. Keep
    // its timestamp as a gap, including at either end of the selected range.
    if (!s.groups || (s.source === 'logs' && !groups.length)) return { ...s, n: null, groups: [], source: 'gap' };
    return { ...s, groups, n: groups.reduce((counts, g) => counts.map((n, i) => n + (g.n[i] || 0)), STATES.map(() => 0)),
      foreground: s.foreground == null && !groups.length ? null : sum(groups.map(g => g.foreground)),
      background: s.background == null && !groups.length ? null : sum(groups.map(g => g.background)) };
  });
  const chartSamples = samples.map(s => ({ ...s, n: metric === 'panes' ? s.n : metric === 'agents'
    ? sum([s.foreground, s.background]) : s[metric] ?? null }));
  // Preserve focus and pointer targets across unchanged long polls.
  const signature = [metric, scope.tool, scope.session, history, historyWindow, historyError, historyData?.step, ...allPanes.flatMap(p => [p.pane_id, p.session, paneName(p), p.activity,
    p.waiting_on, p.tool, p.session_summary, p.status_line])];
  if (root._signature?.length === signature.length && signature.every((value, i) => value === root._signature[i])) return;
  root._signature = signature;
  const charts = root._charts ||= atlasCharts();
  const focus = root.contains(document.activeElement) ? document.activeElement?.dataset.key : null;
  root._mapResize?.disconnect();
  root.replaceChildren();
  const controls = el('div', 'atlas-controls');
  const redraw = () => { root._signature = null; renderAtlas(root, allPanes, navigate, logos, searchTopic); };
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
  controls.append(sessionPicker); root.append(controls);
  const legend = el('div', 'atlas-legend');
  STATES.forEach((label, i) => legend.append(el('span', `atlas-state s${i}`, `${panes.filter(p => stateOf(p) === i).length} ${label}`)));
  root.append(legend);

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
      dot.setAttribute('aria-label', `${paneName(p)} · ${STATES[stateOf(p)]}`);
      dot.title = `${paneName(p)}\n${STATES[stateOf(p)]}\n${p.session_summary || p.status_line || ''}`;
      const logo = el('img', 'atlas-agent-icon');
      logo.src = Object.prototype.hasOwnProperty.call(logos, p.tool) ? logos[p.tool] : '/tmux-logomark.svg';
      logo.alt = p.tool || 'tmux';
      dot.append(logo, el('span', 'atlas-dot-name', paneName(p)));
      dot.onclick = () => navigate(p.pane_id);
      dots.append(dot);
    });
    island.append(dots);
    islands.push({ node: island, weight: members.length + 2 });
  });
  // Attach tiles before the synchronous focus restoration below; ResizeObserver
  // runs later, after live updates would otherwise drop keyboard focus.
  map.append(...islands.map(island => island.node));
  root.append(map);
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
  if (!panes.length) root.append(el('p', 'muted', 'No current panes match these filters.'));
  const busy = panes.filter(p => stateOf(p) === 1).length, waiting = panes.filter(needsYou).length;
  const insight = el('p', 'atlas-insight muted', `${panes.length} panes across ${groups.size} sessions · ${panes.length ? Math.round(busy / panes.length * 100) : 0}% running · ${waiting} waiting for you`);
  root.append(insight);

  const lower = el('div', 'atlas-lower');
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
    button.onclick = () => searchTopic(word);
    topicLinks.append(button);
  });
  topics.append(charts.cloud, topicLinks);
  if (!words.size) topics.append(el('p', 'muted', 'Topics appear as panes acquire titles and summaries.'));
  lower.append(topics);

  const pulse = el('section', 'atlas-panel');
  const range = el('select', 'atlas-range');
  range.setAttribute('aria-label', 'History time range');
  range.dataset.key = 'history-range';
  [['1h', 'Last hour'], ['24h', 'Last 24 hours'], ['7d', 'Last 7 days'], ['all', 'All history']]
    .forEach(([value, label]) => range.append(new Option(label, value)));
  range.value = historyWindow;
  range.onchange = () => {
    historyWindow = range.value;
    history = []; historyData = null; historyError = '';
    redraw(); reloadHistory?.();
  };
  const hasLogs = samples.some(s => s.source === 'logs');
  const heading = el('div', 'atlas-panel-heading');
  heading.append(el('h3', '', 'Activity over time'));
  const metricControls = el('select', 'atlas-range');
  metricControls.setAttribute('aria-label', 'History population');
  metricControls.dataset.key = 'history-metric';
  [['panes', 'Panes'], ['agents', 'All agents'], ['foreground', 'Main agents'], ['background', 'Background agents']]
    .forEach(([value, label]) => metricControls.append(new Option(label, value)));
  metricControls.value = metric;
  metricControls.onchange = () => { root._metric = metricControls.value; redraw(); };
  const pickers = el('div', 'atlas-history-pickers');
  pickers.append(metricControls, range);
  pulse.append(heading,
    el('p', 'muted', historyError || (historyData
      ? 'Saved by this machine’s daemon. Gaps mean no observation.' : 'Loading saved history…')));
  const zoomControls = el('div', 'atlas-zoom-controls');
  const resetZoom = el('button', 'atlas-reset-zoom', 'Reset zoom');
  resetZoom.dataset.key = 'reset-zoom';
  resetZoom.onclick = () => charts.resetZoom();
  zoomControls.append(el('span', 'muted atlas-zoom-desktop', 'Drag to pan · Ctrl + scroll to zoom'),
    el('span', 'muted atlas-zoom-touch', 'Pinch to zoom · Drag to pan'), resetZoom);
  pulse.append(pickers, charts.stateControls, charts.bars, zoomControls);
  pulse.append(el('p', 'atlas-history-note muted', metric === 'panes'
    ? 'Older history grouped compacting and external waits under Running.'
    : 'Observed coding agents only; shells and log tails are excluded. Main agents and their visible background workers are counted separately. Waiting includes review and CI waits. Hidden workers may be missed; this is not CPU or token utilization. Older history has no agent counts.'));
  if (metric !== 'panes' && !chartSamples.some(s => s.n != null)) pulse.append(el('p', 'muted', 'No agent observations in this range yet.'));
  if (hasLogs) pulse.append(el('p', 'atlas-history-note muted', historyData.backfill_note));
  if (scope.session && history.some(s => s.source === 'logs'))
    pulse.append(el('p', 'atlas-history-note muted', 'Older log records have no tmux session identity; they are excluded from this session filter.'));
  lower.append(pulse); root.append(lower);
  charts.update({ words: topWords.map(([word, members]) => [word, members.length]),
    samples: chartSamples, states: STATES, unit: metric === 'panes' ? 'Panes' : 'Agents', step: historyData?.step || 60000,
    zoomKey: JSON.stringify([historyWindow, scope]), selectWord: searchTopic });
  if (focus) [...root.querySelectorAll('[data-key]')].find(n => n.dataset.key === focus)?.focus({ preventScroll: true });
}
