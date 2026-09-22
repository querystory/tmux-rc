import { atlasCharts } from './atlas-charts.js';
import { needsYou, isRunning, paneName, activityLabel } from './pane-model.js';

const STATES = ['Needs you', 'Running', 'Idle', 'Unknown'];
const stateOf = (p) => needsYou(p) ? 0 : isRunning(p) ? 1 : p.activity === 'idle' ? 2 : 3;
const toolOf = p => p.tool || 'other';
const validCounts = n => Array.isArray(n) && n.length === 4 && n.every(v => Number.isInteger(v) && v >= 0);
const KEY = 'tmuxrc-atlas-history-v1', STEP = 5 * 60 * 1000, LIMIT = 288;
let history = [];
try {
  const saved = JSON.parse(localStorage.getItem(KEY));
  if (Array.isArray(saved)) history = saved.filter(s => s && Number.isFinite(s.t) && validCounts(s.n))
    .map(s => ({ ...s, groups: Array.isArray(s.groups) && s.groups.every(g => g && typeof g.session === 'string'
      && typeof g.tool === 'string' && validCounts(g.n)) ? s.groups : undefined })).slice(-LIMIT);
} catch { /* Storage is optional in private windows. */ }

// Coalesce frequent state polls; flush the latest in-memory bucket when leaving.
let persistTimer = null;
function persistHistory() {
  clearTimeout(persistTimer);
  persistTimer = null;
  try { localStorage.setItem(KEY, JSON.stringify(history)); } catch { /* Quota/private mode. */ }
}
window.addEventListener('pagehide', () => { if (persistTimer !== null) persistHistory(); });

// Observations, not reconstructed history: missing buckets stay blank.
export function observeAtlas(panes, now = Date.now()) {
  const t = Math.floor(now / STEP) * STEP;
  const n = STATES.map((_, i) => panes.filter(p => stateOf(p) === i).length);
  const groups = new Map();
  panes.forEach(p => {
    const session = p.session || '', tool = toolOf(p), key = JSON.stringify([session, tool]);
    if (!groups.has(key)) groups.set(key, { session, tool, n: [0, 0, 0, 0] });
    groups.get(key).n[stateOf(p)]++;
  });
  const sample = { t, n, groups: [...groups.values()] };
  if (JSON.stringify(history.at(-1)) === JSON.stringify(sample)) return;
  history = history.filter(s => s.t > t - LIMIT * STEP && s.t < t);
  history.push(sample);
  if (persistTimer === null) persistTimer = setTimeout(persistHistory, 30_000);
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}
const STOP = new Set(('the a an and or to of in on for with from is are was were be been being this that it its as at by has have had not no into about after before all can will would should could their they them then than also currently successfully session agent task work working focused completed using updated implementation changes implemented new current which but while other now ready identified verified three two one these those there here more only already still through when where what how our your you we may any each both same').split(' '));

export function renderAtlas(root, panes, navigate, logos) {
  const allPanes = panes;
  const scope = root._scope ||= { tool: '', session: '' };
  const tools = [...new Set(['claude', 'codex', 'shell', ...allPanes.map(toolOf)])];
  const sessions = [...new Set(allPanes.map(p => p.session))].sort();
  panes = allPanes.filter(p => (!scope.tool || toolOf(p) === scope.tool) && (!scope.session || p.session === scope.session));
  const filtered = scope.tool || scope.session;
  const samples = history.filter(s => !filtered || s.groups).map(s => !filtered ? s : ({ t: s.t,
    n: s.groups.filter(g => (!scope.tool || g.tool === scope.tool) && (!scope.session || g.session === scope.session))
      .reduce((counts, g) => counts.map((n, i) => n + g.n[i]), [0, 0, 0, 0]),
  }));
  // Preserve focus and pointer targets across unchanged long polls.
  const signature = JSON.stringify([scope, allPanes.map(p => [p.pane_id, p.session, paneName(p), p.activity,
    p.waiting_on, p.tool, p.session_summary, p.status_line]), history]);
  if (root._signature === signature) return;
  root._signature = signature;
  const selectedWord = root._selectedWord;
  const charts = root._charts ||= atlasCharts();
  const focus = root.contains(document.activeElement) ? document.activeElement?.dataset.key : null;
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
      logo.src = Object.hasOwn(logos, p.tool) ? logos[p.tool] : '/tmux-logomark.svg';
      logo.alt = p.tool || 'tmux';
      dot.append(logo, el('span', 'atlas-dot-name', paneName(p)));
      dot.onclick = () => navigate(p.pane_id);
      dots.append(dot);
    });
    island.append(dots); map.append(island);
  });
  root.append(map);
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
  const duration = samples.length ? samples.at(-1).t - samples[0].t : 0;
  const span = duration < 3600000 ? `${Math.round(duration / 60000)} minutes` : `${(duration / 3600000).toFixed(1)} hours`;
  pulse.append(el('h3', '', duration ? `State counts · ${span}` : 'State counts · current snapshot'),
    el('p', 'muted', 'Observed in this browser · 5-minute buckets. Gaps are unobserved.'));
  pulse.append(charts.bars);
  if (samples.length < 2) pulse.append(el('p', 'atlas-history-note muted', samples.length ? 'One snapshot so far. The timeline grows as observations arrive.' : 'No observations for this selection yet.'));
  lower.append(pulse); root.append(lower);
  charts.update({ words: topWords.map(([word, members]) => [word, members.length]),
    samples, states: STATES, step: STEP, selectWord });
  if (selectedWord) selectWord(selectedWord);
  if (focus) [...root.querySelectorAll('[data-key]')].find(n => n.dataset.key === focus)?.focus({ preventScroll: true });
}
