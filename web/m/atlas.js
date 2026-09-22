import { needsYou, isRunning, paneName, activityLabel } from './pane-model.js';

const STATES = ['Needs you', 'Running', 'Idle', 'Unknown'];
const stateOf = (p) => needsYou(p) ? 0 : isRunning(p) ? 1 : p.activity === 'idle' ? 2 : 3;
const KEY = 'tmuxrc-atlas-history-v1', STEP = 5 * 60 * 1000, LIMIT = 288;
let history = [];
try {
  const saved = JSON.parse(localStorage.getItem(KEY));
  if (Array.isArray(saved)) history = saved.filter(s => Number.isFinite(s.t) && Array.isArray(s.n)
    && s.n.length === 4 && s.n.every(n => Number.isInteger(n) && n >= 0)).slice(-LIMIT);
} catch { /* Storage is optional in private windows. */ }

// Observations, not reconstructed history: missing buckets stay blank.
export function observeAtlas(panes, now = Date.now()) {
  const t = Math.floor(now / STEP) * STEP;
  const n = STATES.map((_, i) => panes.filter(p => stateOf(p) === i).length);
  history = history.filter(s => s.t > t - LIMIT * STEP && s.t < t);
  history.push({ t, n });
  try { localStorage.setItem(KEY, JSON.stringify(history)); } catch { /* Quota/private mode. */ }
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}
const STOP = new Set(('the a an and or to of in on for with from is are was were be been being this that it its as at by has have had not no into about after before all can will would should could their they them then than also currently successfully session agent task work working focused completed using updated implementation changes implemented new current which but while other now ready identified verified three two one these those there here more only already still through when where what how our your you we may any each both same').split(' '));

export function renderAtlas(root, panes, navigate) {
  // Preserve focus and pointer targets across unchanged long polls.
  const signature = JSON.stringify([panes.map(p => [p.pane_id, p.session, paneName(p), p.activity,
    p.waiting_on, p.session_summary, p.status_line]), history]);
  if (root._signature === signature) return;
  root._signature = signature;
  const selectedWord = root.querySelector('.atlas-cloud button[aria-pressed=true]')?.dataset.key;
  const focus = root.contains(document.activeElement) ? document.activeElement?.dataset.key : null;
  root.replaceChildren();
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
      dot.append(el('span', 'atlas-orb'), el('span', 'atlas-dot-name', paneName(p)));
      dot.onclick = () => navigate(p.pane_id);
      dots.append(dot);
    });
    island.append(dots); map.append(island);
  });
  root.append(map);

  const lower = el('div', 'atlas-lower');
  const topics = el('section', 'atlas-panel');
  topics.append(el('h3', '', 'What’s on the radar'), el('p', 'muted', 'Words shared by pane titles and summaries. Pick one to explore.'));
  const words = new Map();
  panes.forEach(p => {
    const tokens = new Set(`${paneName(p)} ${p.session_summary || p.status_line || ''}`.toLowerCase().match(/[\p{L}][\p{L}\p{N}-]{2,24}/gu) || []);
    tokens.forEach(w => { if (!STOP.has(w)) { if (!words.has(w)) words.set(w, []); words.get(w).push(p); } });
  });
  const cloud = el('div', 'atlas-cloud');
  const matches = el('div', 'atlas-matches');
  [...words].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0])).slice(0, 24).forEach(([word, members]) => {
    const b = el('button', '', word);
    b.dataset.key = `word:${word}`;
    b.style.fontSize = `${14 + Math.min(24, members.length * 4)}px`;
    b.title = `${members.length} panes`;
    b.setAttribute('aria-pressed', 'false');
    b.onclick = () => {
      cloud.querySelectorAll('button').forEach(n => n.setAttribute('aria-pressed', String(n === b)));
      matches.replaceChildren(el('p', 'muted', `${word} · ${members.length} panes`));
      members.forEach(p => { const link = el('button', 'landing-row', paneName(p)); link.onclick = () => navigate(p.pane_id); matches.append(link); });
    };
    cloud.append(b);
  });
  topics.append(cloud, matches);
  if (!words.size) topics.append(el('p', 'muted', 'Topics appear as panes acquire titles and summaries.'));
  lower.append(topics);

  const pulse = el('section', 'atlas-panel');
  pulse.append(el('h3', '', 'The last 24 hours'), el('p', 'muted', 'Pane counts · sampled every 5 minutes in this browser. Blank space means no observation.'));
  const chart = el('div', 'atlas-chart');
  chart.setAttribute('role', 'img');
  const max = Math.max(1, ...history.map(s => s.n.reduce((a, b) => a + b, 0)));
  chart.setAttribute('aria-label', `Observed pane counts over the last 24 hours. Scale zero to ${max} panes. ${history.length} observed five-minute intervals. ${STATES.map((s, i) => `${s}: ${history.at(-1)?.n[i] || 0}`).join(', ')} in the latest observation.`);
  const end = Math.floor(Date.now() / STEP) * STEP;
  const samples = new Map(history.map(s => [s.t, s]));
  for (let i = 0; i < LIMIT; i++) {
    const t = end - (LIMIT - 1 - i) * STEP, sample = samples.get(t);
    const bar = el('div', 'atlas-bar');
    if (sample) {
      bar.classList.add('observed');
      bar.title = `${new Date(t).toLocaleString()}\n${STATES.map((s, j) => `${s}: ${sample.n[j]}`).join('\n')}`;
      sample.n.forEach((n, j) => { const part = el('span', `s${j}`); part.style.height = `${n / max * 100}%`; bar.append(part); });
    }
    chart.append(bar);
  }
  pulse.append(el('p', 'atlas-scale muted', `${max} panes`), chart, el('div', 'atlas-axis muted', '24 hours ago → now'));
  lower.append(pulse); root.append(lower);
  if (selectedWord) [...cloud.children].find(n => n.dataset.key === selectedWord)?.click();
  if (focus) [...root.querySelectorAll('[data-key]')].find(n => n.dataset.key === focus)?.focus({ preventScroll: true });
}
