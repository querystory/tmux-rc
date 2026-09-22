import { atlasCharts } from './atlas-charts.js';
import { needsYou, isRunning, paneName, activityLabel } from './pane-model.js';

const STATES = ['Needs you', 'Running', 'Idle', 'Unknown'];
const stateOf = (p) => needsYou(p) ? 0 : isRunning(p) ? 1 : p.activity === 'idle' ? 2 : 3;
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

export function renderAtlas(root, panes, navigate, logos) {
  const allPanes = panes;
  const scope = root._scope ||= { tool: '', session: '' };
  const tools = [...new Set(['claude', 'codex', 'shell', ...allPanes.map(toolOf), ...history.flatMap(s => s.groups.map(g => g.tool))].filter(Boolean))];
  const sessions = [...new Set([...allPanes.map(p => p.session), ...history.flatMap(s => s.groups.map(g => g.session))].filter(Boolean))].sort();
  panes = allPanes.filter(p => (!scope.tool || toolOf(p) === scope.tool) && (!scope.session || p.session === scope.session));
  const filtered = scope.tool || scope.session;
  const samples = history.map(s => {
    if (!filtered || s.n === null) return s;
    const groups = (s.groups || []).filter(g => (!scope.tool || g.tool === scope.tool) && (!scope.session || g.session === scope.session));
    // A partial log reconstruction cannot prove a filtered fleet was empty. Keep
    // its timestamp as a gap, including at either end of the selected range.
    if (!s.groups || (s.source === 'logs' && !groups.length)) return { ...s, n: null, groups: [], source: 'gap' };
    return { ...s, groups, n: groups.reduce((counts, g) => counts.map((n, i) => n + g.n[i]), [0, 0, 0, 0]) };
  });
  // Preserve focus and pointer targets across unchanged long polls.
  const signature = [scope.tool, scope.session, history, historyWindow, historyError, historyData?.step, ...allPanes.flatMap(p => [p.pane_id, p.session, paneName(p), p.activity,
    p.waiting_on, p.tool, p.session_summary, p.status_line])];
  if (root._signature?.length === signature.length && signature.every((value, i) => value === root._signature[i])) return;
  root._signature = signature;
  const selectedWord = root._selectedWord;
  const charts = root._charts ||= atlasCharts();
  const focus = root.contains(document.activeElement) ? document.activeElement?.dataset.key : null;
  root._mapResize?.disconnect();
  root.replaceChildren();
  const controls = el('div', 'atlas-controls');
  const redraw = () => { root._signature = null; renderAtlas(root, allPanes, navigate, logos); };
  ['', ...tools, ...(scope.tool && !tools.includes(scope.tool) ? [scope.tool] : [])].forEach(tool => {
    const count = allPanes.filter(p => (!tool || toolOf(p) === tool) && (!scope.session || p.session === scope.session)).length;
    const button = el('button', 'atlas-filter', `${tool || 'All tools'} · ${count}`);
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
      dot.setAttribute('aria-label', `${paneName(p)} · ${activityLabel(p)}`);
      dot.title = `${paneName(p)}\n${activityLabel(p)}\n${p.session_summary || p.status_line || ''}`;
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
  const busy = panes.filter(isRunning).length, waiting = panes.filter(needsYou).length;
  const insight = el('p', 'atlas-insight muted', `${panes.length} panes across ${groups.size} sessions · ${panes.length ? Math.round(busy / panes.length * 100) : 0}% running · ${waiting} waiting for you`);
  root.append(insight);

  const lower = el('div', 'atlas-lower');
  const topics = el('section', 'atlas-panel');
  topics.append(el('h3', '', 'What’s on the radar'), el('p', 'muted', 'Words shared by pane titles and summaries. Pick one to explore.'));
  const words = new Map();
  panes.forEach(p => {
    const tokens = new Set(`${paneName(p)} ${p.session_summary || p.status_line || ''}`.toLowerCase().match(/[\p{L}][\p{L}\p{N}-]{2,24}/gu) || []);
    tokens.forEach(w => { if (!STOP.has(w)) { if (!words.has(w)) words.set(w, []); words.get(w).push(p); } });
  });
  const matches = el('div', 'atlas-matches');
  const topWords = [...words].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0])).slice(0, 40);
  const picker = el('select', 'atlas-topic-picker');
  picker.dataset.key = 'topic-picker';
  picker.setAttribute('aria-label', 'Explore a topic');
  picker.append(new Option('Explore a topic…', ''));
  topWords.forEach(([word, members]) => picker.append(new Option(`${word} · ${members.length} panes`, word)));
  const selectWord = word => {
    root._selectedWord = word;
    picker.value = word;
    matches.replaceChildren();
    if (!word || !words.has(word)) return;
    const members = words.get(word);
    matches.append(el('p', 'muted', `${word} · ${members.length} panes`));
    members.forEach(p => { const link = el('button', 'landing-row', paneName(p)); link.onclick = () => navigate(p.pane_id); matches.append(link); });
  };
  picker.onchange = () => selectWord(picker.value);
  topics.append(charts.cloud, picker, matches);
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
  heading.append(el('h3', '', 'Pane states over time'), range);
  pulse.append(heading,
    el('p', 'muted', historyError || (historyData
      ? 'Saved by this machine’s daemon. Blank intervals mean no observation. Idle starts hidden; toggle states in the legend.' : 'Loading saved history…')));
  const zoomControls = el('div', 'atlas-zoom-controls');
  const resetZoom = el('button', 'atlas-reset-zoom', 'Reset zoom');
  resetZoom.dataset.key = 'reset-zoom';
  resetZoom.onclick = () => charts.resetZoom();
  zoomControls.append(el('span', 'muted', 'Drag the handles to zoom · Ctrl + scroll over the chart'), resetZoom);
  pulse.append(charts.bars, zoomControls);
  if (hasLogs) pulse.append(el('p', 'atlas-history-note muted', historyData.backfill_note));
  if (scope.session && scope.session !== '(historical session unknown)' && history.some(s => s.source === 'logs'))
    pulse.append(el('p', 'atlas-history-note muted', 'Older log records have no tmux session identity; they are excluded from this session filter.'));
  lower.append(pulse); root.append(lower);
  charts.update({ words: topWords.map(([word, members]) => [word, members.length]),
    samples, states: STATES, step: historyData?.step || 60000,
    zoomKey: JSON.stringify([historyWindow, scope]), selectWord });
  if (selectedWord) selectWord(selectedWord);
  if (focus) [...root.querySelectorAll('[data-key]')].find(n => n.dataset.key === focus)?.focus({ preventScroll: true });
}
