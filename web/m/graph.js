// The Fleet graph (#view=graph, docs/design/fleet-graph.md): every pane a node on a canvas
// you pan, pinch and zoom, its running sub-agents as satellites, grouped by a lens (Session,
// Account, Repo / dir, Worktree, Tool / model) whose hubs fold. Switching lens glides each
// node to its new place; a poll that changes nothing structural only recolours, and one that
// adds a pane eases it in from its hub, so live updates never jump the layout.
import { STATES, stateOf } from './atlas.js';
import { esc, paneName, paneHeadline, records, since, home } from './pane-model.js';

const LENSES = [['session', 'Session'], ['account', 'Account'], ['repo', 'Repo / dir'], ['worktree', 'Worktree'], ['tool', 'Tool / model']];
const LEGEND = {
  session: 'A ring is a tmux session, coloured by its panes’ states. Tap a ring to fold it.',
  account: 'The account each agent runs under. Shells, and agents with their own login, apart.',
  repo: 'Repository, then the directory the pane works in.',
  worktree: 'Panes in one git worktree share a ring. Amber: two agents busy in one checkout.',
  tool: 'Tool, then model.',
};
const TOOLS = { claude: 'Claude', codex: 'Codex', opencode: 'OpenCode', omp: 'omp', gemini: 'Gemini', antigravity: 'Antigravity', shell: 'Shell' };
const K_MIN = 0.05; // the farthest zoom out, Fit's and the user's alike
const MAX_SUBS = 8; // satellites drawn per pane; the popover still counts them all
const motion = () => !matchMedia('(prefers-reduced-motion: reduce)').matches;
const phone = () => matchMedia('(max-width: 699px), (max-height: 500px)').matches; // a phone in landscape too, as style.css
const busy = s => s === 1 || s === 4 || s === 5;
const frac = x => x - Math.floor(x), hash = i => frac(Math.sin(i * 127.1 + 311.7) * 43758.5453);
const costOf = p => (String(p.cost || '').match(/\d[\d,]*(\.\d+)?/g) || []).reduce((t, x) => t + parseFloat(x.replace(/,/g, '')), 0); // "$0.09 (+$0.18) (sub)" is $0.27
const ageOf = p => Math.max(0, Date.now() / 1000 - (p.state_since || Date.now() / 1000));
const fade = p => { const a = ageOf(p); return a < 600 ? 1 : a < 3600 ? 0.8 : a < 86400 ? 0.6 : 0.42; }; // idle recedes with age: fresh, an hour, a day, older
const cut = (t, n) => { t = String(t || '').replace(/\s+/g, ' ').trim(); return t.length > n ? `${t.slice(0, n - 1)}…` : t; };
const tokens = t => { const m = /([\d.]+)\s*([km]?)/i.exec(String(t || '')); return m ? m[1] * ({ k: 1e3, m: 1e6 }[m[2].toLowerCase()] || 1) : 0; };
// Flows: every moving mark is something that happened. A spark is tokens being written (one
// for each SPARK a second, read over the last WINDOW ms, so a pane that stops writing goes
// still); a pulse is one event, travelling once from where it came from to where it went.
const SPARK = 20, WINDOW = 20000, PULSE = 1300;
const RECENT = 600; // s: a pane you typed into this recently keeps its line to you
const MAX_DOCS = 3; // scratch pages drawn per pane, newest first
const HUB = 'tmuxrc-graph-hub';
// The scratch pages each pane made (GET /api/scratch/panes), asked at most every 15 s while a graph draws.
let shelf = {}, shelfAt = 0;
const scratchPages = () => { if (Date.now() - shelfAt > 15000) { shelfAt = Date.now(); fetch('/api/scratch/panes').then(r => r.ok ? r.json() : {}).then(x => { shelf = x; }, () => {}); } return shelf; };
// The dark canvas: the graph on a navy ground whatever the app's theme (style.css), per device.
const DARK = 'tmuxrc-graph-dark';
try { document.documentElement.classList.toggle('fg-dark', localStorage.getItem(DARK) === '1'); } catch {}
const el = (tag, cls = '', html = '') => Object.assign(document.createElement(tag), { className: cls, innerHTML: html });
const agent = p => p.tool && p.tool !== 'shell';
// Every sub-agent not finished or idle, waiting ones too (the sidebar lists only the busy):
// a parent waiting on six background agents should show six. `agents` is the backend's busy
// count, which can exceed the busy rows listed: the shortfall is busy workers off the roster. A satellite takes its state's colour.
const subsList = p => agent(p) ? records(p.subagents).filter(a => !['done', 'idle'].includes(a.state)) : [];
const subCount = p => { const l = subsList(p), busy = l.filter(a => a.state === 'running' || a.state === 'compacting').length; return agent(p) ? l.length + Math.max(0, (Number(p.agents) || 0) - busy) : 0; };
const subsOf = p => Math.min(MAX_SUBS, subCount(p));
const ACTIVE = { running: 1, compacting: 1, waiting: 1 };
// A pane whose sub-agents are at work is not idle, whatever its own prompt shows: it is waiting on them.
const paneState = p => { const s = stateOf(p); return (s === 2 || s === 3) && agent(p) && (subsList(p).some(a => ACTIVE[a.state]) || Number(p.agents) > 0) ? 5 : s; };
const subState = a => a ? { running: 1, compacting: 4, waiting: 5 }[a.state] ?? 3 : 1; // one counted, not listed, is a busy one
// A path segment is its label, or [identity, label] where two hubs could share a label: two
// clones on the same branch, two accounts given one name.
// Where a pane sits: its repository (the daemon's worktree scan, /api/state's `worktree`) and
// the directories below the worktree's root, else the folder it is in.
const below = ({ cwd = '', worktree: w }) => cwd.startsWith(`${w.path}/`) ? cwd.slice(w.path.length + 1).split('/') : [];
const outside = p => ['no repository', home(p.cwd || '') || 'unknown'];
const PATHS = {
  session: p => [p.session || 'no session'],
  account: (p, owner) => { const a = owner(p.pane_id); return [a ? [a.dir, a.name] : agent(p) ? 'Own login' : 'No agent']; },
  repo: p => p.worktree ? [[p.worktree.key, p.worktree.repo], ...below(p)] : outside(p),
  worktree: ({ worktree: w, ...p }) => w ? [[w.key, w.repo], [w.path, w.branch || w.path.split('/').pop()]] : outside(p),
  tool: p => [TOOLS[p.tool] || p.tool || 'Other', p.model],
};
// The app's tokens as the graph sees them: the dark canvas redefines them under it.
function colors(box) {
  const cs = getComputedStyle(box), v = n => cs.getPropertyValue(n).trim();
  return { s: [0, 1, 2, 3, 4, 5].map(i => v(`--chart-s${i}`)), acct: [0, 1, 2, 3, 4, 5].map(i => v(`--acct-${i}`)), fg: v('--fg'), muted: v('--muted'), line: v('--line'),
    surface: v('--surface'), amber: v('--amber'), accent: v('--accent'), font: v('--mono'), dark: v('color-scheme') !== 'light' }; // amber, accent: state colours safe for text
}
// Idle reads as a quiet grey ring, not the chart's near-invisible idle fill.
const ring = (C, s) => s === 2 || s === 3 ? C.muted : C.s[s];

// The graph on `box`. `mini` is the Dashboard card's: no labels but the hubs', no input.
// ctx: { icon, logos, open(paneId), owner(paneId) -> account profile, onChange() }.
export function fleetGraph(box, mini, ctx) {
  const cv = document.createElement('canvas'), dc = cv.getContext('2d');
  box.append(cv);
  let W = 0, H = 0, C = null, theme = null, hover = null, sel = null, raf = 0, lens = 'session', model = null, anim = null, panes = [], pop = null, flowed = false, hub = mini || ctx.hub !== false, pages = {}; // hub: the centre on, else groups as discs
  let listNodes = () => {}, touched = false, roomAt = null; // touched: panned or zoomed since the last fit // the full view's node buttons (below)
  const PAD = mini ? 52 : 60, view = { k: 1, x: 0, y: 0 }, collapsed = new Set(), imgs = {}, cache = new Map();
  // The renderer's: glow sprites, the ground, and what a draw costs (ms).
  const sprites = new Map(), spacing = 'letterSpacing' in dc;
  let bg = null, cost = 3, particles = 0;
  // Flows (track): each pane's and sub-agent's token gains as [time, tokens], what each pane
  // looked like at the last poll, and the pulses on their way.
  const gains = new Map(), last = new Map(), pulses = [];
  const subKey = x => `${x.parent.id}/${x.a?.label}`;
  const rate = key => { // tokens a second, over the window, or since the reading it started from if that is sooner
    const g = gains.get(key), t = performance.now();
    while (g?.length && g[0][0] < t - WINDOW) g.shift();
    return g?.length ? g.reduce((s, x) => s + x[1], 0) / Math.max(1000, Math.min(WINDOW, t - g.from)) * 1000 : 0;
  };
  const sparks = r => Math.min(30, Math.ceil(r / SPARK));
  const gain = (key, now, was, from) => { const d = now < was ? now : now - was; if (d > 0) (gains.get(key) || gains.set(key, Object.assign([], { from })).get(key)).push([performance.now(), d]); }; // a smaller count is a new turn's
  // What changed since the last poll, as flows: the tokens each pane and sub-agent wrote (the
  // sparks), and a pulse for each event: you typed into a pane (from tmux-rc: a message, an
  // answer, keys), it came to need you, it started or got back a sub-agent, it published a
  // scratch page, and a page it had promised landed (the Ready push). A pane's first poll is
  // only its baseline, so opening the graph replays nothing.
  function track(list) {
    const t = performance.now(), here = new Set(), pulse = (from, to, color, delay = 0) => pulses.push({ from, to, color, t0: t + delay + 150 * (pulses.length % 6) });
    const subAt = (id, l, all) => (all ? [...cache.values()] : model?.nodes || []).find(x => x.kind === 'sub' && x.parent.id === id && x.a?.label === l);
    for (const p of list) {
      const id = p.pane_id, was = last.get(id), subs = new Map(subsList(p).map(a => [a.label, tokens(a.tokens)])), docs = new Map((pages[id] || []).map(d => [d.href, d.pending]));
      const now = { at: t, tok: tokens(p.working?.tokens) || was?.tok || 0, sent: p.input_at || 0, s: paneState(p), subs, docs };
      last.set(id, now); here.add(id);
      if (!was) continue;
      gain(id, now.tok, was.tok, was.at); // a parse that showed no counter kept the last one: no reading is not a new turn
      if (now.sent > was.sent) pulse('you', id, 'fg');
      if (now.s === 0 && was.s !== 0) pulse(id, 'you', 0);
      for (const [l, n] of subs) { gain(`${id}/${l}`, n, was.subs.get(l) ?? n, was.at); if (!was.subs.has(l)) pulse(id, () => subAt(id, l), 1); }
      for (const l of was.subs.keys()) { const x = !subs.has(l) && subAt(id, l, true); if (x) pulse({ x: x.x, y: x.y }, id, 1); } // handed back: from where it was
      for (const [h, pending] of docs) {
        const landed = was.docs.get(h) && !pending;
        if (!was.docs.has(h) || landed) pulse(id, `${id}#${h}`, 'accent');
        if (landed) pulse(`${id}#${h}`, 'you', 1, PULSE);
      }
    }
    for (const id of last.keys()) if (!here.has(id)) last.delete(id);
    for (const [key, g] of gains) if (!g.length || g.at(-1)[0] < t - WINDOW) gains.delete(key);
  }
  // Nodes keep their identity across lenses and polls, so a pane glides to its new place.
  const node = (id, init) => { let n = cache.get(id); if (!n) cache.set(id, n = { id, x: NaN, y: NaN, ...init() }); return n; };
  const paneNode = p => Object.assign(node(p.pane_id, () => ({ kind: 'pane' })), { p, r: mini ? 11 : p.context_pct == null ? 8 : 7 + 9 * p.context_pct / 100 }); // its size: the context it has used
  const subNodes = pn => Array.from({ length: subsOf(pn.p) }, (_, j) => Object.assign(node(`${pn.id}/${j}`, () => ({ kind: 'sub' })), { parent: pn, a: subsList(pn.p)[j] })).map(x => Object.assign(x, { r: !x.a || ACTIVE[x.a.state] ? Math.max(5, x.parent.r * 0.6) : 3.5 }));
  const docNodes = pn => mini ? [] : (pages[pn.id] || []).slice(0, MAX_DOCS).map(d => Object.assign(node(`${pn.id}#${d.href}`, () => ({ kind: 'doc' })), { parent: pn, d, r: 5 }));
  const stateOf_ = n => n.kind === 'pane' ? paneState(n.p) : n.kind === 'sub' ? subState(n.a) : null;
  const logo = tool => { const src = ctx.logos[tool]; if (src && !imgs[tool]) { const i = imgs[tool] = new Image(); i.onload = () => draw(); i.src = src; } return imgs[tool]; };

  // A lens is a tree: the root, a hub per path segment, then the panes and their satellites.
  function build() {
    const root = node('root', () => ({ kind: 'root', r: mini ? 22 : 30 })), nodes = hub ? [root] : [], edges = [], groups = new Map();
    root.children = [];
    for (const p of panes) {
      let up = root, key = `g:${lens}`;
      PATHS[lens](p, ctx.owner).filter(Boolean).slice(0, hub ? 9 : 1).forEach((part, d) => { // without the hub, a lens's first level is its clusters
        const [id, seg] = Array.isArray(part) ? part : [part, part];
        key += `/${encodeURIComponent(id)}`; // a key can hold slashes; segments must not merge
        let g = groups.get(key);
        if (!g) { g = Object.assign(node(key, () => ({ kind: 'group', depth: d + 1, r: d ? 11 : 16 })), { label: seg, children: [], panes: [], up }); groups.set(key, g); up.children.push(g); nodes.push(g); }
        g.panes.push(p); up = g;
      });
      const pn = paneNode(p);
      pn.up = up; pn.children = []; up.children.push(pn); nodes.push(pn);
      pn.subs = subNodes(pn); pn.docs = docNodes(pn);
      [...pn.subs, ...pn.docs].forEach(s => { s.up = pn; nodes.push(s); }); // kept: layout and draw read them, never rebuild them
    }
    // Worktree lens: a checkout with two agents busy in it is where edits collide.
    for (const g of groups.values()) {
      g.busy = g.panes.filter(p => busy(paneState(p))).length; g.subs = g.panes.reduce((t, p) => t + subCount(p), 0); g.need = g.panes.filter(p => paneState(p) === 0).length;
      g.warn = lens === 'worktree' && g.depth === 2 && g.panes.filter(p => p.worktree && agent(p) && busy(paneState(p))).length > 1; // a real checkout, not a folder
    }
    const hidden = n => { for (let u = n.up; u; u = u.up) if (collapsed.has(u.id)) return true; return false; };
    const vis = nodes.filter(n => !hidden(n));
    vis.forEach(n => { if (n.up && (hub || n.kind === 'sub' || n.kind === 'doc')) edges.push({ a: n.up, b: n }); }); // without the hub, only lines that carry something
    const live = new Set(nodes.map(n => n.id));
    for (const id of cache.keys()) if (!live.has(id)) cache.delete(id); // gone panes, other lenses' hubs
    return { root, nodes: vis, edges, sig: `${lens}|${hub}|${vis.map(n => `${n.id}<${n.up?.id}`).join()}` }; // a move between hubs is a change
  }
  // A pane's satellites, its sub-agents then its pages, fan out away from the middle.
  const orbit = n => [...n.subs, ...n.docs].forEach((x, m, all) => { const c = n.out + (m - (all.length - 1) / 2) * 0.55, d = n.r + x.r + 26; Object.assign(x, { tx: n.tx + d * Math.cos(c), ty: n.ty + d * Math.sin(c), out: c }); });
  const layout = m => { (hub ? radial : clusters)(m); listNodes(); };
  // Leaves (panes, folded hubs) share the circle by slot, with a gap between groups.
  function radial({ root }) {
    let s = 0, D = 0;
    const walk = n => {
      if (n.kind === 'group') D = Math.max(D, n.depth);
      if (n.kind === 'pane' || collapsed.has(n.id) || !n.children.length) { n.slot = s++; return [n.slot, n.slot]; } // a leaf, an empty fleet's root too
      let lo = Infinity, hi = -Infinity;
      n.children.forEach((c, i) => { if (i) s += n.kind === 'root' ? 1.4 : 0.6; const [a, b] = walk(c); lo = Math.min(lo, a); hi = Math.max(hi, b); });
      n.slot = (lo + hi) / 2;
      return [lo, hi];
    };
    walk(root); s += 1.4;
    const RL = Math.max(120 * (D + 1), s * 30 / (2 * Math.PI)), ang = slot => -Math.PI / 2 + (slot + 0.5) / s * 2 * Math.PI;
    // An ellipse shaped like the room Fit frames it in (padding included), so a tall phone fills.
    const { w, h } = room(), ax = Math.max(1, Math.min(mini ? 2.6 : 1.35, w / h || 1)), ay = Math.max(1, Math.min(2, ((h / w || 1) * (2 * RL * ax + 2 * PAD) - 2 * PAD) / (2 * RL)));
    const place = (n, d) => {
      const a = ang(n.slot), r = n.kind === 'root' ? 0 : n.kind === 'pane' ? RL : RL * d / (D + 0.6);
      Object.assign(n, { tx: r * Math.cos(a) * ax, ty: r * Math.sin(a) * ay, out: a, R: 0 });
      if (n.kind === 'pane') orbit(n);
      if (!collapsed.has(n.id)) (n.children || []).forEach(c => place(c, d + 1));
    };
    place(root, 0);
  }
  // No hub: each group a disc of its panes, sunflower-packed with the ones that have satellites
  // on the rim, where those point out; the discs in rows shaped like the room. A group's node,
  // the place to tap to fold it, sits on the top of its disc, its words just inside; folded, it is the disc.
  function clusters({ root }) {
    const S = 34, G = 44, { w, h } = room(), sats = p => p.subs.length + p.docs.length;
    const discs = root.children.map(g => {
      const ps = collapsed.has(g.id) ? [] : g.children.slice().sort((a, b) => sats(a) - sats(b));
      return { g, ps, R: ps.length ? S * Math.sqrt(ps.length) + (ps.some(sats) ? 58 : 22) + 20 : 24 }; // 20: room for its words at the top
    }).sort((a, b) => b.R - a.R);
    const rowW = Math.max(...discs.map(d => 2 * d.R), Math.sqrt(discs.reduce((t, d) => t + (2 * d.R + G) ** 2, 0) * w / (h || 1)));
    const rows = [[]];
    let x = 0;
    for (const d of discs) { if (x && x + 2 * d.R > rowW) { rows.push([]); x = 0; } rows.at(-1).push(d); x += 2 * d.R + G; }
    let y = 0;
    for (const row of rows) {
      const rh = Math.max(...row.map(d => 2 * d.R)), rw = row.reduce((t, d) => t + 2 * d.R + G, -G);
      let cx = -rw / 2;
      for (const { g, ps, R } of row) {
        const ox = cx + R, oy = y + rh / 2, spin = hash(g.id.length) * 6.3;
        Object.assign(g, { R: ps.length ? R : 0, tx: ox, ty: ps.length ? oy - R : oy, out: -Math.PI / 2 });
        ps.forEach((p, i) => { const r = S * Math.sqrt(i + 0.5), a = i * 2.39996 + spin; Object.assign(p, { tx: ox + r * Math.cos(a), ty: oy + 20 + r * Math.sin(a), out: a }); orbit(p); });
        cx += 2 * R + G;
      }
      y += rh + G;
    }
  }
  const rad = n => n.kind === 'doc' ? Math.max(n.r, 4 / view.k) : n.kind === 'sub' ? (n.r < 5 ? Math.max(n.r, 2.5 / view.k) : Math.max(n.r, 0.6 * rad(n.parent), 3.5 / view.k)) // a working one stays six tenths of its pane
    : Math.max(n.r, (n.kind === 'group' ? 7 : n.kind === 'root' ? (mini ? 14 : 20) * 1.04 : 4.5) / view.k); // the hub's as drawn, breathing included
  function near(n) { // what a hovered or selected node lights: itself, its edges, a hub's subtree
    if (!n || n.kind === 'root') return null;
    const s = new Set([n.id]), down = x => (x.children || []).forEach(c => { s.add(c.id); down(c); });
    model.edges.forEach(e => { if (e.a === n) s.add(e.b.id); if (e.b === n) s.add(e.a.id); });
    if (n.kind === 'group') down(n);
    if (n.kind === 'pane') [...n.subs, ...n.docs].forEach(x => s.add(x.id));
    return s;
  }

  // A soft round glow, drawn once per colour and stamped with drawImage: far cheaper than
  // shadowBlur or a gradient per particle. `hot` gives it a white-hot centre.
  function sprite(color, hot) {
    let s = sprites.get(color + hot);
    if (!s) {
      s = Object.assign(document.createElement('canvas'), { width: 64, height: 64 });
      const g = s.getContext('2d'), r = g.createRadialGradient(32, 32, 0, 32, 32, 32);
      if (hot) r.addColorStop(0, C.dark ? '#fffaf0' : color);
      r.addColorStop(hot ? 0.18 : 0, color); r.addColorStop(hot ? 0.45 : 0.35, color + (C.dark ? '55' : '33')); r.addColorStop(1, `${color}00`);
      g.fillStyle = r; g.fillRect(0, 0, 64, 64);
      sprites.set(color + hot, s);
    }
    return s;
  }
  const stamp = (s, x, y, size, a) => { dc.globalAlpha = a; dc.drawImage(s, x - size / 2, y - size / 2, size, size); };
  // The ground, drawn once per size and theme: the surface, a glow around the hub, a faint dot grid.
  function ground(dpr) {
    const key = `${W}|${H}|${dpr}|${theme}`;
    if (bg?._key === key) return bg;
    bg = Object.assign(document.createElement('canvas'), { width: Math.round(W * dpr), height: Math.round(H * dpr), _key: key });
    const g = bg.getContext('2d'), r = g.createRadialGradient(W / 2, H * 0.48, 0, W / 2, H * 0.48, Math.hypot(W, H) * 0.62);
    g.scale(dpr, dpr); r.addColorStop(0, `${C.s[1]}${C.dark ? '14' : '0d'}`); r.addColorStop(0.6, `${C.s[1]}00`);
    g.fillStyle = C.surface; g.fillRect(0, 0, W, H); g.fillStyle = r; g.fillRect(0, 0, W, H);
    g.fillStyle = C.dark ? 'rgba(140,165,220,0.07)' : 'rgba(40,60,50,0.07)';
    for (let x = 16; x < W; x += 28) for (let y = 16; y < H; y += 28) g.fillRect(x, y, 1, 1);
    return bg;
  }
  // An edge is a cubic curve: it leaves the parent turned a little (one consistent swirl, so the
  // fleet reads as a galaxy, not a sea urchin) and arrives along the child's outward direction.
  function curve(e) {
    const { a, b } = e, dx = b.x - a.x, dy = b.y - a.y, L = Math.hypot(dx, dy) || 1;
    const ox = Math.cos(b.out || 0), oy = Math.sin(b.out || 0), sw = b.kind === 'sub' ? 0 : 0.55, cs = Math.cos(sw), sn = Math.sin(sw), c = e.c ||= new Float32Array(8);
    c[0] = a.x; c[1] = a.y; c[2] = a.x + (dx * cs - dy * sn) * 0.36; c[3] = a.y + (dx * sn + dy * cs) * 0.36;
    c[4] = b.x - ox * L * 0.36; c[5] = b.y - oy * L * 0.36; c[6] = b.x; c[7] = b.y;
    return c;
  }
  let PX = 0, PY = 0, NX = 0, NY = 0; // the last bz() point and its unit normal
  function bz(c, t) {
    const u = 1 - t, w0 = u * u * u, w1 = 3 * u * u * t, w2 = 3 * u * t * t, w3 = t * t * t;
    PX = w0 * c[0] + w1 * c[2] + w2 * c[4] + w3 * c[6]; PY = w0 * c[1] + w1 * c[3] + w2 * c[5] + w3 * c[7];
    const dx = u * u * (c[2] - c[0]) + 2 * u * t * (c[4] - c[2]) + t * t * (c[6] - c[4]), dy = u * u * (c[3] - c[1]) + 2 * u * t * (c[5] - c[3]) + t * t * (c[7] - c[5]);
    const l = Math.hypot(dx, dy) || 1; NX = -dy / l; NY = dx / l;
  }
  const path = c => { dc.beginPath(); dc.moveTo(c[0], c[1]); dc.bezierCurveTo(c[2], c[3], c[4], c[5], c[6], c[7]); dc.stroke(); };
  // You and a pane: a gentle bow from one to the other, which a pulse rides.
  const bow = (a, b) => ({ x: (a.x + b.x) / 2 - (b.y - a.y) * 0.18, y: (a.y + b.y) / 2 + (b.x - a.x) * 0.18 });
  const arc = (a, b) => { const q = bow(a, b); dc.beginPath(); dc.moveTo(a.x, a.y); dc.quadraticCurveTo(q.x, q.y, b.x, b.y); };
  const along = (a, b, t) => { const q = bow(a, b), u = 1 - t; return { x: u * u * a.x + 2 * u * t * q.x + t * t * b.x, y: u * u * a.y + 2 * u * t * q.y + t * t * b.y }; };
  // A pulse's end: you, a node by id while it is drawn, a node found later (a sub-agent just spawned), or a place one left.
  const spot = (ref, you) => { if (ref === 'you') return you; if (typeof ref === 'function') return ref(); if (typeof ref !== 'string') return ref; const n = cache.get(ref); return n && model.nodes.includes(n) && !Number.isNaN(n.x) ? n : null; };
  const disc = (x, y, r) => { dc.beginPath(); dc.arc(x, y, r, 0, 7); };

  function draw(now = performance.now()) {
    if (!model || !W || !box.isConnected) return; // a view off the page has no palette to read
    const t0 = performance.now();
    if (theme !== document.documentElement.className || !C?.fg) { theme = document.documentElement.className; C = colors(box); sprites.clear(); } // an empty palette is read again
    if (anim) { // a lens change, a fold, a new pane or a jump: everything glides from where it was
      const t = Math.max(0, Math.min(1, (now - anim.t0) / anim.dur)), e = t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2;
      model.nodes.forEach(n => { n.x = n.fx + (n.tx - n.fx) * e; n.y = n.fy + (n.ty - n.fy) * e; n.alpha = n.fresh ? e : 1; });
      Object.assign(view, { k: anim.v0.k + (anim.v1.k - anim.v0.k) * e, x: anim.v0.x + (anim.v1.x - anim.v0.x) * e, y: anim.v0.y + (anim.v1.y - anim.v0.y) * e });
      if (t >= 1) { const then = anim.then; anim = null; model.nodes.forEach(n => { n.fresh = false; n.alpha = 1; }); then?.(); }
    }
    const dpr = devicePixelRatio || 1, k = view.k, moving = motion(), add = C.dark ? 'lighter' : 'source-over'; // additive light only reads on a dark ground
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) Object.assign(cv, { width: Math.round(W * dpr), height: Math.round(H * dpr) }); // a resize, or another display's ratio
    dc.setTransform(1, 0, 0, 1, 0, 0); dc.globalAlpha = 1; dc.globalCompositeOperation = 'source-over';
    dc.drawImage(ground(dpr), 0, 0);
    dc.setTransform(dpr * k, 0, 0, dpr * k, dpr * view.x, dpr * view.y);
    const lit = near(hover || sel), dim = (...ids) => lit && !ids.every(i => lit.has(i)) ? 0.12 : 1;
    const font = (size, weight, track) => { dc.font = `${weight} ${size / k}px ${C.font}`; if (spacing) dc.letterSpacing = `${track / k}px`; };
    const label = (text, x, y, align, color, size, weight = 400, base = 'middle', track = 0) => {
      font(size, weight, track); dc.textAlign = align; dc.textBaseline = base;
      dc.lineJoin = 'round'; dc.lineWidth = 4 / k; dc.strokeStyle = C.surface; dc.strokeText(text, x, y); dc.fillStyle = color; dc.fillText(text, x, y);
    };
    // Words are blocks of lines ([text, colour, size, weight, track]) anchored at a node, queued
    // by rank and drawn after every mark, most wanted first: the selection and needs you (always
    // drawn), hub names, hub sums, working panes, the rest. A block stays inside the canvas, cut
    // to its width and slid in from an edge while its anchor is on screen; one that would land
    // on a block already drawn, or under the HUD, is dropped, so a crowd sheds the quiet ones.
    const tags = [], tag = (rank, A, ...a) => tags.push([rank, A, a]), R0 = roomAt || room();
    const outward = (n, away, rank, A, lines) => { // words beside a node, away from its hub, or on the far side if they would cover it
      const ux = Math.cos(n.out || 0), uy = Math.sin(n.out || 0), side = Math.abs(ux) < 0.3;
      tag(rank, A, lines, n.x + ux * away, n.y + uy * away, side ? 'center' : ux > 0 ? 'left' : 'right', side ? (uy > 0 ? 'top' : 'bottom') : 'middle', n.x - ux * away);
    };
    const taken = [[0, 0, W, R0.top], [0, R0.top + R0.h, W, H], [0, 0, R0.left, H], [W - R0.right, 0, W, R0.tools]]; // the HUD's bands, and the controls' column
    // `fx`: where a pane's block goes, on the far side of its node, when sliding it in would cover the node.
    // A screen rectangle [l, t, r, b] taken for words, unless it lands on one already taken (`force` takes it anyway).
    const claim = (q, force) => { if (!force && taken.some(b => q[0] < b[2] + 4 && q[2] + 4 > b[0] && q[1] < b[3] + 2 && q[3] + 2 > b[1])) return false; taken.push(q); return true; };
    const words = (force, lines, x, y, align, vb, fx) => {
      let sx = x * k + view.x;
      const ns = (fx != null ? (x + fx) / 2 : x) * k + view.x, // the node's own place decides whether it is on screen
 sy = y * k + view.y, R = sy < R0.tools + 20 ? W - R0.right : W - 6; // beside the controls, stop short of them
      let bw = 0, bh = 0;
      for (const l of lines) {
        font(l[2], l[3], l[4] || 0);
        let w = dc.measureText(l[0]).width * k;
        if (w > R - 6) { l[0] = cut(l[0], Math.floor(l[0].length * (R - 6) / w)); w = R - 6; }
        bw = Math.max(bw, w); bh += l[2] + 3;
      }
      const on = ns > 0 && ns < W && sy > R0.top && sy < R0.top + R0.h; // its node is in view, not off the canvas or under the HUD
      if (on && fx != null && (align === 'left' ? R - bw < ns : align === 'right' && 6 + bw > ns)) { x = fx; sx = x * k + view.x; align = align === 'left' ? 'right' : 'left'; } // slid in, it would cover its node
      let L = align === 'center' ? sx - bw / 2 : align === 'left' ? sx : sx - bw;
      const T0 = vb === 'top' ? sy : vb === 'bottom' ? sy - bh : sy - bh / 2, T = on ? Math.max(R0.top, Math.min(R0.top + R0.h - bh, T0)) : T0, dx = on ? Math.max(6, Math.min(R - bw, L)) - L : 0;
      L += dx;
      if (!claim([L, T, L + bw, T + bh], force)) return;
      let ly = (T - view.y) / k;
      for (const [t, c, z, w, tr] of lines) { label(t, x + dx / k, ly, align, c, z, w, 'top', tr); ly += (z + 3) / k; }
      return true;
    };
    const zs = Math.min(2, Math.max(0.55, k)) / k; // a spark grows a little with zoom, and never vanishes
    let parts = 0, pulsing = false, live = false; // live: something is writing or travelling
    const spark = (x, y, a, hot, size = 11) => { stamp(hot, x, y, size * zs, a); parts++; };
    dc.lineCap = 'round';

    // ---- without the hub, each group's disc, under everything ----
    for (const n of hub ? [] : model.nodes) if (n.R) { const A = dim(n.id) * (n.alpha ?? 1), col = n.need ? C.s[0] : n.busy ? C.s[1] : C.muted;
      dc.globalAlpha = A * 0.06; dc.fillStyle = col; disc(n.x, n.y + n.R, n.R); dc.fill(); dc.globalAlpha = A * 0.35; dc.strokeStyle = col; dc.lineWidth = 1 / k; dc.stroke(); }
    // ---- edges: grouping lines (the hub's), sub-agents and pages; tokens a sub-agent writes run up its line ----
    const verbs = [], go = sprite(C.s[1], true);
    for (const e of model.edges) {
      const { a, b } = e, s = stateOf_(b), c = curve(e), A = Math.min(a.alpha ?? 1, b.alpha ?? 1) * dim(a.id, b.id);
      if (b.kind === 'doc') { dc.globalAlpha = A * 0.5; dc.strokeStyle = C.muted; dc.lineWidth = 0.8 / k; dc.setLineDash([2 / k, 3 / k]); path(c); dc.setLineDash([]); continue; }
      const bn = b.kind === 'group' ? b.busy : busy(s) ? 1 : 0, need = b.kind === 'group' ? b.need > 0 : s === 0;
      const color = b.kind === 'group' ? (bn ? C.s[1] : need ? C.s[0] : C.line) : ring(C, s);
      if (bn || need) { dc.globalCompositeOperation = add; dc.globalAlpha = A * (C.dark ? 0.07 : 0.1); dc.strokeStyle = color; dc.lineWidth = (b.kind === 'group' ? 7 : 4.5) / k; path(c); dc.globalCompositeOperation = 'source-over'; }
      dc.globalAlpha = A * (bn || need ? (C.dark ? 0.42 : 0.6) : C.dark ? 0.22 : 0.4); dc.strokeStyle = bn || need ? color : C.dark ? C.line : C.muted; dc.lineWidth = (bn ? 1.1 : 0.8) / k; path(c);
      if (!mini && b.kind === 'pane' && (bn || need) && k > 0.75 && A > 0.5) verbs.push([c, b]);
      const n = b.kind === 'sub' && moving && A > 0.5 ? sparks(rate(subKey(b))) : 0;
      dc.globalCompositeOperation = add;
      for (let i = 0; i < n; i++) { const h = hash(b.id.length * 7 + i); bz(c, 1 - frac(now / 1400 + h)); spark(PX, PY, 0.9, go); } // sub-agent to its pane
      dc.globalCompositeOperation = 'source-over';
      live ||= n > 0;
    }
    // ---- you: lines to the panes you typed into lately and from the ones that need you; the pulses on their way ----
    const you = !mini && { x: (R0.left + R0.w / 2 - view.x) / k, y: (R0.top + R0.h - 16 - view.y) / k }, nowS = Date.now() / 1000;
    if (you) {
      for (const n of model.nodes) {
        if (n.kind !== 'pane') continue;
        const sent = nowS - (n.p.input_at || 0), need = stateOf_(n) === 0, A = dim(n.id) * (n.alpha ?? 1);
        if (sent < RECENT) { dc.globalAlpha = A * (0.12 + 0.3 * (1 - sent / RECENT)); dc.strokeStyle = C.fg; dc.lineWidth = 0.9 / k; arc(you, n); dc.stroke(); }
        if (need) { dc.globalAlpha = A * 0.5; dc.strokeStyle = C.s[0]; dc.lineWidth = 1 / k; dc.setLineDash([3 / k, 4 / k]); arc(n, you); dc.stroke(); dc.setLineDash([]); }
      }
      dc.globalAlpha = 1; dc.fillStyle = C.surface; dc.strokeStyle = C.fg; dc.lineWidth = 1.4 / k; disc(you.x, you.y, 7 / k); dc.fill(); dc.stroke();
      dc.fillStyle = C.fg; disc(you.x, you.y, 2.5 / k); dc.fill();
      tag(0, 1, [['YOU', C.fg, 9.5, 600, 2]], you.x + 12 / k, you.y, 'left', 'middle');
    }
    // ---- nodes ----
    dc.globalCompositeOperation = 'source-over';
    for (const n of model.nodes) {
      const s = stateOf_(n), A = dim(n.id) * (n.alpha ?? 1) * (!mini && n.kind === 'pane' && s === 2 ? fade(n.p) : 1);
      if (n.kind === 'root') { // the hub: a glow, a ring, a lit core; still, as nothing flows through it
        const R = Math.max(n.r, (mini ? 14 : 20) / k);
        dc.globalCompositeOperation = add;
        stamp(sprite(C.s[1], false), n.x, n.y, R * 6, A * (C.dark ? 0.55 : 0.35)); stamp(sprite(C.s[1], true), n.x, n.y, R * 2.2, A * 0.9);
        dc.globalCompositeOperation = 'source-over';
        dc.globalAlpha = A * 0.5; dc.strokeStyle = C.s[1]; dc.lineWidth = 1 / k; disc(n.x, n.y, R * 1.5); dc.stroke();
        const g = dc.createRadialGradient(n.x, n.y - R * 0.2, 0, n.x, n.y, R * 0.75);
        g.addColorStop(0, C.dark ? '#eafff3' : C.s[1]); g.addColorStop(0.55, C.s[1]); g.addColorStop(1, `${C.s[1]}00`);
        dc.globalAlpha = A; dc.fillStyle = g; disc(n.x, n.y, R * 0.75); dc.fill();
        dc.globalAlpha = A * 0.9; dc.lineWidth = 1.4 / k; disc(n.x, n.y, R); dc.stroke();
        if (!mini && k > 0.5) tag(2, A, [[lens === 'session' ? `TMUX · ${new Set(panes.map(p => p.session)).size} SESSIONS` : `${n.children.length} × ${LENSES.find(l => l[0] === lens)[1].toUpperCase()}`, C.fg, 12, 600, 2.4],
          [`${panes.length} panes · ${panes.filter(p => busy(paneState(p))).length} running`, C.muted, 10, 400, 1]], n.x, n.y + R * 1.5 + 9 / k, 'center', 'top'); // small: the HUD says it too
      } else if (n.kind === 'group') { // a hub ring, the share of its panes at work as an arc on it; folded, it grows and counts
        const fold = collapsed.has(n.id), r = (fold ? 1.35 : 1) * Math.max(n.r, (n.depth === 1 ? 9 : 6) / k), { busy: bn, need: nd } = n, tot = n.panes.length, col = nd ? C.s[0] : bn ? C.s[1] : C.muted;
        const at = d => n.y + r + d / k; // below its ring: inside the top of its disc, without the hub
        if (bn || nd) { dc.globalCompositeOperation = add; stamp(sprite(col, false), n.x, n.y, r * 6, A * (C.dark ? 0.5 : 0.3)); dc.globalCompositeOperation = 'source-over'; }
        if (n.warn) { dc.globalAlpha = A; dc.strokeStyle = C.amber; dc.lineWidth = 1.5 / k; dc.setLineDash([4 / k, 3 / k]); disc(n.x, n.y, r + 7 / k); dc.stroke(); dc.setLineDash([]); }
        dc.globalAlpha = A; dc.fillStyle = C.surface; disc(n.x, n.y, r); dc.fill();
        dc.globalAlpha = A * (bn || nd ? 0.9 : 0.6); dc.strokeStyle = col; dc.lineWidth = 1.2 / k; dc.stroke();
        let a0 = -Math.PI / 2; dc.lineWidth = 2.6 / k; dc.globalAlpha = A;
        for (const [cnt, cc] of [[nd, C.s[0]], [bn, C.s[1]]]) if (cnt) { const a1 = a0 + cnt / tot * Math.PI * 2; dc.strokeStyle = cc; dc.beginPath(); dc.arc(n.x, n.y, r + 3 / k, a0, a1); dc.stroke(); a0 = a1; }
        if (fold) label(String(tot), n.x, n.y, 'center', C.fg, 11, 700);
        else { dc.fillStyle = col; disc(n.x, n.y, r * 0.34); dc.fill(); }
        dc.globalAlpha = A;
        if (n.depth === 1) { // its name, and what it adds up to
          const cost = n.panes.reduce((t, p) => t + costOf(p), 0);
          tag(2, A, [[String(n.label).toUpperCase(), C.fg, mini ? 9.5 : 11, 600, 2]], n.x, at(8), 'center', 'top');
          const sums = [nd && `${nd} need you`, bn && `${bn} running`, n.subs && `${n.subs} sub-agent${n.subs > 1 ? 's' : ''}`, cost && `$${cost.toFixed(2)}`].filter(Boolean), line = t => [[t, nd ? C.amber : C.muted, 9.5, 400]];
          if (!mini) { tag(nd ? 2.5 : 3, A, line(sums.join(' · ') || `${tot} idle`), n.x, at(25), 'center', 'top'); if (sums.length > 1) tags.at(-1).push(line(sums[0])); }
        } else if (!mini) tag(n.warn ? 1 : 3, A, [[n.label + (n.warn ? ' · 2 agents busy' : ''), n.warn ? C.amber : C.muted, 10, 400, 0.5]], n.x, n.y + r + (n.warn ? 9 : 6) / k, 'center', 'top');
      } else if (n.kind === 'pane') {
        const r = rad(n), col = ring(C, s), on = busy(s) || s === 0, p = n.p;
        if (on) { dc.globalCompositeOperation = add; stamp(sprite(col, false), n.x, n.y, r * 5, A * (C.dark ? 0.75 : 0.45)); dc.globalCompositeOperation = 'source-over'; }
        const sp = moving && A > 0.5 ? sparks(rate(n.id)) : 0; // tokens it is writing, thrown off its rim
        if (sp) { dc.globalCompositeOperation = add; for (let i = 0; i < sp; i++) { const h = hash(n.id.length * 13 + i), q = frac(now / 1400 + h), a = hash(i * 7.3 + 1) * 6.283, d = r + (3 + q * 30) / k; spark(n.x + d * Math.cos(a), n.y + d * Math.sin(a), 1 - q, go); } dc.globalCompositeOperation = 'source-over'; live = true; }
        if (s === 0) { pulsing = moving; const q = moving ? frac(now / 1600) : 0.35; dc.globalAlpha = A * (1 - q) * 0.8; dc.strokeStyle = col; dc.lineWidth = 1.2 / k; disc(n.x, n.y, r + (3 + q * 12) / k); dc.stroke(); }
        dc.globalAlpha = A; dc.fillStyle = C.surface; disc(n.x, n.y, r); dc.fill();
        dc.globalAlpha = A * (on ? 1 : 0.75); dc.strokeStyle = col; dc.lineWidth = (on ? 1.6 : 1) / k; dc.stroke();
        const img = logo(p.tool);
        if (img?.complete && img.naturalWidth && r * k > 8) {
          dc.globalAlpha = A * (on ? 1 : 0.7); dc.save(); disc(n.x, n.y, r * 0.72); dc.clip(); dc.drawImage(img, n.x - r * 0.72, n.y - r * 0.72, r * 1.44, r * 1.44); dc.restore();
        } else { dc.globalAlpha = A * (on ? 1 : 0.8); dc.fillStyle = col; disc(n.x, n.y, r * (on ? 0.5 : 0.28)); dc.fill(); }
        if (n === sel) { // brackets, as a sight picks its target
          const q = r + 7 / k, l = 5 / k; dc.globalAlpha = 1; dc.strokeStyle = C.fg; dc.lineWidth = 1.4 / k; dc.beginPath();
          for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) { dc.moveTo(n.x + sx * q, n.y + sy * (q - l)); dc.lineTo(n.x + sx * q, n.y + sy * q); dc.lineTo(n.x + sx * (q - l), n.y + sy * q); }
          dc.stroke();
        }
        if (!mini) paneWords(n, r, s, on, A, marks(n, r, s, on, A), outward);
      } else if (n.kind === 'doc') { // a scratch page the pane made: a sheet, ringed by a turning arc while it is being made
        const r = rad(n), w = r * 0.8, f = r * 0.45, made = !n.d.pending, col = made ? C.muted : C.accent;
        dc.globalAlpha = A; dc.fillStyle = C.surface; dc.strokeStyle = col; dc.lineWidth = 1.2 / k; dc.beginPath();
        dc.moveTo(n.x - w, n.y - r); dc.lineTo(n.x + w - f, n.y - r); dc.lineTo(n.x + w, n.y - r + f); dc.lineTo(n.x + w, n.y + r); dc.lineTo(n.x - w, n.y + r); dc.closePath(); dc.fill(); dc.stroke();
        if (!made) { const a = moving ? now / 300 : 0; dc.lineWidth = 1.6 / k; dc.beginPath(); dc.arc(n.x, n.y, r + 4 / k, a, a + 4); dc.stroke(); live ||= moving; }
        const close = n === hover || n === sel;
        if (!mini && (close || k > 1.3)) outward(n, r + 5 / k, close ? 0 : 5, A, [[made ? cut(n.d.title, 24) : `${cut(n.d.title, 18)} · being made`, made ? C.muted : C.accent, 9, 500]]);
      } else { // a sub-agent: a small pane of its own, lit while it works, its job and time beside it where they fit
        const r = rad(n), col = ring(C, s), on = busy(s), fa = A * (on ? 1 : 0.6);
        if (on) { dc.globalCompositeOperation = add; stamp(sprite(col, false), n.x, n.y, r * 5, fa * (C.dark ? 0.7 : 0.4)); dc.globalCompositeOperation = 'source-over'; }
        dc.globalAlpha = fa; dc.fillStyle = C.surface; disc(n.x, n.y, r); dc.fill(); dc.strokeStyle = col; dc.lineWidth = 1.3 / k; dc.stroke();
        dc.fillStyle = col; disc(n.x, n.y, r * 0.42); dc.fill();
        const close = n === hover || n === sel;
        if (!mini && n.a?.label && (close || k > 0.6)) outward(n, r + 5 / k, close ? 0 : 4.5, fa, [[[cut(n.a.label, 22), n.a.elapsed].filter(Boolean).join(' · '), on ? C.accent : C.muted, 9, 500]]);
      }
    }
    dc.globalCompositeOperation = add;
    for (let i = pulses.length - 1; i >= 0; i--) { // one event each, from where it came from to where it went, then a ring where it landed
      const q = pulses[i], t = (performance.now() - q.t0) / PULSE, A = spot(q.from, you), B = spot(q.to, you);
      if (t > 1.3 || !A || !B) { pulses.splice(i, 1); continue; }
      live = true;
      if (t < 0 || !moving) continue;
      const u = Math.min(1, t), e = u < 0.5 ? 2 * u * u : 1 - (-2 * u + 2) ** 2 / 2, col = typeof q.color === 'number' ? C.s[q.color] : C[q.color], hot = sprite(col, true);
      for (let j = 0; j < 8; j++) { const p = along(A, B, Math.max(0, e - j * 0.03)); spark(p.x, p.y, (1 - j / 8) * Math.min(1, 4 * (1.3 - t)), hot, 26 - 2 * j); } // a bright head, a fading tail
      if (t > 0.85) { const r = (t - 0.85) / 0.45; dc.globalAlpha = 1 - r; dc.strokeStyle = col; dc.lineWidth = 2 / k; disc(B.x, B.y, (B.r ? rad(B) : 6 / k) + 26 * r / k); dc.stroke(); }
    }
    dc.globalCompositeOperation = 'source-over';
    tags.sort((a, b) => a[0] - b[0]).forEach(([rank, A, a, b]) => { dc.globalAlpha = A; if (!words(rank < 2, ...a) && b) words(false, b, ...a.slice(1)); }); // b: a shorter say, where the long one has no room
    // Last, what each running pane is doing, along its edge, where the edge and the words around it leave room (the popover says it either way).
    for (const [c, b] of verbs) {
      const p = b.p, s = stateOf_(b), w = p.working, text = s === 0 ? 'needs you' : w?.verb ? [w.verb, w.elapsed].filter(Boolean).join(' · ') : cut(p.status_line || p.headline, 30);
      font(9, 500, 0.3);
      const tw = dc.measureText(text).width;
      if (!text || Math.hypot(c[6] - c[0], c[7] - c[1]) * 0.8 < tw + 30 / k) continue;
      bz(c, 0.55); let ang = Math.atan2(NX, -NY); if (Math.cos(ang) < 0) ang += Math.PI;
      const cs = Math.cos(ang), sn = Math.sin(ang), hw = tw * k / 2, hh = 6, cx = PX * k + view.x + sn * 11, cy = PY * k + view.y - cs * 11; // its rotated box, centred above the edge
      const ex = Math.abs(cs) * hw + Math.abs(sn) * hh, ey = Math.abs(sn) * hw + Math.abs(cs) * hh;
      if (!claim([cx - ex, cy - ey, cx + ex, cy + ey])) continue; // under the HUD or on other words
      dc.save(); dc.translate(PX, PY); dc.rotate(ang); dc.globalAlpha = 0.9;
      label(text, 0, -5 / k, 'center', s === 0 ? C.amber : C.accent, 9, 500, 'bottom', 0.3);
      dc.restore();
    }
    dc.globalAlpha = 1;
    if (spacing) dc.letterSpacing = '0px';
    flowed = moving && (live || pulsing); // nothing writing, travelling or asking: no frames
    cost += (performance.now() - t0 - cost) * 0.1; particles = parts;
  }
  // A pane's marks: its size is the context it has used (build), an arc round it the same share,
  // hot past 80%; a badge counts its sub-agents; a dashed ring is its account when there are several.
  function marks(n, r, s, on, A) {
    const p = n.p, k = view.k, ctxPct = p.context_pct, acc = k > 0.6 && ctx.several() && agent(p) && ctx.owner(p.pane_id), subs = subCount(p);
    if (ctxPct != null && k * r > 5) { dc.globalAlpha = A * 0.9; dc.strokeStyle = ctxPct >= 80 ? C.amber : C.muted; dc.lineWidth = 1.8 / k; dc.beginPath(); dc.arc(n.x, n.y, r + 3 / k, -Math.PI / 2, -Math.PI / 2 + ctxPct / 50 * Math.PI); dc.stroke(); }
    if (acc) { dc.globalAlpha = A * 0.7; dc.strokeStyle = C.acct[(acc.color || 0) % C.acct.length]; dc.lineWidth = 1 / k; dc.setLineDash([2 / k, 2 / k]); disc(n.x, n.y, r + 6.5 / k); dc.stroke(); dc.setLineDash([]); }
    if (subs) {
      const bx = n.x + r * 0.78, by = n.y - r * 0.78;
      dc.globalAlpha = A; dc.fillStyle = C.s[1]; disc(bx, by, 6.5 / k); dc.fill(); dc.strokeStyle = C.surface; dc.lineWidth = 1.5 / k; dc.stroke();
      dc.font = `700 ${8.5 / k}px ${C.font}`; dc.textAlign = 'center'; dc.textBaseline = 'middle'; dc.fillStyle = C.dark ? C.surface : C.fg; dc.fillText(String(subs), bx, by + 0.5 / k);
    }
    return acc;
  }
  // Its words, outward from the hub, arriving by zoom: working names from 0.3 (where they fit),
  // the headline or question from 1.25, idle names from 1.3, the numbers from 2. A pane that
  // needs you always says who it is and how long it has waited.
  function paneWords(n, r, s, on, A, acc, outward) {
    const p = n.p, k = view.k, close = n === hover || n === sel;
    if (!(close || s === 0 || k > 1.3 || (k > 0.3 && on))) return;
    const lines = [[paneName(p), on ? C.fg : C.muted, 10.5, on ? 600 : 400]];
    if (s === 0 || s === 5) lines.push([`waiting ${since(p) || 'now'}`, s === 0 ? C.amber : C.muted, 9.5, 600]);
    const head = (k > 1.25 || close) && (s === 0 ? p.question?.prompt : paneHeadline(p));
    if (head) lines.push([cut(head, k > 2 ? 54 : 34), C.muted, 9.5, 400]);
    if (k > 2) lines.push([[p.model, p.context_pct != null && `ctx ${p.context_pct}%`, p.working?.tokens, s === 2 && `idle ${since(p)}`, p.cost, acc?.name].filter(Boolean).join(' · '), C.muted, 9, 400]);
    const sat = n.subs.reduce((m, x) => Math.max(m, n.r + 3 * rad(x) + 26), 0); // past its satellites, which sit the same way out
    outward(n, Math.max(r + (acc ? 10 : 7) / k, sat), close ? 0 : s === 0 ? 1 : on ? 4 : 5, A, lines);
  }
  // Frames run while something moves and the canvas is on screen; a hidden page costs nothing.
  let seen = false; // on screen: a hidden page or a card scrolled away costs nothing
  new IntersectionObserver(([e]) => { seen = e.isIntersecting; if (seen) animate(); }).observe(cv);
  const loop = t => { raf = 0; if (!seen) return; draw(t); if (anim || flowed) raf = requestAnimationFrame(loop); };
  const animate = () => { if (!raf) raf = requestAnimationFrame(loop); };
  matchMedia('(prefers-reduced-motion: reduce)').addEventListener?.('change', () => { draw(); animate(); });
  new MutationObserver(() => draw()).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] }); // the theme: draw() rereads the palette

  // The canvas less the HUD's chrome (ctx.inset), kept for draw() to keep words out from under it.
  function room() { const { top = 10, bottom = 0, left = 0, right = 0, tools = 0 } = ctx.inset?.() || {}; return roomAt = { top, left, right, tools, w: W - left - right, h: H - top - bottom }; }
  function fitView() { // the graph framed inside the HUD's chrome (ctx.inset), not under it
    const R = n => n.R || 0, xs = model.nodes.flatMap(n => [n.tx - R(n), n.tx + R(n)]), ys = model.nodes.flatMap(n => [n.ty - (R(n) && 30), n.ty + 2 * R(n)]), { top, left, w, h } = room(); // a disc hangs below its group's node, its name above
    const x0 = Math.min(...xs) - PAD, x1 = Math.max(...xs) + PAD, y0 = Math.min(...ys) - PAD, y1 = Math.max(...ys) + PAD;
    const k = Math.max(K_MIN, Math.min(3, w / (x1 - x0), h / (y1 - y0)));
    return { k, x: left + w / 2 - (x0 + x1) / 2 * k, y: top + h / 2 - (y0 + y1) / 2 * k };
  }
  // Rebuild and glide to it. A structure unchanged since the last poll only redraws; `refit`
  // re-frames the view too (a lens change), where a poll or a fold keeps the user's pan and zoom.
  function relayout(refit) {
    const next = build();
    if (!W) { model = next; return; }
    if (model?.sig === next.sig && !refit) { model = next; layout(model); draw(); return animate(); }
    const old = new Map(model ? model.nodes.filter(n => !Number.isNaN(n.x)).map(n => [n.id, n]) : []); // placed ones
    model = next; layout(model);
    const first = old.size < 2; // nothing placed but the root (an empty fleet): frame what arrives
    const v1 = refit || first || mini ? fitView() : { ...view }; // the card has no camera of its own to keep
    const fromOf = n => { if (old.has(n.id)) return [n.x, n.y, false];
      const u = n.up && old.has(n.up.id) ? n.up : null; return u ? [u.x, u.y, true] : [n.tx, n.ty, true]; };
    model.nodes.forEach(n => { const [fx, fy, fresh] = fromOf(n); Object.assign(n, { fx, fy, fresh }); });
    if (first || !motion()) { model.nodes.forEach(n => Object.assign(n, { x: n.tx, y: n.ty, alpha: 1, fresh: false })); Object.assign(view, v1); anim = null; draw(); return animate(); }
    anim = { t0: performance.now(), dur: 650, v0: { ...view }, v1 };
    animate();
  }
  // The user (or a resize) takes the camera mid-glide: the nodes land where they were going, the view stays put.
  function land() { if (!anim) return; anim = null; model.nodes.forEach(n => Object.assign(n, { x: n.tx, y: n.ty, alpha: 1, fresh: false })); }
  function size() {
    const b = box.getBoundingClientRect();
    if (b.width < 20 || b.height < 60) return;
    const first = !W;
    if (b.width === W && b.height === H) { draw(); return animate(); } // reattached as it was
    const dx = (b.width - W) / 2, dy = (b.height - H) / 2;
    W = b.width; H = b.height;
    if (first) return relayout(true);
    land(); // a glide's endpoints were for the old size
    refreshPop(true); // placed, and sheet or not, for the new size
    // A view the user framed keeps its pan and zoom, recentred; an untouched one refits.
    layout(model); model.nodes.forEach(n => { n.x = n.tx; n.y = n.ty; });
    Object.assign(view, touched ? { x: view.x + dx, y: view.y + dy } : fitView()); draw(); animate();
  }
  new ResizeObserver(size).observe(box);
  const close = () => { pop?.remove(); pop = null; sel = null; draw(); };
  const api = {
    update(list) {
      pages = mini ? {} : (ctx.pages || scratchPages)();
      track(list); panes = list; relayout(false);
      // A node gone takes its hover, selection and popover with it; an open popover follows the
      // live data, in place, focus kept.
      if (hover && !model.nodes.includes(hover)) { hover = null; draw(); }
      if (sel && !model.nodes.includes(sel)) return close();
      if (!pop) return;
      refreshPop();
    },
    fit() { touched = false; size(); anim = { t0: performance.now(), dur: motion() ? 400 : 1, v0: { ...view }, v1: fitView() }; model.nodes.forEach(n => { n.fx = n.x; n.fy = n.y; }); animate(); },
    setLens(l) { lens = l; close(); relayout(true); ctx.onChange?.(); }, lens: () => lens,
    setHub(on) { hub = on; close(); relayout(true); ctx.onChange?.(); }, hub: () => hub,
    rate: () => [...last.keys()].reduce((t, id) => t + rate(id), 0) + (model?.nodes || []).reduce((t, n) => t + (n.kind === 'sub' ? rate(subKey(n)) : 0), 0), // tokens a second, the fleet's
    zoom(f) { zoomAt(W / 2, H / 2, f); },
    // Glide to a pane, unfolding its hubs, and open its popover there.
    focus(id) {
      const n = cache.get(id);
      if (!n) return;
      let fold = false;
      for (let u = n.up; u; u = u.up) fold = collapsed.delete(u.id) || fold;
      if (fold) relayout(false);
      touched = true; land();
      const k = Math.max(view.k, phone() ? 1.1 : 1.4);
      anim = { t0: performance.now(), dur: motion() ? 500 : 1, v0: { ...view }, v1: { k, x: W / 2 - n.tx * k, y: H / 2 - n.ty * k }, then: () => tap({ x: W / 2, y: H / 2 }, n) };
      model.nodes.forEach(m => { m.fx = m.x; m.fy = m.y; }); animate();
    },
    hud() { room(); draw(); }, // the HUD changed shape (callouts folded): words keep clear of it, the camera stays
    perf: () => ({ cost, particles }),
  };
  if (mini) return api;

  // Every node as a button too, for the keyboard and screen readers: focus selects it on the
  // canvas, Enter does what a tap does (a pane's popover, a hub's fold).
  const buttons = el('div', 'fg-nodes');
  box.append(buttons);
  const say = n => n.kind === 'group' ? `${n.label}: ${n.panes.length} panes, ${collapsed.has(n.id) ? 'folded' : 'unfolded'}`
    : n.kind === 'pane' ? `${paneName(n.p)}: ${STATES[paneState(n.p)]}` : n.kind === 'doc' ? `Scratch page ${n.d.title} by ${paneName(n.parent.p)}` : `Sub-agent of ${paneName(n.parent.p)}`;
  listNodes = () => {
    if (buttons._sig !== model.sig) {
      const had = document.activeElement?._n?.id;
      buttons._sig = model.sig;
      buttons.replaceChildren(...model.nodes.filter(n => n.kind !== 'root').map(n => Object.assign(el('button'), { _n: n,
        onfocus: () => { if (sel !== n) { pop?.remove(); pop = null; } sel = n; draw(); }, // another node's popover goes
        onclick: () => { tap({ x: n.x * view.k + view.x, y: n.y * view.k + view.y }, n); pop?.querySelector('[data-open]')?.focus(); } })));
      [...buttons.children].find(b => b._n.id === had)?.focus(); // a fold rebuilt the list: stay on its hub
    }
    for (const b of buttons.children) b.textContent = say(b._n);
  };

  // ---- pan, pinch, wheel, hover, tap ----
  function zoomAt(x, y, f) { touched = true; land(); const k = Math.max(K_MIN, Math.min(4, view.k * f)); view.x = x - (x - view.x) * k / view.k; view.y = y - (y - view.y) * k / view.k; view.k = k; draw(); }
  const world = (x, y) => ({ x: (x - view.x) / view.k, y: (y - view.y) / view.k });
  function hit(x, y) {
    const w = world(x, y), slop = n => Math.max(rad(n), (phone() ? 18 : 10) / view.k);
    let best = null, bd = Infinity;
    for (const n of model.nodes) { const d = Math.hypot(w.x - n.x, w.y - n.y); if (d < slop(n) && d < bd) { best = n; bd = d; } }
    return best;
  }
  const pts = new Map();
  let start = null, pinch = null, moved = false;
  const pos = e => { const b = cv.getBoundingClientRect(); return { x: e.clientX - b.left, y: e.clientY - b.top }; };
  cv.addEventListener('pointerdown', e => {
    cv.setPointerCapture(e.pointerId); moved = pts.size > 0; pts.set(e.pointerId, pos(e)); land(); // a second finger is never a tap
    const [p, q] = [...pts.values()];
    start = { ...p, vx: view.x, vy: view.y };
    pinch = q && { d: Math.hypot(p.x - q.x, p.y - q.y), k: view.k, mx: (p.x + q.x) / 2, my: (p.y + q.y) / 2, vx: view.x, vy: view.y };
  });
  cv.addEventListener('pointermove', e => {
    const at = pos(e);
    if (!pts.has(e.pointerId)) { if (e.pointerType === 'mouse') { const h = hit(at.x, at.y); if (h !== hover) { hover = h; cv.style.cursor = h ? 'pointer' : 'grab'; draw(); } } return; }
    pts.set(e.pointerId, at); land(); // a glide begun mid-gesture (a poll's relayout) yields the camera
    const [p, q] = [...pts.values()];
    if (pinch && q) {
      const k = Math.max(K_MIN, Math.min(4, pinch.k * Math.hypot(p.x - q.x, p.y - q.y) / pinch.d)), mx = (p.x + q.x) / 2, my = (p.y + q.y) / 2;
      view.x = mx - (pinch.mx - pinch.vx) / pinch.k * k; view.y = my - (pinch.my - pinch.vy) / pinch.k * k; view.k = k; moved = touched = true;
    } else if (start) {
      if (Math.abs(at.x - start.x) + Math.abs(at.y - start.y) > 6) moved = true;
      if (moved) { touched = true; view.x = start.vx + at.x - start.x; view.y = start.vy + at.y - start.y; cv.style.cursor = 'grabbing'; }
    }
    draw();
  });
  const up = e => {
    const at = pos(e), wasTap = !moved && pts.size === 1;
    pts.delete(e.pointerId); cv.style.cursor = 'grab';
    const rest = [...pts.values()][0];
    start = rest ? { ...rest, vx: view.x, vy: view.y } : null; pinch = null;
    if (wasTap && e.type === 'pointerup') tap(at);
  };
  cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', up);
  cv.addEventListener('pointerleave', () => { if (hover && !pts.size) { hover = null; draw(); } });
  cv.addEventListener('wheel', e => { e.preventDefault(); const at = pos(e); zoomAt(at.x, at.y, Math.exp(-e.deltaY * [1, 16, H][e.deltaMode] * (e.ctrlKey ? 0.01 : 0.0015))); }, { passive: false }); // deltaY in px, ~16px lines, or pages
  // A drag on the graph pans it: not the app's edge swipe back (app.js), which listens on window.
  for (const t of ['touchstart', 'touchend']) box.addEventListener(t, e => e.stopPropagation());

  // The open popover redrawn from its node's latest data, keyboard focus kept; `moved` redraws
  // it even unchanged, to place it again.
  function refreshPop(moved) {
    if (!pop) return;
    const focused = pop.contains(document.activeElement) && document.activeElement.className;
    if (moved) pop._html = null;
    tap(pop._at, sel);
    if (focused) pop.querySelector(`.${focused.split(' ')[0]}`)?.focus();
  }
  function popover(html, at, n) {
    if (pop?._html === html && sel === n) return; // a poll that changed nothing in it
    pop?.remove();
    pop = Object.assign(el('div', `fg-pop${phone() ? ' sheet' : ''}`, html), { _at: at, _html: html });
    box.append(pop);
    if (!phone()) { pop.style.left = `${Math.min(W - 292, Math.max(8, at.x + 18))}px`; pop.style.top = `${Math.max(8, Math.min(H - pop.offsetHeight - 8, at.y - 30))}px`; }
    pop.querySelector('.fg-x').onclick = () => { // the keyboard goes back to the node it came from
      const back = pop.contains(document.activeElement) && [...buttons.children].find(b => b._n === sel);
      close(); back?.focus();
    };
    pop.querySelector('[data-open]').onclick = () => n.kind === 'doc' ? window.open(`/scratch/${n.d.href}`, '_blank', 'noopener') : ctx.open(n.kind === 'sub' ? n.parent.id : n.id);
    sel = n; draw();
  }
  const head = (lead, title) => `<div class="fg-pop-head">${lead}<b>${esc(title)}</b><button class="fg-x" aria-label="Close">${ctx.icon('x', 16)}</button></div>`;
  const stLine = (s, extra) => `<span class="fg-st"><i style="background:${ring(C, s)}"></i>${STATES[s]}${extra ? ` · ${esc(extra)}` : ''}</span>`;
  const dl = rows => `<dl>${rows.filter(([, v]) => v).map(([t, v]) => `<dt>${t}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
  // "3 running, 1 waiting": the satellites name them; the ones counted but not listed are busy.
  const agentTally = (p, subs) => { const t = {}, list = subsList(p); [...list, ...Array(subs - list.length).fill(null)].forEach(a => { const k = STATES[subState(a)].toLowerCase(); t[k] = (t[k] || 0) + 1; });
    return Object.entries(t).map(([k, n]) => `${n} ${k}`).join(', '); };
  const openBtn = '<button class="fg-btn" data-open>Open pane</button>', tps = r => r >= 1 && `${Math.round(r)} tokens/s`;
  function tap(at, n = hit(at.x, at.y)) {
    if (!n) return close();
    if (n.kind === 'root') { close(); return api.fit(); }
    if (n.kind === 'group') { collapsed.has(n.id) ? collapsed.delete(n.id) : collapsed.add(n.id); close(); return relayout(false); }
    if (n.kind === 'doc') return popover(`${head(ctx.icon('fileText', 16), n.d.title)}<span class="fg-st">${n.d.pending ? 'Being made' : 'Scratch page'}</span>
        ${dl([['By', paneName(n.parent.p)]])}<button class="fg-btn" data-open>Open page</button>`, at, n);
    if (n.kind === 'sub') {
      const a = n.a || {}, st = subState(n.a);
      return popover(`${head(`<i class="fg-dot" style="background:${ring(C, st)}"></i>`, a.label || 'Sub-agent')}${stLine(st, 'sub-agent')}
        ${dl([['Pane', paneName(n.parent.p)], ['Tokens', a.tokens], ['Writing', tps(rate(subKey(n)))], ['Time', a.elapsed]])}${openBtn}`, at, n);
    }
    const p = n.p, s = paneState(p), w = p.worktree, subs = subCount(p), src = ctx.logos[p.tool], q = s === 0 && p.question?.prompt && p.question;
    popover(`${head(src ? `<img class="fg-logo" src="${esc(src)}" alt="">` : '', paneName(p))}${stLine(s, p.session)}
      ${q ? `<div class="fg-q">${esc(q.prompt)}${Array.isArray(q.options) && q.options.length ? `<div>${q.options.map(o => `<span>${esc(o)}</span>`).join('')}</div>` : ''}</div>` : ''}
      ${dl([['Model', p.model], ['Context', p.context_pct != null && `${p.context_pct}% used`], ['Cost', p.cost], ['Account', ctx.owner(p.pane_id)?.name], ['Tokens', p.working?.tokens], ['Writing', tps(rate(n.id))], ['Agents', subs && agentTally(p, subs)],
        ['Now', paneHeadline(p)], ['Time', (busy(s) && p.working?.elapsed) || (since(p) && `${s === 2 ? 'idle' : 'for'} ${since(p)}`)],
        ['Where', `${home(p.cwd || '')}${w?.branch ? ` @ ${w.branch}` : ''}`]])}${openBtn}`, at, n);
  }
  return api;
}

// The view: the graph filling it, under a HUD of lens chips, the counts, jump-to callouts and the
// key. Built once and kept, so a visit back finds the lens, folds and places it left.
export function renderGraphView(root, panes, ctx) {
  if (!root._g) {
    const lenses = el('div', 'fg-lenses', '<span>Group by</span>'), box = el('div', 'fg-box'), tools = el('div', 'fg-tools'), hud = el('div', 'fg-hud');
    const stats = el('div', 'fg-stats'), legend = el('div', 'fg-legend'), call = el('details', 'fg-call', '<summary>Jump to</summary><div></div>');
    call.open = true;
    call.addEventListener('toggle', () => root._g?.hud());
    for (const [name, label] of LENSES) { const b = el('button', '', label); b.dataset.lens = name; b.onclick = () => { root._lens = null; g.setLens(name); }; lenses.append(b); } // a pick ends any pending restore
    const at = e => e.getBoundingClientRect(), inset = () => { // what the HUD covers, for Fit
      const b = at(box), ph = phone();
      return { top: Math.max(at(lenses).bottom, at(call).bottom) - b.top + 6, bottom: b.bottom - at(ph ? stats : legend).top + 6, left: ph ? 0 : at(stats).right - b.left + 6, right: 52, tools: at(tools).bottom - b.top + 6 };
    };
    let hubOn = true;
    try { hubOn = localStorage.getItem(HUB) !== '0'; } catch {}
    const g = root._g = fleetGraph(box, false, { ...ctx, hub: hubOn, inset, onChange: () => { try { localStorage.setItem('tmuxrc-graph-lens', g.lens()); localStorage.setItem(HUB, g.hub() ? '1' : '0'); } catch {} paint(); } });
    const dark = () => { const on = document.documentElement.classList.toggle('fg-dark'); try { localStorage.setItem(DARK, on ? '1' : ''); } catch {} paint(); };
    // Full screen heads the column, in and out, so the way back is where the way in was.
    for (const [name, label, fn] of [['fullscreen', 'Full screen', () => full(!root.classList.contains('full'))], ['plus', 'Zoom in', () => g.zoom(1.35)], ['minus', 'Zoom out', () => g.zoom(1 / 1.35)],
      ['locate', 'Fit to view', () => g.fit()], ['network', 'Hub', () => g.setHub(!g.hub())], ['moon', 'Dark canvas', dark]]) {
      const b = el('button', '', ctx.icon(name, 18)); b.title = b.ariaLabel = label; b.onclick = fn; tools.append(b);
    }
    // Full screen is the view itself pinned over the page (style.css), not the Fullscreen API,
    // which an iPhone grants only to video. The page under it goes inert, so Tab stays in the
    // graph; the view leaving the screen (any navigation) ends it.
    const zoomBtn = tools.firstChild, darkBtn = tools.lastChild, hubBtn = darkBtn.previousSibling, full = on => {
      if (on === root.classList.contains('full')) return;
      if (on) { root._inert = []; for (let a = root; a.parentElement && a !== document.body; a = a.parentElement) root._inert.push(...[...a.parentElement.children].filter(s => s !== a && !s.inert)); }
      root._inert.forEach(s => { s.inert = on; });
      root.classList.toggle('full', on); zoomBtn.innerHTML = ctx.icon(on ? 'minimize' : 'fullscreen', 18);
      zoomBtn.title = zoomBtn.ariaLabel = on ? 'Exit full screen' : 'Full screen'; g.fit();
    };
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && root.classList.contains('full')) full(false); });
    new IntersectionObserver(([e]) => { if (!e.isIntersecting) full(false); }).observe(root);
    call.lastChild.onclick = e => { const b = e.target.closest('button'); if (b) g.focus(b.dataset.id); };
    hud.append(lenses, call, stats, legend);
    box.append(hud, tools);
    root.append(box);
    const paint = root._paint = () => {
      const lens = g.lens(), list = root._panes, c = [0, 0, 0, 0, 0, 0], subs = list.reduce((t, p) => t + subCount(p), 0); // all of them, where the canvas draws MAX_SUBS
      list.forEach(p => c[paneState(p)]++);
      const tps = Math.round(g.rate());
      const oldest = list.reduce((a, p) => !a || ageOf(p) > ageOf(a) ? p : a, null), need = list.filter(p => paneState(p) === 0).sort((a, b) => ageOf(b) - ageOf(a))[0];
      const dear = list.reduce((a, p) => costOf(p) > (a ? costOf(a) : 0) ? p : a, null), spend = list.reduce((t, p) => t + costOf(p), 0);
      stats.innerHTML = [['', new Set(list.map(p => p.session)).size, 'Sessions'], ['go', `${c[1] + c[4] + c[5]}<small>/${list.length}</small>`, 'Running'], [c[0] ? 'need' : '', c[0], 'Needs you'],
        ['opt', subs, 'Sub-agents'], ['', tps >= 1000 ? `${(tps / 1000).toFixed(1)}k` : tps, 'Tokens / s'], ['', `$${spend.toFixed(spend >= 100 ? 0 : 2)}`, 'Cost'], ['opt', oldest ? since(oldest) || '–' : '–', 'Oldest pane']]
        .map(([cls, v, l]) => `<div class="${cls}"><b>${v}</b><span>${l}</span></div>`).join('');
      legend.innerHTML = `<p>${LEGEND[lens]} ${g.hub() ? 'Lines from the centre group panes; nothing flows along them.' : 'Each disc is a group: tap its name to fold it.'} Size and arc: context used.
        Sparks are tokens being written, one for every ${SPARK} a second. A line from you: a pane you typed into from here in the last ${RECENT / 60} minutes; dashed amber, one that needs you. A dot runs along when it happens.</p>
        <div class="fg-key"><span><i class="fg-k-spark"></i>${SPARK} tok/s</span><span><i class="fg-k-you"></i>you</span><span><i class="fg-k-doc"></i>page</span><span><i class="fg-k-doc busy"></i>page being made</span></div><div class="fg-key">${[1, 0, 2, 4, 5].map(i => `<span><i style="background:${i === 2 ? 'var(--muted)' : `var(--chart-s${i})`}"></i>${STATES[i]}</span>`).join('')}${
        ctx.several() ? '<span><i class="fg-acct-key"></i>account</span>' : ''}${lens === 'worktree' ? '<span><i class="fg-warn-key"></i>two agents, one checkout</span>' : ''}</div>`;
      // The callouts' buttons are kept and their words updated in place, so a poll mid-tap or under focus loses neither.
      const items = [need && ['need', 'Longest waiting', need, since(need) || 'now'], dear && ['', 'Most expensive', dear, dear.cost]].filter(Boolean), slots = call.lastChild;
      while (slots.children.length < items.length) slots.append(el('button', '', '<span></span><b></b><em></em>'));
      [...slots.children].forEach((b, i) => {
        const it = items[i]; b.hidden = !it;
        if (!it) return;
        const [cls, t, p, v] = it; b.className = cls; b.dataset.id = p.pane_id;
        [t, `${p.session} / ${cut(paneName(p), 22)}`, v].forEach((x, j) => { if (b.children[j].textContent !== x) b.children[j].textContent = x; });
      });
      call.hidden = !items.length;
      darkBtn.setAttribute('aria-pressed', String(document.documentElement.classList.contains('fg-dark'))); hubBtn.setAttribute('aria-pressed', String(g.hub()));
      for (const b of lenses.querySelectorAll('button')) { b.hidden = b.dataset.lens === 'account' && !ctx.several(); b.setAttribute('aria-pressed', String(b.dataset.lens === lens)); }
    };
    let saved = 'session';
    try { saved = localStorage.getItem('tmuxrc-graph-lens') || saved; } catch {}
    root._lens = saved;
  }
  root._panes = panes;
  const g = root._g, want = root._lens;
  // The remembered lens once it can show: Account waits for the accounts, and is gone with one.
  if (want && LENSES.some(l => l[0] === want) && (want !== 'account' || ctx.several())) { root._lens = null; if (g.lens() !== want) g.setLens(want); }
  if (g.lens() === 'account' && !ctx.several()) g.setLens('session');
  root._paint();
  g.update(panes);
  return root;
}

// The Dashboard's card: the session lens at card size, live, every tap opening the view.
export function renderGraphCard(card, panes, ctx) {
  const box = card._box ||= card.appendChild(el('div', 'fg-mini'));
  (card._g ||= fleetGraph(box, true, ctx)).update(panes);
  const running = panes.filter(p => busy(paneState(p))).length;
  card.querySelector('.atlas-panel-heading > .muted').textContent = `${new Set(panes.map(p => p.session)).size} sessions · ${panes.length} panes · ${running} running`;
}
