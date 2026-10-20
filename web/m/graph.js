// The Fleet graph (#view=graph, docs/design/fleet-graph.md): every pane a node on a canvas
// you pan, pinch and zoom, its running sub-agents as satellites, grouped by a lens (Session,
// Account, Repo / dir, Worktree, Tool / model) whose hubs fold. Switching lens glides each
// node to its new place; a poll that changes nothing structural only recolours, and one that
// adds a pane eases it in from its hub, so live updates never jump the layout.
import { STATES, stateOf } from './atlas.js';
import { esc, paneName, paneHeadline, liveSubagents, subagentCount, since, home } from './pane-model.js';

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
const phone = () => matchMedia('(max-width: 699px)').matches;
const busy = s => s === 1 || s === 4 || s === 5;
const el = (tag, cls = '', html = '') => Object.assign(document.createElement(tag), { className: cls, innerHTML: html });
const agent = p => p.tool && p.tool !== 'shell';
const subsOf = p => agent(p) ? Math.min(MAX_SUBS, subagentCount(p)) : 0;
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
function colors() {
  const cs = getComputedStyle(document.documentElement), v = n => cs.getPropertyValue(n).trim();
  return { s: [0, 1, 2, 3, 4, 5].map(i => v(`--chart-s${i}`)), red: v('--red'), fg: v('--fg'), muted: v('--muted'), line: v('--line'),
    surface: v('--surface'), raised: v('--raised'), amber: v('--amber'), font: getComputedStyle(document.body).fontFamily };
}
// Idle reads as a quiet grey ring, not the chart's near-invisible idle fill.
const ring = (C, s) => s === 2 || s === 3 ? C.muted : C.s[s];

// The graph on `box`. `mini` is the Dashboard card's: no labels but the hubs', no input.
// ctx: { icon, logos, open(paneId), owner(paneId) -> account profile, onChange() }.
export function fleetGraph(box, mini, ctx) {
  const cv = document.createElement('canvas'), dc = cv.getContext('2d');
  box.append(cv);
  let W = 0, H = 0, C = colors(), theme = '', hover = null, sel = null, raf = 0, lens = 'session', model = null, anim = null, panes = [], pop = null, flowed = false;
  let listNodes = () => {}, touched = false; // touched: panned or zoomed since the last fit // the full view's node buttons (below)
  const view = { k: 1, x: 0, y: 0 }, collapsed = new Set(), imgs = {}, cache = new Map();
  // Nodes keep their identity across lenses and polls, so a pane glides to its new place.
  const node = (id, init) => { let n = cache.get(id); if (!n) cache.set(id, n = { id, x: NaN, y: NaN, ...init() }); return n; };
  const paneNode = p => Object.assign(node(p.pane_id, () => ({ kind: 'pane', r: 11 })), { p });
  const subNodes = pn => Array.from({ length: subsOf(pn.p) }, (_, j) => Object.assign(node(`${pn.id}/${j}`, () => ({ kind: 'sub', r: 4.5 })), { parent: pn, a: liveSubagents(pn.p)[j] }));
  const stateOf_ = n => n.kind === 'pane' ? stateOf(n.p) : n.kind === 'sub' ? 1 : null;
  const logo = tool => { const src = ctx.logos[tool]; if (src && !imgs[tool]) { const i = imgs[tool] = new Image(); i.onload = () => draw(); i.src = src; } return imgs[tool]; };

  // A lens is a tree: the root, a hub per path segment, then the panes and their satellites.
  function build() {
    const root = node('root', () => ({ kind: 'root', r: 24 })), nodes = [root], edges = [], groups = new Map();
    root.children = [];
    for (const p of panes) {
      let up = root, key = `g:${lens}`;
      PATHS[lens](p, ctx.owner).filter(Boolean).forEach((part, d) => {
        const [id, seg] = Array.isArray(part) ? part : [part, part];
        key += `/${encodeURIComponent(id)}`; // a key can hold slashes; segments must not merge
        let g = groups.get(key);
        if (!g) { g = Object.assign(node(key, () => ({ kind: 'group', depth: d + 1, r: d ? 11 : 16 })), { label: seg, children: [], panes: [], up }); groups.set(key, g); up.children.push(g); nodes.push(g); }
        g.panes.push(p); up = g;
      });
      const pn = paneNode(p);
      pn.up = up; pn.children = []; up.children.push(pn); nodes.push(pn);
      subNodes(pn).forEach(s => { s.up = pn; nodes.push(s); });
    }
    // Worktree lens: a checkout with two agents busy in it is where edits collide.
    for (const g of groups.values()) g.warn = lens === 'worktree' && g.depth === 2 && g.panes.filter(p => p.worktree && agent(p) && busy(stateOf(p))).length > 1; // a real checkout, not a folder
    const hidden = n => { for (let u = n.up; u; u = u.up) if (collapsed.has(u.id)) return true; return false; };
    const vis = nodes.filter(n => !hidden(n));
    vis.forEach(n => { if (n.up) edges.push({ a: n.up, b: n }); });
    const live = new Set(nodes.map(n => n.id));
    for (const id of cache.keys()) if (!live.has(id)) cache.delete(id); // gone panes, other lenses' hubs
    return { root, nodes: vis, edges, sig: `${lens}|${vis.map(n => `${n.id}<${n.up?.id}`).join()}` }; // a move between hubs is a change
  }
  // Leaves (panes, folded hubs) share the circle by slot, with a gap between groups.
  function layout({ root }) {
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
    const ax = Math.max(1, Math.min(mini ? 2.6 : 1.35, W / H || 1)), ay = Math.max(1, Math.min(1.5, H / W || 1));
    const place = (n, d) => {
      const a = ang(n.slot), r = n.kind === 'root' ? 0 : n.kind === 'pane' ? RL : RL * d / (D + 0.6);
      Object.assign(n, { tx: r * Math.cos(a) * ax, ty: r * Math.sin(a) * ay, out: a });
      if (n.kind === 'pane') subNodes(n).forEach((x, m, all) => { const c = a + (m - (all.length - 1) / 2) * 0.5; Object.assign(x, { tx: n.tx + 30 * Math.cos(c), ty: n.ty + 30 * Math.sin(c), out: c }); });
      if (!collapsed.has(n.id)) (n.children || []).forEach(c => place(c, d + 1));
    };
    place(root, 0);
    listNodes();
  }
  const rad = n => Math.max(n.r, (n.kind === 'group' ? 7 : n.kind === 'root' ? 9 : n.kind === 'sub' ? 2.5 : 4.5) / view.k);
  function near(n) { // what a hovered or selected node lights: itself, its edges, a hub's subtree
    if (!n || n.kind === 'root') return null;
    const s = new Set([n.id]), down = x => (x.children || []).forEach(c => { s.add(c.id); down(c); });
    model.edges.forEach(e => { if (e.a === n) s.add(e.b.id); if (e.b === n) s.add(e.a.id); });
    if (n.kind === 'group') down(n);
    if (n.kind === 'pane') subNodes(n).forEach(x => s.add(x.id));
    return s;
  }

  function draw(now = performance.now()) {
    if (!model || !W) return;
    if (theme !== document.documentElement.className) { theme = document.documentElement.className; C = colors(); }
    if (anim) { // a lens change, a fold or a new pane: everything glides from where it was
      const t = Math.max(0, Math.min(1, (now - anim.t0) / anim.dur)), e = t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2;
      model.nodes.forEach(n => { n.x = n.fx + (n.tx - n.fx) * e; n.y = n.fy + (n.ty - n.fy) * e; n.alpha = n.fresh ? e : 1; });
      Object.assign(view, { k: anim.v0.k + (anim.v1.k - anim.v0.k) * e, x: anim.v0.x + (anim.v1.x - anim.v0.x) * e, y: anim.v0.y + (anim.v1.y - anim.v0.y) * e });
      if (t >= 1) { anim = null; model.nodes.forEach(n => { n.fresh = false; n.alpha = 1; }); }
    }
    const dpr = devicePixelRatio || 1;
    dc.setTransform(dpr, 0, 0, dpr, 0, 0);
    dc.clearRect(0, 0, W, H);
    dc.setTransform(dpr * view.k, 0, 0, dpr * view.k, dpr * view.x, dpr * view.y);
    const k = view.k, lit = near(hover || sel), dim = (...ids) => lit && !ids.every(i => lit.has(i)) ? 0.15 : 1, moving = motion();
    const label = (text, x, y, align, color, size = 11, weight = 400, base = 'middle') => {
      dc.font = `${weight} ${size / k}px ${C.font}`; dc.textAlign = align; dc.textBaseline = base;
      dc.lineWidth = 3 / k; dc.strokeStyle = C.surface; dc.strokeText(text, x, y); dc.fillStyle = color; dc.fillText(text, x, y);
    };
    dc.lineCap = 'round';
    flowed = false;
    for (const { a, b } of model.edges) {
      const s = stateOf_(b);
      dc.globalAlpha = Math.min(a.alpha ?? 1, b.alpha ?? 1) * dim(a.id, b.id) * (s === 2 || s === 3 ? 0.45 : 0.85);
      dc.strokeStyle = b.kind === 'group' ? C.line : ring(C, s); dc.lineWidth = 1.1 / k;
      dc.beginPath(); dc.moveTo(a.x, a.y); dc.lineTo(b.x, b.y); dc.stroke();
      // Particles flow outward along every edge that leads to work in flight.
      if (!moving || !(b.kind === 'group' ? b.panes.some(p => busy(stateOf(p))) : busy(s))) continue;
      flowed = true;
      const seed = (a.id.length * 7 + b.id.charCodeAt(b.id.length - 1)) % 10 / 10;
      dc.fillStyle = C.s[1];
      for (let j = 0; j < 2; j++) {
        const t = (now / (b.kind === 'group' ? 2600 : 1700) + seed + j / 2) % 1;
        dc.beginPath(); dc.arc(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, (mini ? 1.6 : 2.2) / k, 0, 7); dc.fill();
      }
    }
    for (const n of model.nodes) {
      const r = rad(n), s = stateOf_(n), ux = Math.cos(n.out || 0), uy = Math.sin(n.out || 0);
      dc.globalAlpha = dim(n.id) * (n.alpha ?? 1);
      if (n.kind === 'root') {
        dc.fillStyle = C.raised; dc.beginPath(); dc.arc(n.x, n.y, r, 0, 7); dc.fill();
        dc.strokeStyle = C.line; dc.lineWidth = 1.5 / k; dc.stroke();
        if (k > 0.3) { label(lens === 'session' ? 'tmux' : LENSES.find(l => l[0] === lens)[1], n.x, n.y - 5 / k, 'center', C.fg, 11, 700); label(`${panes.length} panes`, n.x, n.y + 8 / k, 'center', C.muted, 9); }
      } else if (n.kind === 'group') {
        // A hub is a donut of its panes' states; folded, it grows and says how many it holds.
        const big = collapsed.has(n.id) ? r * 1.35 : r, counts = [0, 0, 0, 0, 0, 0];
        n.panes.forEach(p => counts[stateOf(p)]++);
        if (n.warn) { dc.strokeStyle = C.amber; dc.lineWidth = 2 / k; dc.setLineDash([4 / k, 3 / k]); dc.beginPath(); dc.arc(n.x, n.y, big + 6 / k, 0, 7); dc.stroke(); dc.setLineDash([]); }
        dc.fillStyle = C.surface; dc.beginPath(); dc.arc(n.x, n.y, big, 0, 7); dc.fill();
        let a0 = -Math.PI / 2;
        dc.lineWidth = Math.max(3.5, big * 0.32 * k) / k;
        for (const i of [1, 0, 4, 5, 2, 3]) if (counts[i]) {
          const a1 = a0 + counts[i] / n.panes.length * Math.PI * 2;
          dc.strokeStyle = i === 2 || i === 3 ? C.line : C.s[i]; dc.beginPath(); dc.arc(n.x, n.y, Math.max(0.1, big - dc.lineWidth / 2), a0, a1); dc.stroke(); a0 = a1;
        }
        if (collapsed.has(n.id)) label(String(n.panes.length), n.x, n.y, 'center', C.fg, 11, 700);
        if (!mini || n.depth === 1) label(n.label + (n.warn ? ' · 2 agents busy' : ''), n.x, n.y + big + (n.warn ? 8 : 4) / k, 'center', n.warn ? C.amber : n.depth === 1 ? C.fg : C.muted, n.depth === 1 ? (mini ? 11 : 12) : 11, n.depth === 1 ? 600 : 400, 'top');
      } else if (n.kind === 'pane') {
        if (s === 0) { dc.fillStyle = C.amber; dc.globalAlpha *= 0.25; dc.beginPath(); dc.arc(n.x, n.y, r + 5 / k, 0, 7); dc.fill(); dc.globalAlpha = dim(n.id) * (n.alpha ?? 1); }
        dc.fillStyle = C.surface; dc.beginPath(); dc.arc(n.x, n.y, r, 0, 7); dc.fill();
        const img = logo(n.p.tool);
        if (img?.complete && img.naturalWidth && r * k > 5) {
          dc.save(); dc.globalAlpha *= s === 2 ? 0.55 : 1; dc.beginPath(); dc.arc(n.x, n.y, r * 0.78, 0, 7); dc.clip();
          dc.drawImage(img, n.x - r * 0.78, n.y - r * 0.78, r * 1.56, r * 1.56); dc.restore();
        } else { dc.fillStyle = ring(C, s); dc.beginPath(); dc.arc(n.x, n.y, r * 0.6, 0, 7); dc.fill(); }
        dc.strokeStyle = ring(C, s); dc.lineWidth = (s === 2 ? 1.2 : 2.4) / k; dc.beginPath(); dc.arc(n.x, n.y, r, 0, 7); dc.stroke();
        if (n === sel) { dc.strokeStyle = C.fg; dc.lineWidth = 2 / k; dc.beginPath(); dc.arc(n.x, n.y, r + 7 / k, 0, 7); dc.stroke(); }
        if (!mini && (n === hover || n === sel || k > 1.05 || (k > 0.5 && s !== 2))) label(paneName(n.p), n.x + ux * (r + 6 / k), n.y + uy * (r + 6 / k), Math.abs(ux) < 0.3 ? 'center' : ux > 0 ? 'left' : 'right', s === 2 ? C.muted : C.fg);
      } else {
        dc.fillStyle = C.s[1]; dc.beginPath(); dc.arc(n.x, n.y, r, 0, 7); dc.fill();
        if ((n === hover || n === sel) && n.a?.label) label(String(n.a.label), n.x + 8 / k, n.y, 'left', C.muted, 10);
      }
    }
    dc.globalAlpha = 1;
  }
  // Frames run while something moves and the canvas is on screen; a hidden page costs nothing.
  let seen = false; // on screen: a hidden page or a card scrolled away costs nothing
  new IntersectionObserver(([e]) => { seen = e.isIntersecting; if (seen) animate(); }).observe(cv);
  const loop = t => { raf = 0; if (!seen) return; draw(t); if (anim || flowed) raf = requestAnimationFrame(loop); };
  const animate = () => { if (!raf) raf = requestAnimationFrame(loop); };
  matchMedia('(prefers-reduced-motion: reduce)').addEventListener?.('change', () => { draw(); animate(); });
  new MutationObserver(() => draw()).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] }); // the theme: draw() rereads the palette

  function fitView() {
    const xs = model.nodes.map(n => n.tx), ys = model.nodes.map(n => n.ty), pad = mini ? 52 : 60, top = mini || phone() ? 10 : 46;
    const x0 = Math.min(...xs) - pad, x1 = Math.max(...xs) + pad, y0 = Math.min(...ys) - pad, y1 = Math.max(...ys) + pad;
    const k = Math.max(K_MIN, Math.min(3, W / (x1 - x0), (H - top) / (y1 - y0)));
    return { k, x: W / 2 - (x0 + x1) / 2 * k, y: top + (H - top) / 2 - (y0 + y1) / 2 * k };
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
    const b = box.getBoundingClientRect(), dpr = devicePixelRatio || 1;
    if (b.width < 20 || b.height < 60) return;
    const first = !W;
    if (b.width === W && b.height === H) { draw(); return animate(); } // reattached as it was
    const dx = (b.width - W) / 2, dy = (b.height - H) / 2;
    W = b.width; H = b.height; cv.width = W * dpr; cv.height = H * dpr;
    if (first) return relayout(true);
    land(); // a glide's endpoints were for the old size
    // A view the user framed keeps its pan and zoom, recentred; an untouched one refits.
    layout(model); model.nodes.forEach(n => { n.x = n.tx; n.y = n.ty; });
    Object.assign(view, touched ? { x: view.x + dx, y: view.y + dy } : fitView()); draw(); animate();
  }
  new ResizeObserver(size).observe(box);
  const close = () => { pop?.remove(); pop = null; sel = null; draw(); };
  const api = {
    update(list) {
      panes = list; relayout(false);
      // A node gone takes its hover, selection and popover with it; an open popover follows the
      // live data, in place, focus kept.
      if (hover && !model.nodes.includes(hover)) { hover = null; draw(); }
      if (sel && !model.nodes.includes(sel)) return close();
      if (!pop) return;
      const focused = pop.contains(document.activeElement) && document.activeElement.className;
      tap(pop._at, sel);
      if (focused) pop.querySelector(`.${focused.split(' ')[0]}`)?.focus();
    },
    fit() { touched = false; anim = { t0: performance.now(), dur: motion() ? 400 : 1, v0: { ...view }, v1: fitView() }; model.nodes.forEach(n => { n.fx = n.x; n.fy = n.y; }); animate(); },
    setLens(l) { lens = l; close(); relayout(true); ctx.onChange?.(); }, lens: () => lens,
    zoom(f) { zoomAt(W / 2, H / 2, f); },
  };
  if (mini) return api;

  // Every node as a button too, for the keyboard and screen readers: focus selects it on the
  // canvas, Enter does what a tap does (a pane's popover, a hub's fold).
  const buttons = el('div', 'fg-nodes');
  box.append(buttons);
  const say = n => n.kind === 'group' ? `${n.label}: ${n.panes.length} panes, ${collapsed.has(n.id) ? 'folded' : 'unfolded'}`
    : n.kind === 'pane' ? `${paneName(n.p)}: ${STATES[stateOf(n.p)]}` : `Sub-agent of ${paneName(n.parent.p)}`;
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
    pts.set(e.pointerId, at);
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
  cv.addEventListener('wheel', e => { e.preventDefault(); const at = pos(e); zoomAt(at.x, at.y, Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015))); }, { passive: false });
  // A drag on the graph pans it: not the app's edge swipe back (app.js), which listens on window.
  for (const t of ['touchstart', 'touchend']) box.addEventListener(t, e => e.stopPropagation());

  function popover(html, at, n) {
    if (pop?._html === html && sel === n) return; // a poll that changed nothing in it
    pop?.remove();
    pop = Object.assign(el('div', `fg-pop${phone() ? ' sheet' : ''}`, html), { _at: at, _html: html });
    box.append(pop);
    if (!phone()) { pop.style.left = `${Math.min(W - 292, Math.max(8, at.x + 18))}px`; pop.style.top = `${Math.min(H - pop.offsetHeight - 8, Math.max(8, at.y - 30))}px`; }
    pop.querySelector('.fg-x').onclick = () => { // the keyboard goes back to the node it came from
      const back = pop.contains(document.activeElement) && [...buttons.children].find(b => b._n === sel);
      close(); back?.focus();
    };
    pop.querySelector('[data-open]').onclick = () => ctx.open(n.kind === 'sub' ? n.parent.id : n.id);
    sel = n; draw();
  }
  const head = (lead, title) => `<div class="fg-pop-head">${lead}<b>${esc(title)}</b><button class="fg-x" aria-label="Close">${ctx.icon('x', 16)}</button></div>`;
  const stLine = (s, extra) => `<span class="fg-st"><i style="background:${ring(C, s)}"></i>${STATES[s]}${extra ? ` · ${esc(extra)}` : ''}</span>`;
  const dl = rows => `<dl>${rows.filter(([, v]) => v).map(([t, v]) => `<dt>${t}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
  const openBtn = '<button class="fg-btn" data-open>Open pane</button>';
  function tap(at, n = hit(at.x, at.y)) {
    if (!n) return close();
    if (n.kind === 'root') { close(); return api.fit(); }
    if (n.kind === 'group') { collapsed.has(n.id) ? collapsed.delete(n.id) : collapsed.add(n.id); close(); return relayout(false); }
    if (n.kind === 'sub') {
      const a = n.a || {};
      return popover(`${head(`<i class="fg-dot" style="background:${C.s[1]}"></i>`, a.label || 'Sub-agent')}${stLine(1, 'sub-agent')}
        ${dl([['Pane', paneName(n.parent.p)], ['Tokens', a.tokens], ['Time', a.elapsed]])}${openBtn}`, at, n);
    }
    const p = n.p, s = stateOf(p), w = p.worktree, subs = subagentCount(p), src = ctx.logos[p.tool];
    popover(`${head(src ? `<img class="fg-logo" src="${esc(src)}" alt="">` : '', paneName(p))}${stLine(s, p.session)}
      ${dl([['Model', p.model], ['Account', ctx.owner(p.pane_id)?.name], ['Tokens', p.working?.tokens], ['Agents', subs ? `${subs} running` : ''],
        ['Now', paneHeadline(p)], ['Time', busy(s) ? p.working?.elapsed || (since(p) && `for ${since(p)}`) : since(p) && `idle ${since(p)}`],
        ['Where', `${home(p.cwd || '')}${w?.branch ? ` @ ${w.branch}` : ''}`]])}${openBtn}`, at, n);
  }
  return api;
}

// The view: Group by, the counts, and the graph filling the rest. Built once and kept, so a
// visit back finds the lens, folds and places it left.
export function renderGraphView(root, panes, ctx) {
  if (!root._g) {
    const lenses = el('div', 'fg-lenses', '<span>Group by</span>'), box = el('div', 'fg-box'), tools = el('div', 'fg-tools');
    root._stats = el('div', 'fg-stats'); root._legend = el('div', 'fg-legend');
    for (const [name, label] of LENSES) { const b = el('button', '', label); b.dataset.lens = name; b.onclick = () => g.setLens(name); lenses.append(b); }
    const g = root._g = fleetGraph(box, false, { ...ctx, onChange: () => { try { localStorage.setItem('tmuxrc-graph-lens', g.lens()); } catch {} paint(); } });
    for (const [name, label, fn] of [['plus', 'Zoom in', () => g.zoom(1.35)], ['minus', 'Zoom out', () => g.zoom(1 / 1.35)], ['fit', 'Fit to view', () => g.fit()]]) {
      const b = el('button', '', ctx.icon(name, 18)); b.title = b.ariaLabel = label; b.onclick = fn; tools.append(b);
    }
    box.append(tools, root._legend);
    root.append(lenses, root._stats, box);
    const paint = root._paint = () => {
      const lens = g.lens(), list = root._panes, counts = [0, 0, 0, 0, 0, 0], subs = list.reduce((t, p) => t + (agent(p) ? subagentCount(p) : 0), 0); // all of them, where the canvas draws MAX_SUBS
      list.forEach(p => counts[stateOf(p)]++);
      root._stats.innerHTML = `<span><b>${new Set(list.map(p => p.session)).size}</b> sessions · <b>${list.length}</b> panes${subs ? ` · <b>${subs}</b> sub-agents` : ''}</span>`
        + [1, 0, 4, 5].filter(i => counts[i]).map(i => `<span class="fg-st"><i style="background:var(--chart-s${i})"></i><b>${counts[i]}</b> ${STATES[i].toLowerCase()}</span>`).join('');
      root._legend.innerHTML = `<p>${LEGEND[lens]}</p><div class="fg-key">${[1, 0, 2, 4, 5].map(i => `<span><i style="background:${i === 2 ? 'var(--muted)' : `var(--chart-s${i})`}"></i>${STATES[i]}</span>`).join('')}${lens === 'worktree' ? '<span><i class="fg-warn-key"></i>two agents, one checkout</span>' : ''}</div>`;
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
  g.update(panes);
  root._paint();
  return root;
}

// The Dashboard's card: the session lens at card size, live, every tap opening the view.
export function renderGraphCard(card, panes, ctx) {
  const box = card._box ||= card.appendChild(el('div', 'fg-mini'));
  (card._g ||= fleetGraph(box, true, ctx)).update(panes);
  const running = panes.filter(p => busy(stateOf(p))).length;
  card.querySelector('.atlas-panel-heading > .muted').textContent = `${new Set(panes.map(p => p.session)).size} sessions · ${panes.length} panes · ${running} running`;
}
