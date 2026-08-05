// tmux-rc PWA. Polls /api/state, renders ONE pane card at a time (the dock — icon
// tabs, tally filters — and card swipes switch panes), and posts answers back.
// No framework, no build step (native ES module — index.html loads type=module).
//
// ════════════════════════════ THE RENDER INVARIANT ════════════════════════════
// A node, once built, is NEVER replaced. Handlers close over `pane_id` (a string),
// never over a state object, and read current state via `panesById[paneId]` at call
// time. render() may only write text/attrs/classes onto existing nodes, or
// insert/remove whole pane-level nodes when deck membership changes.
//
// WHY (this is not a performance rule — it is a correctness rule):
//   • The long-poll returns the instant anything changes, so a busy pane re-renders
//     constantly. The browser only fires `click` when press AND release land on the
//     SAME element, so replacing a node between finger-down and finger-up silently
//     SWALLOWS the tap. Building once means handlers, focus, caret, scroll position,
//     selection and in-flight gestures survive BY CONSTRUCTION, not by workaround.
//   • Assigning an identical `textContent` still destroys and recreates the text node,
//     which collapses any document Selection inside it — which is why the setText/
//     setAttr/setCls no-ops below are SEMANTIC, not an optimization.
//
// #bar (the composer) has always been built this way — static HTML, wired once — which
// is exactly why typed text survives polls. tickBadges() is the other model: it reads
// data-since off live nodes and writes textContent. This module follows both.
//
// THE SANCTIONED EXCEPTIONS, none of them on the poll path:
//   1. ONE-TIME skeleton construction. panesUI() and the build* half of each build/apply
//      pair may replaceChildren/append freely; they are memoized and run once per node's
//      lifetime, not per render.
//   2. The TERMINAL paint (bgTerm's peek and openScreen's overlay) still swaps innerHTML
//      wholesale, because renderCapture emits a whole colored frame. It is guarded by a
//      selection check and _caretGrace instead; a TODO in bgTerm points at line-diffing it,
//      which is what would let those guards go. This is the last rendering workaround left.
//   3. One-shot innerHTML for STATIC inline SVG/icon markup (the ⤢ button, Lucide icons,
//      the fullscreen overlay's chrome). Written once at build time from string literals,
//      never per poll, and never from server data.
// Anything else assigning innerHTML or replaceChildren from an apply*/render path is a bug.
// ══════════════════════════════════════════════════════════════════════════════
import { renderCapture, linkifyText } from "./terminal.js";

// ── In-place write primitives ────────────────────────────────────────────────
// Each no-ops when the value is already current. The no-op is the POINT (see the invariant
// above); a redundant attribute write also restarts a CSS animation or transition.
function setText(node, s) {
  if (!node) return;
  const v = s == null ? "" : String(s);
  if (node.textContent !== v) node.textContent = v;
}
function setAttr(node, k, v) {
  if (!node) return;
  if (v == null || v === false) { if (node.hasAttribute(k)) node.removeAttribute(k); return; }
  const s = String(v);
  if (node.getAttribute(k) !== s) node.setAttribute(k, s);
}
function setCls(node, name, on) {
  if (!node) return;
  if (node.classList.contains(name) !== !!on) node.classList.toggle(name, !!on);
}
// setHtml is the ONE sanctioned in-place innerHTML write, for the three surfaces whose
// content is linkified prose (headline, session summary, event text): linkifyText returns
// MARKUP, so textContent would show the tags. It is safe only because linkifyText escapes
// everything it does not itself turn into an anchor — never hand this raw server text.
//
// The unchanged-guard is as load-bearing here as in setText, and more so: assigning an
// identical innerHTML tears down and rebuilds the whole subtree, which collapses any
// Selection inside it — the user copying a URL out of the summary would lose it on every
// poll. Comparing against the live innerHTML is exact for our own serialized output.
function setHtml(node, html) {
  if (!node) return;
  const v = html == null ? "" : String(html);
  if (node.innerHTML !== v) node.innerHTML = v;
}

// ── keyedList: the one reconciler ────────────────────────────────────────────
// `build(item, key)` makes a node the FIRST time a key is seen; `apply(node, item, i)`
// updates it every time. Nodes are cached on the parent, which is what makes handlers
// wired inside build() permanent.
//
// SERVER ARRAY ORDER IS THE ORDER. tmux's own session/window/pane order is load-bearing
// (it drives the dock, the list and swipe navigation), so a single forward pass is exact —
// no LIS, no move-minimization heuristics. insertBefore is called ONLY when a node is not
// already in position, so a stable list performs zero DOM moves.
function keyedList(parentEl, items, keyFn, build, apply) {
  let cache = parentEl._klCache;
  if (!cache) cache = parentEl._klCache = new Map();
  const next = new Map();
  let cursor = parentEl.firstChild;
  items.forEach((item, i) => {
    const key = String(keyFn(item, i));
    let node = cache.get(key);
    if (!node) node = build(item, key);
    if (!node) return;
    next.set(key, node);
    if (apply) apply(node, item, i);
    // Already in the right slot? Then advance past it and touch nothing.
    if (cursor === node) { cursor = node.nextSibling; return; }
    parentEl.insertBefore(node, cursor);
  });
  // Remove leftovers. Actually REMOVE them (never hide): CSS structural selectors
  // (#top .dock:empty, #filters:empty, .metarow:empty, .lm-convo:empty) depend on an
  // emptied list having no children at all.
  for (const [key, node] of cache) if (!next.has(key)) node.remove();
  parentEl._klCache = next;
  return next;
}

// Real brand marks per tool (served from web/). One img template so every icon renders
// identically; unidentified panes fall back to the tmux logomark. `tool` comes from
// parser JSON, so look it up with hasOwnProperty (a value like "toString"/"constructor"
// would otherwise resolve up the prototype chain and render garbage) and escape its alt.
const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const LOGOS = { claude: "/claude.png", codex: "/openai.svg", gemini: "/gemini.svg",
  shell: "/bash.png" }; // official Bash logo (MIT — see bash-logo.LICENSE)
// Unidentified panes get the tmux logomark ("some tmux pane") instead of a bare dot.
const UNKNOWN_LOGO = "/tmux-logomark.svg";
// Tools with status/input chrome at the bottom of their screen (see bgTerm).
const AGENT_TOOLS = new Set(["claude", "codex", "gemini"]);
// activity comes from parser (LLM) output and gets interpolated into class names —
// whitelist it so an unexpected value can't inject markup/classes.
const ACTIVITIES = new Set(["running", "waiting", "idle", "compacting", "unknown"]);
// `waiting` means "blocked on input"; `waiting_on` says on WHOM. Only a user-wait is
// actionable ("tap me, I need input") — an external-wait (a background subagent, a
// Copilot/CI/poll it spawned) is just busy on a machine, so we FOLD it into `running`
// here. That single chokepoint keeps the amber `waiting` badge/dot/tally/filter/favicon
// honest — they all read `actOf` — without forking a parallel render path. Absent or
// any non-"external" value ⇒ user-wait (the safe default: never hide a real user-wait).
const actOf = (s) => {
  const a = ACTIVITIES.has(s.activity) ? s.activity : "unknown";
  return a === "waiting" && s.waiting_on === "external" ? "running" : a;
};
// The pane icon's src/alt. Both dock icons and list rows build a real <img> once and
// setAttr these onto it, so the tool name never reaches markup and needs no escaping.
const logoFor = (tool) => (has(LOGOS, tool) ? LOGOS[tool] : UNKNOWN_LOGO);

// Lucide icons (ISC), inlined: stroke follows currentColor so they theme for free —
// the emoji they replace rendered as platform-colored glyphs that clashed with the
// chrome (and differed per device). Same inline-SVG approach as the ⤢ fsbtn.
const LUCIDE = {
  mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/>',
  keyboard: '<rect width="20" height="12" x="2" y="6" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h.01M18 14h.01M9 14h6"/>',
  paperclip: '<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  clipboard: '<rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
};
const licon = (name, size = 16) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor"` +
  ` stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${LUCIDE[name]}</svg>`;

const panesEl = document.getElementById("panes");
const liveEl = document.getElementById("live");

// Tab title carries the host, so tabs to different tmux-rc daemons are tellable apart.
document.title = `tmuxʳᶜ - ${location.host}`;

// GitHub-style tab badge: while any pane is WAITING, the favicon gets an amber dot
// (drawn once onto a canvas over the base icon, cached as a data URL) — so a
// background tab still shows "something needs you".
const favLink = document.querySelector('link[rel="icon"]');
const favBase = favLink && favLink.href;
let favDotUrl = null;
let favWaiting = false;
function setFavicon(waiting) {
  if (!favLink || !favBase || waiting === favWaiting) return;
  favWaiting = waiting;
  if (!waiting) { favLink.href = favBase; return; }
  if (favDotUrl) { favLink.href = favDotUrl; return; }
  const im = new Image();
  // Any failure resets favWaiting so a later render RETRIES the badge — otherwise the
  // waiting===favWaiting short-circuit wedges the tab dotless for the whole wait.
  im.onerror = () => { favWaiting = false; };
  im.onload = () => {
    const c = document.createElement("canvas");
    c.width = c.height = 32;
    const g = c.getContext("2d");
    if (!g) { favWaiting = false; return; } // context can be null (memory pressure)
    g.drawImage(im, 0, 0, 32, 32);
    // Punch a clear ring first so the dot reads over busy icon pixels. Sized like
    // the in-app corner badges — a hint, not an eclipse.
    g.globalCompositeOperation = "destination-out";
    g.beginPath(); g.arc(26, 6, 5.5, 0, Math.PI * 2); g.fill();
    g.globalCompositeOperation = "source-over";
    g.beginPath(); g.arc(26, 6, 4, 0, Math.PI * 2);
    g.fillStyle = "#e3b341"; g.fill();
    favDotUrl = c.toDataURL("image/png");
    if (favWaiting) favLink.href = favDotUrl;
  };
  im.src = favBase;
}

// Theme. The inline boot script in index.html applied html.light before first paint;
// here we own the header toggle, the "tmuxrc-theme" override (absent = follow the OS)
// and the theme-color meta, so the PWA status bar tracks the page background.
const themeBtn = document.getElementById("theme-btn");
const prefersLight = matchMedia("(prefers-color-scheme: light)");
function applyTheme(light) {
  document.documentElement.classList.toggle("light", light);
  if (themeBtn) { // a missing button must not abort the module (theme still applies)
    themeBtn.setAttribute("aria-pressed", String(light));
    // Lucide sun/moon (shows the mode a tap switches TO) — SVGs render identically
    // everywhere, unlike the emoji glyphs these replaced.
    themeBtn.innerHTML = licon(light ? "moon" : "sun", 14);
    themeBtn.title = light ? "Switch to dark mode" : "Switch to light mode";
    // aria-label stays "Light mode" on purpose: an aria-pressed toggle keeps a FIXED
    // accessible name (ARIA authoring practices) — pressed state carries the rest.
  }
  document.querySelector('meta[name="theme-color"]').content =
    getComputedStyle(document.body).backgroundColor;
}
applyTheme(document.documentElement.classList.contains("light"));
if (themeBtn) themeBtn.onclick = () => {
  const light = !document.documentElement.classList.contains("light");
  // Toggling INTO the OS's current preference clears the override — back to auto,
  // so the app resumes following the OS (e.g. sunset auto-dark) instead of pinning.
  try {
    if (light === prefersLight.matches) localStorage.removeItem("tmuxrc-theme");
    else localStorage.setItem("tmuxrc-theme", light ? "light" : "dark");
  } catch {}
  applyTheme(light);
};
const onSchemeChange = (e) => {
  let stored = null;
  try { stored = localStorage.getItem("tmuxrc-theme"); } catch {}
  // Same normalization as the boot script: junk/legacy values are NOT an override.
  if (stored !== "light" && stored !== "dark") applyTheme(e.matches); // live-track the OS
};
// Older iOS Safari only has the deprecated addListener on MediaQueryList — and
// phones are exactly where this app runs.
if (prefersLight.addEventListener) prefersLight.addEventListener("change", onSchemeChange);
else if (prefersLight.addListener) prefersLight.addListener(onSchemeChange);

// Collapse is a VIEW-WIDE preference, not per-pane: collapse one card (caret ▸) and
// every pane — including ones you swipe to — shows its one-line header, handing the
// screen to the live terminal. Expanding anywhere expands them all.
let cardsCollapsed = false;
// NO render-freeze flag here, deliberately. `busy` / `busySend` / `busyGesture` and their
// 10s watchdog existed because a poll render REPLACED the DOM under a finger or mid-send.
// Under the render invariant a render only rewrites text/attrs on the very nodes a gesture
// is already translating, so there is nothing to freeze — and nothing that a missed release
// can leak into a wedged app, which is what the watchdog was rescuing. The two things the
// freeze also did are now local: swipeNav snapshots the id list at touchstart (so a
// mid-swipe reorder cannot change which pane it commits to) and pinchZoom exposes
// panning() for the peek's scroll-to-tail. `sending` — a real in-flight POST guard — stays.

// A fetch deadline that works on older iOS Safari too. AbortSignal.timeout only landed in
// Safari 16, and this file otherwise accounts for older iOS (the MediaQueryList and :has()
// notes below), so calling it unguarded would throw before the request even starts —
// turning a timeout safeguard into a hard failure on exactly the phones this app targets.
// Shared rather than inlined per call site so every bounded fetch expires the same way.
function timeoutSignal(ms) {
  if (typeof AbortSignal !== "undefined" && AbortSignal.timeout) return AbortSignal.timeout(ms);
  // No abort primitives at all (old engines that still have fetch): return undefined so
  // the caller sends an UNBOUNDED request. Losing the deadline is bad; throwing a
  // ReferenceError here would break every send and upload outright, which is worse.
  if (typeof AbortController === "undefined") return undefined;
  const ac = new AbortController();
  // abort() with a reason only if DOMException exists — an engine can have AbortController
  // without it, and throwing inside this timer would leave the request UNaborted plus raise
  // an uncaught async error. A bare abort() still cancels; the reason is only for the
  // catch's message. Third unguarded global in this one helper: fetch existing does not
  // imply the abort family exists.
  setTimeout(() => ac.abort(
    typeof DOMException === "undefined" ? undefined
      : new DOMException("TimeoutError", "TimeoutError")), ms);
  return ac.signal;
}
// Panes awaiting a forced reparse after input: pane_id -> the parsed_at we saw when we
// sent. The card (and its answered question) render in a spinning "reparsing" state
// until the served parsed_at advances past this — so a submitted answer / picked menu
// visibly WORKS instead of sitting stale until the LLM re-reads the screen. Cleared on
// a newer parse or after a timeout (so a failed/silent parse can't spin forever).
const reparsing = {}; // pane_id -> { q, since, ts }
const REPARSE_TIMEOUT = 12000; // stop spinning even if the screen never settles
function markReparsing(id) {
  const s = panesById[id];
  // Remember BOTH the question we just answered (its prompt) and the parsed_at at send.
  // If we answered a question, spin until that question is actually GONE — not merely
  // until a parse lands. The forced reparse often fires before the agent has redrawn,
  // so it re-reports the SAME question and parsed_at ticks; clearing on that tick
  // stopped the spinner with the menu still on screen (the bug). For a plain send with
  // no question, there's nothing to "clear", so fall back to parsed_at advancing.
  reparsing[id] = {
    q: (s && s.question && s.question.prompt) || null,
    since: (s && s.parsed_at) || 0,
    ts: Date.now(),
  };
}
function isReparsing(s) {
  const r = reparsing[s.pane_id];
  if (!r) return false;
  const settled = r.q !== null
    ? ((s.question && s.question.prompt) || null) !== r.q // answered question gone/changed
    : (s.parsed_at || 0) > r.since;                        // no question: a fresh parse landed
  if (settled || Date.now() - r.ts > REPARSE_TIMEOUT) {
    delete reparsing[s.pane_id];
    return false;
  }
  return true;
}

// The web surface is a dumb remote control for tmux — ALL state is in tmux. The active
// pane is whatever tmux reports as focused (state.tmux_active). Tapping a card just
// tells tmux to focus that pane; the next poll renders the new truth. No client-side
// selection state.
let panesById = {}; // latest state per pane, for the bottom bar to act on
// A selection we've told tmux about but the watcher hasn't observed yet. Without this,
// the poll right after a switch still carries the OLD tmux_active and flips the UI
// back for a beat (then forward again) — the "ticky" double-switch. Held until the
// server confirms, with a timeout so a focus change made in tmux itself still wins.
let pending = null; // {id, ts}
// The pane we last showed. NOT a selection (tmux stays the truth) — it anchors which
// SESSION we're viewing. tmux's global "current pane" (tmux_active) flips to whichever
// attached session the user touched last, so with several sessions attached, following
// it yanks the phone between sessions on every desktop keystroke. Instead we follow
// session_active — each session's own focused pane — but only within `shown`'s session;
// focus movement in OTHER sessions is desktop noise, not a signal to switch the view.
let shown = null;
function activeId() {
  if (pending) {
    const s = panesById[pending.id];
    // Only SERVER data may confirm (panesById is never mutated locally): earlier this
    // also checked a locally-set tmux_active flag, which "confirmed" the pending pick
    // instantly — so the next stale poll yanked the selection back to the old pane.
    // Confirm on session_active, not tmux_active: selecting a pane in another session
    // makes it focused in ITS session immediately, but the GLOBAL current pane only
    // moves if a client is attached there — cross-session picks would never confirm.
    // Confirmation must also MOVE the anchor: if the tapped pane was already its
    // session's focused pane, session_active is true in the very next (even stale)
    // poll — clearing pending without re-anchoring would leave `shown` in the old
    // session and the tap would appear to do nothing.
    if (s && s.session_active) { pending = null; return (shown = s.pane_id); }
    // Pane gone / select never landed: drop the anchor too, not just the pending pick —
    // `shown` was optimistically set below while pending, and keeping it would leave
    // the view parked in a session tmux never actually switched to. Null falls through
    // to the global-focus branch: resync to tmux's truth, same as before multi-session.
    if (!s || Date.now() - pending.ts > 8000) { pending = null; shown = null; }
    else return (shown = pending.id);
  }
  const cur = panesById[shown];
  if (cur) {
    // Follow tmux focus within the session we're on (desktop window switches there
    // SHOULD move the phone); stay put if that session's focus is elsewhere-unknown.
    const inSess = Object.values(panesById).find(
      (s) => s.session === cur.session && s.session_active);
    return (shown = (inSess || cur).pane_id);
  }
  // No anchor yet (first load) or our pane vanished: fall back to the global focus.
  const focused = Object.values(panesById).find((s) => s.tmux_active);
  return (shown = focused ? focused.pane_id : Object.keys(panesById)[0] || null);
}
// Taps are plain `click` handlers. Nodes are permanent (see the render invariant), so
// press and release always land on the same element — and `click`, unlike a pointerdown
// commit, still yields to a scroll gesture in the overflow-x strips for free.
// Because a tap is a plain `click`, the four in-scroller controls (collapse caret,
// session-summary toggle, sub-agents chip, question ANSWER OPTIONS) can no longer be
// fired by a scroll that merely STARTED on them: `click` requires press and release on
// the same element and the browser withholds it after a scroll. That is what onTap's
// `defer` mode was hand-rolling, and it comes for free once nodes are permanent.
function setActive(id) {
  // The composer buffer (typed text + staged images) is the user's un-sent message; it
  // persists across pane switches just like the text input does, and sends to whichever
  // pane is active when they hit Send.
  fetch(`/api/panes/${encodeURIComponent(id)}/select`, { method: "POST" }).catch(() => {});
  // pending makes the switch instant in the UI (the next poll is 2s away, and the
  // watcher's view of tmux focus lags a tick or two behind that).
  pending = { id, ts: Date.now() };
  render(Object.values(panesById));
}

// The activity log lives SERVER-SIDE now (/api/panes/{id}/events — bootstrap-seeded
// history + live events), so a page reload doesn't start the feed from zero. Each
// state advertises events_seq, a MONOTONIC append counter; we refetch a pane's log
// when it changes. (Not the log length — the log is capped, so its length freezes
// once full while content still rotates.) Fetches are per-pane, deduped while in flight.
const eventLog = {}; // pane_id -> {seq, events: [{text, file?, meta?, ts, historical?}]}
const evFetching = new Set();


function syncEvents(s) {
  const id = s.pane_id;
  const cached = eventLog[id];
  // seq 0 ⇒ the log is empty: either a fresh pane, or one whose seq the daemon reset
  // (restart / pane-id recycle). Skip the guaranteed-empty fetch, but drop any stale
  // cache first so a reset pane shows an empty feed instead of the pre-reset one.
  if (!s.events_seq) { if (cached) delete eventLog[id]; return; }
  if (evFetching.has(id)) return;
  if (cached && cached.seq === s.events_seq) return;
  evFetching.add(id);
  fetch(`/api/panes/${encodeURIComponent(id)}/events`)
    .then((r) => (r.ok ? r.json() : null))
    .then((events) => {
      if (Array.isArray(events)) {
        eventLog[id] = { seq: s.events_seq, events };
        // Re-render only if this pane's feed is on screen (it's the active card).
        if (id === activeId() && !listFilter) render(Object.values(panesById));
      }
    })
    .catch(() => {})
    .finally(() => evFetching.delete(id));
}

function fmtIdle(s) {
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  return Math.floor(s / 3600) + "h";
}

// Seconds a pane has been in its current state, computed LIVE from state_since (epoch
// secs the daemon stamped when the activity/question last changed). Client-side so it
// keeps climbing even when the pane stops re-parsing — idle_seconds was a frozen
// parse-time snapshot that undercounted a long-quiet pane. Falls back to that snapshot
// only if an older daemon didn't send state_since.
function stateDur(s) {
  // Coerce: state is untyped server/LLM JSON. null must NOT coerce (+null is 0 — a
  // missing timestamp would read as the epoch); junk falls back like absent.
  const t = s.state_since == null ? NaN : +s.state_since;
  return Number.isFinite(t)
    ? Math.max(0, Math.floor(Date.now() / 1000 - t))
    : s.idle_seconds || 0;
}

// The ONE pane-header layout, shared by the expanded card and the list rows so they
// can't drift: [caret?] [icon?] · title + headline (left, ellipsized) · working +
// activity badge (right column, right-justified, stacked). Differences are just flags:
//   caret — the card's collapse ▾/▸ (rows don't collapse);
//   icon  — the row shows the pane icon; the card doesn't (its dock tab IS the icon).
// Returns the innerHTML string; callers wire their own click handlers on the result.
// Non-negative int count of RUNNING sub-agents (classify.py derives s.agents from
// the parser's subagents[] before it reaches the UI). Coerce —
// it's untyped LLM/server JSON headed for innerHTML, so junk must never reach the DOM.
// One definition shared by the dock badge and the list-row icon.
function nsubOf(s) {
  return Number.isFinite(+s.agents) && +s.agents > 0 ? Math.floor(+s.agents) : 0;
}

// The ONE pane-header, as a build/apply pair so the card and the rows share both halves
// and can't drift. buildPaneHeader creates every node the header can ever need;
// applyPaneHeader writes current state onto them.
//
// Nodes that don't apply to a given state are EMPTIED, not removed, so their handlers and
// identity persist; CSS :empty rules hide them (see .ph-sub:empty / .worksub:empty /
// .wnum:empty in index.html).
// `link`: may the headline contain anchors? NO for list rows — row() puts this header
// INSIDE .row-open, a real <button>, and an <a> inside a <button> is invalid HTML:
// browsers disagree about which element a click or Enter activates, so the row's own
// open-the-card tap becomes unreliable and AT announces it inconsistently. The card
// header is not inside a button, so it keeps its links. Recorded on the header handle at
// BUILD time (not passed per apply) because it is a property of where the header lives,
// which never changes for a given node — the invariant's whole point.
function buildPaneHeader(parent, { caret = false, icon = false, link = false } = {}, onCaret) {
  const h = { link };
  if (caret) {
    h.caret = document.createElement("button");
    h.caret.className = "card-caret";
    if (onCaret) h.caret.onclick = onCaret;
    parent.appendChild(h.caret);
  }
  if (icon) {
    h.icon = document.createElement("span");
    h.icon.className = "icon";
    h.iconImg = document.createElement("img");
    h.iconImg.width = h.iconImg.height = 22;
    h.iconImg.style.borderRadius = "5px";
    // aria-hidden: a bare "2" is meaningless to AT (and garbles any computed name). The
    // count is spoken via the sub-toggle chip's text / the dock icons' aria-label.
    h.sacount = document.createElement("sub");
    h.sacount.className = "sacount";
    h.sacount.setAttribute("aria-hidden", "true");
    h.icon.append(h.iconImg, h.sacount);
    parent.appendChild(h.icon);
  }
  const meta = document.createElement("div");
  meta.className = "ph-meta";
  h.name = document.createElement("div");
  h.name.className = "ph-name";
  if (icon) {
    // Rows (icon mode) lead with the tmux window number — the identity the user reads
    // off their own status bar — so the list scans as "the windows of this session".
    h.wnum = document.createElement("span");
    h.wnum.className = "wnum";
    h.name.appendChild(h.wnum);
  }
  h.nameText = document.createElement("span");
  h.name.appendChild(h.nameText);
  h.sub = document.createElement("div");
  h.sub.className = "ph-sub";
  meta.append(h.name, h.sub);
  h.right = document.createElement("div");
  h.right.className = "ph-right";
  h.work = document.createElement("span");
  h.work.className = "worksub";
  h.badge = document.createElement("span");
  h.badge.className = "badge";
  // The pulse dot is permanent; running/compacting show it, other states empty it out.
  h.pulse = document.createElement("span");
  h.pulse.className = "pulse";
  h.badgeText = document.createElement("span");
  h.badge.append(h.pulse, h.badgeText);
  h.right.append(h.work, h.badge);
  parent.append(meta, h.right);
  return h;
}

function applyPaneHeader(h, s, collapsed) {
  const a = actOf(s);
  if (h.caret) {
    setAttr(h.caret, "aria-label", collapsed ? "expand" : "collapse");
    setAttr(h.caret, "aria-expanded", String(!collapsed));
    setText(h.caret, collapsed ? "▸" : "▾");
  }
  if (h.icon) {
    setAttr(h.iconImg, "src", logoFor(s.tool));
    setAttr(h.iconImg, "alt", s.tool || "pane");
    const nsub = nsubOf(s);
    setText(h.sacount, nsub > 0 ? String(nsub) : "");
  }
  if (h.wnum) setText(h.wnum, s.window_index != null ? String(s.window_index) : "");
  setText(h.nameText, s.title || s.label || s.pane_id);
  // linkifyText escapes everything it does not turn into an anchor, so it is a safe
  // drop-in for untrusted model text — but it produces MARKUP, so it needs innerHTML and
  // its own unchanged-guard (assigning identical innerHTML would still rebuild the
  // subtree and collapse a selection inside it, which setText exists to avoid).
  if (h.link) setHtml(h.sub, s.headline ? linkifyText(s.headline) : "");
  else setText(h.sub, s.headline || "");
  // The working sub-line — verb · elapsed · ↓tokens (e.g. "Waiting for review 43s
  // ↓40.4k"). Not gated on activity: a waiting pane still reports what it waits on.
  const w = s.working || {};
  setText(h.work, [w.verb, w.elapsed, w.tokens && "↓" + w.tokens].filter(Boolean).join(" "));
  // idle and waiting both show time-in-state ("idle 4m" / "waiting 4m"); the data-since
  // attr lets tickBadges() advance the text every second in place, so a parked pane's
  // clock stays honest without a re-render (see stateDur).
  const timed = a === "idle" || a === "waiting";
  setAttr(h.badge, "class", "badge b-" + a);
  // Numeric-coerced: a quoted/junk state_since must never reach the attribute.
  const t0 = s.state_since == null ? NaN : +s.state_since;
  setAttr(h.badge, "data-since", timed && Number.isFinite(t0) ? String(t0) : null);
  setCls(h.pulse, "on", a === "running" || a === "compacting");
  setText(h.badgeText, timed ? `${a} ${fmtIdle(stateDur(s))}` : a);
}

// Advance every idle/waiting badge's text once a second, in place, from its data-since.
// The server bumps the deck version only when something it renders changes, so a parked
// pane wouldn't otherwise re-render — this keeps its clock climbing between the sparse
// re-parses without repainting the whole deck (which would disrupt peek/scroll/animation).
function tickBadges() {
  for (const el of document.querySelectorAll(".badge[data-since]")) {
    const a = el.classList.contains("b-waiting") ? "waiting" : "idle";
    const txt = `${a} ${fmtIdle(stateDur({ state_since: +el.dataset.since }))}`;
    // Write the inner text span, NOT el.textContent: the badge owns a permanent .pulse
    // child (see buildPaneHeader), and setting textContent on the badge would delete it.
    // setText's no-op also means a badge whose second hasn't ticked over is left
    // completely alone, so a selection inside it survives.
    setText(el.lastElementChild || el, txt);
  }
}

// Run the 1s badge clock ONLY while visible — a hidden/backgrounded PWA shouldn't wake
// the JS engine each second. On visibilitychange we stop the interval when hidden and, on
// return, tick once immediately (the badges froze while away) before rearming it.
let _badgeTimer = null;
function syncBadgeTick() {
  if (document.visibilityState === "visible") {
    if (_badgeTimer === null) _badgeTimer = setInterval(tickBadges, 1000);
    tickBadges();
  } else if (_badgeTimer !== null) {
    clearInterval(_badgeTimer);
    _badgeTimer = null;
  }
}
document.addEventListener("visibilitychange", syncBadgeTick);
window.addEventListener("pageshow", syncBadgeTick); // bfcache restore may skip visibilitychange

let _stateVersion = null;  // last deck version the server gave us — sent back to long-poll;
                           // null until the first reply so cold load asks for state outright
let _booted = false;       // server has completed its first tick — an empty deck is only
                           // "no panes" once this is true (before it, initial parses run)
// Long-poll /api/state: the request HOLDS on the server until the deck changes (pane
// switch, add/remove, label/activity, new events) or ~25s, then returns. pollLoop is the
// ONLY caller and runs one at a time, so there's no concurrent-fetch state to track —
// sends never poll, and nothing pauses this loop any more. Returns true
// on success, false to signal pollLoop to back off before the next hold.
async function poll() {
  try {
    // Omit `v` until we've received a first version, so the cold-load fetch is
    // unambiguously "give me current state now" — never conflated with a real echoed
    // version. (The server only holds when v == its current version AND version > 0, so
    // a null here keeps the first paint immediate regardless of startup timing.)
    const r = await fetch("/api/state" + (_stateVersion !== null ? `?v=${_stateVersion}` : ""));
    // Check status before parsing: when the tunnel/backend is down the relay
    // returns a non-JSON body (e.g. "no tunnel connected for …"), and blindly
    // JSON.parse-ing it throws a cryptic "Unexpected token" that we used to
    // misattribute to a stale app.js. Report the real condition instead.
    if (!r.ok) {
      const body = (await r.text()).trim().slice(0, 200);
      liveEl.className = "dot off";
      liveEl.title = "backend unavailable";
      const hint = r.status === 502 || r.status === 503 || r.status === 504
        ? "tunnel or backend is down — is the tunnel client running?"
        : "";
      // Write through the persistent empty-state node instead of replacing #panes: the
      // deck, card and peek live in there, and blowing them away would take the peek's
      // live stream and every wired handler with them.
      showNotice(`backend unavailable (${r.status})` + (body ? `: ${body}` : "")
        + (hint ? ` — ${hint}` : ""));
      return false;  // back off — without a gap pollLoop would re-request instantly and hammer
    }
    const data = await r.json();
    // Applied unconditionally, even mid-gesture: a render only writes text onto nodes the
    // gesture is animating, so there is no reason to drop a response (and dropping one
    // leaves the deck stale).
    // A well-formed response always carries a numeric version. version 0 = no initial
    // state yet; a missing/non-numeric version = an older daemon that predates long-poll.
    // Both must back off, else pollLoop re-requests with gap=0 and tight-loops the backend.
    const ok = typeof data.version === "number" && data.version > 0;
    if (ok) _stateVersion = data.version; // re-hold on this version
    // stale = the watcher loop stopped ticking (dead/stalled); served cards are frozen.
    liveEl.className = data.stale ? "dot off" : "dot";
    liveEl.title = data.stale ? "watcher stalled — cards may be frozen" : "live";
    // Compact usage readout in the top bar (next to the live dot): tokens · $cost ·
    // failed/total calls · calls/min. Lets you SEE the API-call volume (what tripped
    // the 429) at a glance, without a big scary banner for transient errors.
    showUsage(data.usage, data.llm_error);
    // Update the prefix button label/key from the server's auto-detected value.
    const pfx = document.getElementById("bar-prefix");
    if (pfx && data.prefix) {
      pfx.dataset.k = data.prefix;
      // Uppercase the key in the LABEL only (Ctrl-A, matching the other key buttons);
      // dataset.k keeps tmux's exact name (C-a).
      pfx.textContent = data.prefix.replace(/^C-(.)/, (_, k) => "Ctrl-" + k.toUpperCase());
    }
    // The literal Ctrl-B button exists because a remapped prefix (this host uses C-a)
    // leaves no way to send a real Ctrl-B, which nested tmux and apps that bind it
    // themselves need. On a STOCK tmux the prefix already IS C-b, so the two buttons
    // would be the same keystroke under two labels — hide the literal one there rather
    // than ship a duplicate. Keyed off the server's detected prefix, so it follows a
    // config change without a reload.
    //
    // OUTSIDE the `if (data.prefix)` guard, and the button starts `hidden` in the HTML.
    // Inside the guard, a response that omitted `prefix` never ran this line, so the
    // button kept whatever visibility it had — and since it shipped VISIBLE, a stock-C-b
    // host that then served one prefix-less response showed a permanent duplicate of the
    // Prefix button. Starting hidden and assigning unconditionally makes the state a pure
    // function of the last response instead of a latch.
    const cb = document.getElementById("bar-ctrl-b");
    if (cb) cb.hidden = !data.prefix || data.prefix === "C-b";
    // Legacy daemons omit `booted`; treat its absence as booted so old servers keep
    // their previous behavior (empty ⇒ "no panes") rather than spinning forever.
    _booted = data.booted !== false;
    render(data.panes || []);
    return ok; // ok=false (version 0 / legacy daemon) → pollLoop backs off
  } catch (e) {
    // Surface the real error instead of silently sitting on "Connecting…" forever.
    // "Failed to fetch" is usually a resume/network blip (the OS aborted the in-flight
    // long-poll while backgrounded), NOT a stale bundle — the resume handler below
    // re-polls immediately, so don't send the user hard-refreshing over a transient.
    liveEl.className = "dot off rc"; // pulsing gray = reconnecting, not dead
    liveEl.title = "reconnecting…";
    const transient = /failed to fetch|networkerror|load failed/i.test(String(e && e.message || e));
    // Report the NON-transient poll failures (a resume/network blip is expected noise);
    // a persistent JSON/parse fault is the invisible-on-mobile bug #57 is about.
    if (!transient) reportError("poll", e);
    // A transient blip while a deck is on screen (the Android app-switch case: the OS
    // aborts the in-flight long-poll, first re-poll fails while the radio wakes): KEEP
    // the cached UI — it was correct a second ago and the pulsing dot already says
    // "reconnecting". Nuking it for an error page threw away good content on every
    // app resume. Only show the notice when nothing is rendered yet (cold load) or the
    // failure isn't transient.
    // EITHER mode counts as "content on screen". Checking only the deck was wrong: in list
    // mode the deck is hidden and the LIST is what the user is looking at, so a blip there
    // fell through and replaced their rows with an error page — the exact behaviour this
    // guard exists to prevent, for half the UI. (main's version asked `panesEl.children
    // .length && !panesEl.querySelector(".empty")`, which was mode-agnostic; naming the
    // deck when the persistent nodes arrived is what narrowed it.) The empty/notice node
    // being visible means nothing is rendered yet, so a cold-load failure still shows.
    const showing = _panesUI && _panesUI.empty.classList.contains("hid")
      && (!_panesUI.deck.classList.contains("hid") || !_panesUI.list.classList.contains("hid"));
    if (transient && showing) return false;
    const hint = transient ? "reconnecting…" : "often a stale cached app.js — hard-refresh";
    showNotice(`poll error: ${String(e && e.message || e)} (${hint})`);
    return false;
  }
}

// The one place a full-screen message replaces the deck. It writes through the PERSISTENT
// empty-state node (never #panes.innerHTML), so the card, peek and their handlers survive
// an outage and are simply revealed again when the next poll succeeds.
function showNotice(msg) {
  const ui = panesUI();
  setCls(ui.deck, "hid", true);
  setCls(ui.list, "hid", true);
  setCls(ui.empty, "hid", false);
  setCls(ui.spinner, "hid", true);
  // Drop cardmode with the deck (#106). This path is reached from an outage while a CARD
  // was on screen, so the class is still set from that render — and it exists to switch OFF
  // #panes' bar padding, which only the viewport-sized deck can do without. With the deck
  // hidden the notice is a short block in an unpadded #panes, i.e. squeezed against the
  // fixed input bar. Same reason render()'s empty/loading branch clears it.
  panesEl.classList.remove("cardmode");
  setText(ui.emptyText, msg); // error text is untrusted-ish: textContent, never innerHTML
}
// Woken when the PWA returns to the foreground: an interruptible backoff sleep resolves
// early via this, so a resume cuts short the post-error wait AND the version reset makes
// the next poll return immediately instead of the server holding it ~25s.
let _wakePoll = null;
// Interruptible sleep: resolves after `ms` OR as soon as _wakePoll() is called.
function pollSleep(ms) {
  return new Promise((resolve) => {
    const t = setTimeout(() => { _wakePoll = null; resolve(); }, ms);
    _wakePoll = () => { clearTimeout(t); _wakePoll = null; resolve(); };
  });
}
// Drive the long-poll: as soon as one hold returns, start the next. The server does the
// waiting (holds ~25s or until change), so this is not a busy-loop — and pollLoop is the
// ONLY caller of poll(), so no concurrent-fetch coordination is needed. poll() returning
// false (backend down / legacy daemon) backs off, so we never tight-loop.
//
// The loop never pauses for rendering: nothing freezes renders any more, so there is no
// freeze flag that a missed release could leak into a wedged app.
async function pollLoop() {
  for (;;) {
    const ok = await poll();
    if (!ok) await pollSleep(1000); // outage / resume blip / legacy daemon: back off, wake early on resume
  }
}

// On resume from a backgrounded/suspended PWA, the OS-aborted long-poll leaves a stale
// "poll error" until the next hold returns (up to ~25s). Force an immediate fresh poll:
// null the version so the server answers at once (no hold), and wake any pending backoff.
function onResume() {
  if (document.visibilityState !== "visible") return;
  _stateVersion = null;           // next poll = "give me current state now", never held
  if (_wakePoll) _wakePoll();     // cut short an in-progress backoff sleep
  // Nothing to unwedge: backgrounding mid-gesture means the OS may never deliver touchend,
  // but an abandoned gesture leaves only per-instance state that the next touchstart
  // overwrites — no global flag can survive to freeze the returning user's app.
}
document.addEventListener("visibilitychange", onResume);
window.addEventListener("pageshow", onResume); // bfcache restore fires pageshow, not visibilitychange

const usageEl = document.getElementById("usage");
// The stats are dim debug telemetry, not primary chrome — so they hide behind the status
// dot: tapping the dot toggles the #usage popover. The dot is focusable (tabindex in HTML)
// so keyboard users get the same toggle; aria-expanded reflects popover state.
liveEl.onclick = () => {
  usageEl.hidden = !usageEl.hidden;
  liveEl.setAttribute("aria-expanded", String(!usageEl.hidden));
};
// Native-button key semantics: Enter fires on keydown; Space on keyup (keydown only
// suppresses page scroll) so key-repeat can't machine-gun the toggle.
liveEl.onkeydown = (e) => {
  if (e.key === "Enter") { e.preventDefault(); liveEl.click(); }
  else if (e.key === " ") e.preventDefault();
};
liveEl.onkeyup = (e) => {
  if (e.key === " ") { e.preventDefault(); liveEl.click(); }
};
// One labeled menu row: what the number IS on the left, the number on the right. Rows are
// DESCRIPTORS keyed by name, so the numbers tick in place — rebuilding would destroy the
// docs link under a user who has the popover open and is reaching for it.
const uRow = (key, label, val, cls = "") => ({ key, label, val, cls });
// The docs link rides in the popover, not the top bar — header space is too tight on phones
// for a rarely-tapped link. It's a link, not a stat, so it gets its own row shape.
const DOCS_ROW = { key: "docs", docs: true };

function applyUsageRows(rows) {
  keyedList(usageEl, rows, (r) => r.key, (r) => {
    const sp = document.createElement("span");
    if (r.docs) {
      sp.className = "u-row u-docs";
      const a = document.createElement("a");
      a.id = "docs-link";
      a.href = "/docs/";
      a.target = "_blank";
      a.rel = "noopener";
      a.title = "Design docs (opens in a new tab)";
      a.textContent = "design docs ↗";
      sp.appendChild(a);
      return sp;
    }
    sp.className = "u-row";
    sp._label = document.createElement("span");
    sp._val = document.createElement("span");
    // The error row shows a warning glyph rather than a number; the icon is static markup
    // built once here, and only its title (the untrusted error text) is written per poll.
    sp._icon = document.createElement("span");
    sp._icon.className = "warn";
    sp._icon.innerHTML = licon("alert", 12);
    sp._val.append(sp._icon);
    sp._num = document.createElement("span");
    sp._val.append(sp._num);
    sp.append(sp._label, sp._val);
    return sp;
  }, (sp, r) => {
    if (r.docs) return;
    setText(sp._label, r.label);
    setAttr(sp._val, "class", "u-val" + (r.cls || ""));
    setCls(sp._icon, "on", !!r.icon);
    setAttr(sp._icon, "title", r.icon ? r.title : null);
    setText(sp._num, r.icon ? "" : r.val);
  });
}

function showUsage(u, err) {
  if (!u) {
    // Gone (reconnect, fresh daemon): drop the stale stats but keep the docs link —
    // and DON'T touch hidden/aria-expanded: force-closing on every poll would slam
    // the popover shut under a user who opened it for the docs link.
    applyUsageRows([DOCS_ROW]);
    usageEl.title = "";
    return;
  }
  // Debug telemetry, not session-critical — so it sits dimmed in the background (CSS)
  // and brightens on hover/tap. The success ratio (was "648/657 ok") is noise here;
  // errors already surface via the ⚠ hint and the amber tint on cost. Rate is a plain
  // session average (calls/uptime) computed server-side — stable. `cost` is the COMBINED
  // parser + voice spend; the tooltip splits it (voice is billed at ~30× parser rates).
  const live = u.live || { cost: 0, sessions: 0, in_tokens: 0, out_tokens: 0 };
  // Total tokens = parser + voice, matching the combined `cost` (otherwise the readout
  // would show a parser-only token count next to a parser+voice dollar figure).
  const tok = ((u.in_tokens + u.out_tokens + live.in_tokens + live.out_tokens) / 1000).toFixed(0);
  const parser = (u.parser_cost ?? u.cost);
  const rows = [
    uRow("tok", "LLM tokens (parser + voice)", `${tok}k`),
    uRow("spend", "spend this run", `$${u.cost.toFixed(3)}`, u.errors ? " warn" : ""),
    uRow("rate", "parser calls", `${u.rate_per_min}/min`),
  ];
  if (live.sessions) {
    rows.push(uRow("vsess", "voice sessions", String(live.sessions)));
    rows.push(uRow("vspend", "voice spend (of total)", `$${live.cost.toFixed(3)}`));
    rows.push(uRow("pspend", "parser spend (of total)", `$${parser.toFixed(3)}`));
  }
  // The error text is untrusted; it rides as a title ATTRIBUTE via setAttr, which sets the
  // DOM property directly, so it needs no escaping.
  if (err) rows.push({ key: "err", label: "last LLM error", icon: true, title: String(err) });
  rows.push(DOCS_ROW);
  applyUsageRows(rows);
  usageEl.title = ""; // the labels ARE the explanation now — no tooltip needed
}
// No attribute escaping helper: nothing interpolates untrusted values into attribute
// markup. Every attribute goes through setAttr (a direct DOM property write), so quotes
// and angle brackets in parser JSON cannot break out. esc() survives for the one remaining
// markup template, the fullscreen overlay's static chrome.


function render(states) {
  panesById = Object.fromEntries(states.map((s) => [s.pane_id, s]));
  // Refetch each pane's server-side activity log if its events_seq advanced — AFTER
  // panesById is set, because syncEvents' async completion checks activeId() (which
  // reads panesById) to decide whether to re-render the visible feed.
  states.forEach(syncEvents);
  // Prune per-pane caches when panes vanish — otherwise pane churn grows them
  // without bound over a long-running session.
  for (const m of [eventLog, peekCache, bgZoom, cardUI ? cardUI.events.openByPane : {}])
    for (const k of Object.keys(m)) if (!has(panesById, k)) delete m[k];
  setFavicon(states.some((s) => actOf(s) === "waiting"));
  // No card visible (empty / list mode) ⇒ no peek stream should be running. bgTerm
  // restarts it when a card renders; here we make sure it's stopped otherwise.
  const stopPeek = () => {
    if (peekStop) { peekStop(); peekStop = null; }
    peekStreamPane = peekBox = peekWrap = null;
    peekLive = false;
  };
  const ui = panesUI();
  if (!states.length) {
    stopPeek();
    // Sweep the tab-join fillets: they're parented to #top (to escape the dock's overflow
    // clip), so a hidden deck leaves them dangling over the empty screen — the two stray
    // blue curves seen during a daemon reload's brief no-panes.
    document.querySelectorAll(".tab-fillet").forEach((e) => e.remove());
    _joinRO.disconnect(); // stop watching the card we're about to hide
    keyedList(dockEl, [], (x) => x, () => null);
    keyedList(filtersEl, [], (x) => x, () => null); // no panes ⇒ no tallies to filter by
    // Drop the card-view dock state too: the seam classes would style a dock that no
    // longer has a card.
    dockEl.onscroll = null;
    dockEl.classList.remove("edge-l", "has-sel");
    setCls(ui.deck, "hid", true);
    setCls(ui.list, "hid", true);
    setCls(ui.empty, "hid", false);
    // Empty deck has two causes: still loading (server booting / initial pane parses in
    // flight) vs. genuinely no panes. Only claim "no panes" once the server has booted —
    // otherwise show a spinner, since panes may exist and just aren't parsed yet.
    // The spinner is a PERMANENT node: recreating it across the several polls a load can
    // span would restart its CSS animation and visibly jitter.
    setCls(ui.spinner, "hid", !!_booted);
    setText(ui.emptyText, _booted
      ? "No tmux pane found. Start a session and it will appear here."
      : "Loading panes…");
    // The empty/loading state is neither mode, and it returns BEFORE the branches below —
    // so clear cardmode here (#106) or a stale one from the last render leaves #panes with
    // no bar padding and squeezes the message against the input bar.
    panesEl.classList.remove("cardmode");
    updateBar(null);
    return;
  }
  setCls(ui.empty, "hid", true);
  // Only the ACTIVE pane gets a full card. Other AGENT panes (and anything waiting) each
  // get a compact row above it; plain shells fold into one summary line so a big fleet
  // doesn't shove the active card off screen.
  //
  // NO render-skip fingerprint (_renderFp/_renderFpLast are gone). It existed to protect
  // live nodes from a rebuild that no longer happens: every write below goes through
  // setText/setAttr/setCls/setHtml, each a no-op when the value is already current, so an
  // unchanged poll performs ZERO DOM mutations without having to predict that in advance.
  // That is strictly better than the fingerprint, which had to ENUMERATE the drawn fields
  // and silently skipped whatever it forgot — it omitted parsed_at and dropped 6 of 10 real
  // content changes (a task's text, a link's label, a copyable's payload: all rewrites that
  // left the sampled LENGTHS unchanged). Here those same changes flow through setText,
  // which compares the actual value and writes it. There is no field to remember.
  const act = activeId();
  // List mode (a dock tally badge or "all" was tapped): just those panes as one-liners;
  // the dock stays up (tap an icon or a row to open that pane's card).
  const subset = listFilter && states.filter((s) => listFilter === "all" || actOf(s) === listFilter);
  if (subset && subset.length) {
    stopPeek(); // list mode: no card, no peek stream
    setCls(ui.deck, "hid", true);
    setCls(ui.list, "hid", false);
    dock(states, act); // dock stays up in list mode — icon tap jumps to that card
    panesEl.classList.remove("cardmode"); // list rows need the bar padding to clear the bar
    // Session headers, ordinals and row order all live in applyList — it keys headers and
    // rows into one list so a session boundary appearing/disappearing inserts or removes
    // exactly that header, instead of re-keying the rows after it.
    applyList(ui.list, states, subset, act);
    updateBar(panesById[act]);
    return;
  }
  listFilter = null; // filter emptied out (e.g. last waiting pane answered) — card view
  setCls(ui.list, "hid", true);
  // Hiding the list must also EMPTY it: a .sess-hdr:first-child rule and the row nodes
  // themselves would otherwise linger behind the hidden class, and stale rows holding
  // pane state is exactly what this refactor is removing.
  applyList(ui.list, states, [], act);
  dock(states, act); // sticky top bar — constant height, content swaps below it
  const a = panesById[act];
  // #106: drop #panes' bar padding in card mode, where the deck is already sized to the
  // remaining viewport AND runs under the bar via its own negative margin, so counting the
  // bar height again scrolled the whole DOCUMENT ~62px behind the card. Keyed to the deck
  // being SHOWN, not merely to reaching this branch: with no active pane the deck is hidden
  // and nothing is sized to the viewport, so the padding is what keeps content off the bar.
  // (A class, not :has() — app.js deliberately avoids :has() for older iOS Safari.)
  setCls(panesEl, "cardmode", !!a);
  setCls(ui.deck, "hid", !a);
  if (a) {
    applyCard(cardUI, a);
    bgTerm(a);
    ui.fs._pane = a.pane_id;
    ui.fs._label = a.title || a.label;
    joinTab(ui.deck);
  }
  updateBar(panesById[act]);
}

// #panes' one-time skeleton: the empty/loading notice, the list container, and the deck
// (a positioning context holding the card, the background terminal and the ⤢ button).
// All three are created once and shown/hidden by class — never replaced.
let _panesUI = null;
function panesUI() {
  if (_panesUI) return _panesUI;
  const empty = document.createElement("div");
  empty.className = "empty";
  const spinner = document.createElement("span");
  spinner.className = "spinner";
  spinner.setAttribute("aria-hidden", "true");
  const emptyText = document.createElement("div");
  empty.append(spinner, emptyText);
  const list = document.createElement("div");
  list.className = "plist";
  const deck = document.createElement("div");
  deck.className = "deck";
  const fs = document.createElement("button");
  fs.className = "fsbtn";
  // Inline SVG, not the ⤢ glyph: the phone's font fallback renders the char with odd
  // metrics (tiny ink, wide advance), distorting the button — confirmed by A/B on device.
  fs.innerHTML =
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"' +
    ' stroke-width="2" stroke-linecap="round" aria-hidden="true">' +
    '<path d="M14 4h6v6M20 4l-7 7M10 20H4v-6M4 20l7-7"/></svg>';
  fs.title = "Full screen";
  fs.setAttribute("aria-label", "Full screen");
  // Reads the pane it targets off the node at CALL time, so the one persistent button
  // always opens the pane the deck currently shows.
  fs.onclick = () => { if (fs._pane) openScreen(fs._pane, fs._label); };
  cardUI = buildCard();
  // flex column: DOM order = visual order. bgTerm's wrap is inserted between the card and
  // the ⤢ by buildPeek (see bgTerm), which owns that slot for the life of the page.
  deck.append(cardUI.root, fs);
  // REPLACE the boot placeholder (#panes ships with a "Connecting…" .empty div) rather than
  // appending beside it — otherwise it lingers above the deck forever, since nothing else
  // ever clears #panes now.
  panesEl.replaceChildren(empty, list, deck);
  return (_panesUI = { empty, spinner, emptyText, list, deck, fs });
}

// The tab-to-card join hardware: a 1px card-colored notch laid over the card's blue
// top border under the selected dock icon (the break that lets the tab's open bottom
// flow into the card), plus a concave fillet at each of the tab's feet curving the
// line up into the tab's sides, Chrome-style (see .tab-notch / .tab-fillet CSS).
// All positioned from measured rects (never guessed), re-pinned on dock scroll.
// Fillets live in #top (the deck would clip their above-the-line half); stale ones
// are swept each call — and by dock() in list mode, where there's no card to join.
function joinTab(deck) {
  const top = document.getElementById("top");
  top.querySelectorAll(".tab-fillet").forEach((e) => e.remove());
  // Stop watching the prior render's card up front, and drop the prior pin() closure so
  // EVERY exit — including the list-mode early return below (no selected icon, no card to
  // join) — releases the observer AND the closure's captured DOM nodes for GC.
  _joinRO.disconnect();
  _joinPin = null;
  if (!dockEl.querySelector(".dock-icon.sel")) return;
  const n = document.createElement("i");
  n.className = "tab-notch";
  deck.appendChild(n);
  // Two fillets, one per tab foot (mirror-image gradients — see the .tab-fillet CSS).
  const [fl, fr] = ["l", "r"].map((side) => {
    const f = document.createElement("i");
    f.className = "tab-fillet " + side;
    top.appendChild(f);
    return f;
  });
  const pin = _joinPin = () => {
    // Resolve the selected icon at CALL time, never from a closure. Dock icons are now
    // permanent nodes whose .sel class MOVES between them (see the render invariant), so
    // a captured `sel` would keep measuring whichever icon happened to be selected when
    // this join was created — pinning the fillets under the wrong tab after a switch.
    const sel = dockEl.querySelector(".dock-icon.sel");
    if (!sel) return;
    const s = sel.getBoundingClientRect(),
      d = deck.getBoundingClientRect(), t = top.getBoundingClientRect(),
      dock = dockEl.getBoundingClientRect();
    // The fillets/notch live in #top (outside the dock's overflow clip), so when the
    // selected icon scrolls out of the dock's visible strip nothing clips them — they'd
    // orphan as a stray blue arc at the clip edge. Self-hide when the icon (with its 7px
    // flare) leaves the dock's horizontal bounds; the next in-view pin() re-shows them.
    if (s.right - 7 < dock.left || s.left + 7 > dock.right) {
      // Hiding the fillets isn't enough: the seam classes (edge-l / sq-l / sq-r) styled
      // the join too, so leaving them set mis-renders the dock border and card corners
      // under a join that's no longer drawn. Tear the whole join down to its no-join
      // state; the in-view pin() below re-sets all of them when the icon scrolls back.
      fl.style.display = fr.style.display = n.style.display = "none";
      dockEl.classList.remove("edge-l");
      deck.querySelector(".card.active")?.classList.remove("sq-l", "sq-r");
      return;
    }
    fr.style.display = n.style.display = "";
    // Selected tab sits flush against the dock's LEFT edge — no room for the left flare,
    // so hide fl and let the tab's own blue left border BE the line, collinear with the
    // card below (see .edge-l CSS). This is GEOMETRIC (the icon's left flare at/past the
    // dock edge), NOT "is it the DOM-first icon": scroll can put any icon flush-left, and
    // an icon can be visually first without being first in DOM order. Same test drives
    // sq-l below, so they stay consistent.
    const edgeL = s.left - dock.left - 7 < 14;
    dockEl.classList.toggle("edge-l", edgeL);
    fl.style.display = edgeL ? "none" : "";
    n.style.left = s.left - d.left + 1 + "px"; // inset 1px each side: the fillets own
    n.style.width = s.width - 2 + "px";        // the corner pixels
    // A fillet needs a FLAT border line under it; inside the card's corner-radius
    // zone the border curves away and nothing lines up. When an edge tab's flare
    // would land there, square that corner (14 = the .card border-radius).
    const card = deck.querySelector(".card.active");
    if (card) {
      card.classList.toggle("sq-l", edgeL);
      card.classList.toggle("sq-r", d.right - s.right - 7 < 14);
    }
    // Each patch is placed so the arc's center (its top corner) sits radius-8 from the
    // tab border it curves into — tangency by construction, not tuning. The patch spans
    // the tab's 1px side border plus the flare beyond it (the flare replaces the border).
    fl.style.top = fr.style.top = t.height - 7 + "px";
    fl.style.left = s.left - t.left - 7 + "px";
    fr.style.left = s.right - t.left - 1 + "px";
  };
  // Measure AFTER layout: joinTab runs right after replaceChildren(deck), before the
  // browser has laid the new deck out, so a synchronous pin() reads stale rects and the
  // fillets land against the OLD geometry — visible as a square corner for a beat until
  // the next render corrects it (worse now that switching is fast). rAF defers the first
  // measure to post-layout. Guard: a rapid re-render sweeps these nodes, so skip if this
  // fillet was already removed (isConnected) — its own render's pin owns the corner now.
  requestAnimationFrame(() => { if (fl.isConnected) pin(); });
  dockEl.onscroll = pin;
  // Re-pin whenever the active card's height changes BETWEEN renders — a live-updating
  // card (its summary/events growing as the pane works) reflows the deck under the
  // already-placed fillets, leaving them detached from the card's moved top edge. render()
  // re-pins on a full re-render but not on a same-card height change, so without this the
  // busy active pane shows disconnected tab-tops. The observer was disconnected at the top
  // of joinTab; re-target it at this render's card (guarded above so list mode leaves it off).
  const card = deck.querySelector(".card.active");
  if (card) _joinRO.observe(card);
}

// The current joinTab's pin() closure, so the shared card-resize observer below can
// re-run the latest one (each joinTab reassigns it). null before the first join.
let _joinPin = null;
let _joinRAF = 0; // pending rAF handle, so bursts of resize callbacks coalesce to one pin
// Reused across renders so we never leak observers; joinTab disconnects + re-observes
// the current active card each time. rAF: ResizeObserver fires mid-layout, so defer the
// measure a frame; coalesce multiple callbacks in one frame into a single pin(); guard on
// a still-present fillet in case a re-render swept it.
const _joinRO = new ResizeObserver(() => {
  if (_joinRAF) return; // a pin is already queued for the next frame
  _joinRAF = requestAnimationFrame(() => {
    _joinRAF = 0;
    const fl = document.querySelector("#top .tab-fillet");
    if (fl && fl.isConnected && _joinPin) _joinPin();
  });
});

// The pane dock: one icon per pane in tmux window order — active highlighted, a
// colored dot showing each pane's activity. Tap an icon to jump; it doubles as the
// page indicator while swiping the card. Folded shells and dismissed waiters live here.
// Pane order everywhere (dock, list, swipe) is the SERVER's array order — tmux's own
// session/window/pane order, matching the window numbers in tmux's status bar.
// (%id creation order scrambles as windows come and go; don't sort by it.)

// List filter: null = card view; "all"/"waiting"/"running"/"idle" = one-liner list
// of just those panes (tapped via the dock's tally badges / "all").
let listFilter = null;
const dockEl = document.getElementById("dock");
const filtersEl = document.getElementById("filters"); // pane filters, homed in the header
// One dock icon, built once per pane. The handler closes over the pane_id STRING
// (never over `s`), so it stays correct for the life of the node no matter how many
// polls rewrite the icon around it.
function buildDockIcon(paneId) {
  const b = document.createElement("button");
  b.className = "dock-icon";
  b.dataset.pane = paneId;
  const im = document.createElement("img");
  im.width = im.height = 22;
  im.style.borderRadius = "5px";
  const dot = document.createElement("i");
  dot.setAttribute("aria-hidden", "true");
  const sac = document.createElement("sub");
  sac.className = "sacount";
  sac.setAttribute("aria-hidden", "true");
  b.append(im, dot, sac);
  b._im = im; b._dot = dot; b._sac = sac;
  // Jump to that pane's CARD — including from list mode (a dock tap means "show me
  // this pane", not "re-highlight it inside the list").
  b.onclick = () => { listFilter = null; setActive(paneId); };
  return b;
}

// Write the current state of one pane onto its existing icon.
function applyDockIcon(b, s, act) {
  const a = actOf(s);
  const nsub = nsubOf(s);
  setCls(b, "sel", s.pane_id === act);
  setAttr(b._im, "src", logoFor(s.tool));
  setAttr(b._im, "alt", s.tool || "pane");
  // The activity dot is a permanent node whose class carries the state; idle panes get
  // no dot at all — quiet is the default, only running/waiting/compacting earn a signal.
  const dotted = a === "running" || a === "waiting" || a === "compacting";
  setAttr(b._dot, "class", dotted ? `ddot d-${a}` : "ddot-off");
  setText(b._sac, nsub > 0 ? String(nsub) : "");
  const title = s.title || s.label || s.pane_id;
  setAttr(b, "title", title);
  // Fold the sub-agent count into the button's own label so AT announces it.
  setAttr(b, "aria-label", title + (nsub > 0 ? `, ${nsub} sub-agent${nsub === 1 ? "" : "s"}` : ""));
}

function dock(states, act) {
  const el = dockEl;
  // Card view only: the selected icon joins to the card below it (see .has-sel CSS).
  // In list mode there's no card under the dock, so no seam to open — and no fillets,
  // and no scroll re-pin handler.
  const joined = !listFilter && states.some((s) => s.pane_id === act);
  setCls(el, "has-sel", joined);
  if (!joined) {
    document.querySelectorAll(".tab-fillet").forEach((e) => e.remove());
    el.onscroll = null;
    el.classList.remove("edge-l");
  }
  // Chrome-tab-group-style session trays: each session's run of icons shares one
  // .dock-group span; the tray CSS paints a colored rail + session name above it. The
  // array is already in tmux session order, so a group is just "same session as the
  // previous icon".
  //
  // CRITICAL: .dock-group:nth-of-type(4n+1..4n) sets each tray's hue from its DOM
  // POSITION, so the groups must be reconciled IN ORDER — keyedList's forward pass
  // guarantees that, and keying by session name means a session keeps its own node
  // (and therefore its icons' handlers) across polls.
  const groups = [];
  for (const s of states) {
    const sess = s.session ?? "";
    if (!groups.length || groups[groups.length - 1].sess !== sess) groups.push({ sess, panes: [] });
    groups[groups.length - 1].panes.push(s);
  }
  keyedList(el, groups, (g) => g.sess, (g) => {
    const sp = document.createElement("span");
    sp.className = "dock-group";
    sp.dataset.sess = g.sess;
    return sp;
  }, (sp, g) => {
    keyedList(sp, g.panes, (s) => s.pane_id,
      (s) => buildDockIcon(s.pane_id),
      (b, s) => applyDockIcon(b, s, act));
  });
  // Tray chrome (rails + labels + the padding that hosts them) only when the deck
  // actually spans sessions — the CSS keys off this class, not group count.
  setCls(el, "grouped", groups.length > 1);
  // Density + navigation: per-activity tallies homed in the header title bar
  // (#filters) — always visible no matter how many dock icons crowd the strip. Each
  // one FILTERS the list view to those panes; "all" lists everything.
  const n = {};
  states.forEach((s) => (n[actOf(s)] = (n[actOf(s)] || 0) + 1));
  const tallies = ["waiting", "running", "compacting", "idle", "unknown"]
    .filter((a) => n[a]).map((a) => ({ key: a, label: `${n[a]} ${a}` }));
  tallies.push({ key: "all", label: "all" });
  // #filters:empty { display: none } — keyedList removes emptied children outright, so
  // that rule keeps working. Badges are keyed by activity, so the "3 running" badge is
  // ONE node whose text changes as the count moves, not a new node per poll.
  keyedList(filtersEl, tallies, (t) => t.key, (t) => {
    const b = document.createElement("button");
    b.className = "badge b-" + t.key;
    b.onclick = () => { listFilter = t.key; render(Object.values(panesById)); };
    return b;
  }, (b, t) => setText(b, t.label));

  // With many panes the dock scrolls horizontally, and the selected icon can sit off
  // screen — its card then joins to a tab that isn't visible (looks severed). Center
  // the selected icon in the strip so it and its tab-join are always on screen.
  // Deferred a frame so freshly-inserted icons are laid out. Gate on `joined` (card
  // view AND the selected tab joined to a card) — NOT just a .sel, which also exists
  // in list mode. Only scroll when the icon is actually clipped, else every render
  // would re-fire a scroll and fight the resting position.
  const sel = joined && el.querySelector(".dock-icon.sel");
  if (sel) requestAnimationFrame(() => {
    if (!sel.isConnected) return;
    const i = sel.getBoundingClientRect(), c = el.getBoundingClientRect();
    if (i.left < c.left || i.right > c.right)
      sel.scrollIntoView({
        inline: "center", block: "nearest",
        // Respect reduced-motion: jump instead of glide.
        behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      });
  });
}

// One list row per pane, built once and keyed by pane_id, so the row the user is
// pressing is still there when their finger lifts.
//
// The filter switch is INSTANT by design: the old flying-clone animation only existed
// because rows were deleted before they could animate. Rows persist now, so a future
// transition would be a genuine FLIP on the surviving nodes (measure, invert, play).
//
// Structure: TWO SIBLING buttons, never nested — role=button on the row itself would be
// invalid ARIA once the sub-agents toggle (a real <button>) moved in. The icon+name+
// headline area is .row-open, a real button that opens the card (keyboard operable); the
// toggle sits beside it in .ph-right. The row div stays a pointer target so taps on its
// padding still open the card.
function buildRow(paneId) {
  const el = document.createElement("div");
  el.className = "prow";
  el.dataset.pane = paneId;
  const goCard = () => { listFilter = null; setActive(paneId); };
  el.onclick = goCard;
  const openBtn = document.createElement("button");
  openBtn.className = "row-open";
  // stopPropagation so an activation on the button doesn't also bubble to the row and
  // navigate twice.
  openBtn.onclick = (e) => { e.stopPropagation(); goCard(); };
  el.appendChild(openBtn);
  // link:false — this header goes INSIDE .row-open, a <button>. See buildPaneHeader.
  const hdr = buildPaneHeader(openBtn, { icon: true, link: false });
  // .ph-right belongs to the ROW, beside .row-open — not inside it (see the ARIA note).
  el.appendChild(hdr.right);
  // A pane with sub-agents gets a labeled chip under the activity badge (reusing the card
  // meta's .chip.agents look) that toggles the SAME sub-agents box the card shows — one
  // component, two surfaces. The open flag lives ON THE ROW NODE rather than in a
  // module-level Set, since the row persists. Toggling repaints just this row.
  const toggle = document.createElement("button");
  toggle.className = "badge sub-toggle"; // same pill as the activity badge, agents purple
  const subsBox = buildTasks("Sub-agents", "tasks subagents");
  toggle.onclick = (e) => {
    e.stopPropagation(); // a toggle tap expands the box — it must not open the card
    el._subsOpen = !el._subsOpen;
    const cur = panesById[paneId];
    if (cur) applyRowSubs(el, realSubs(cur.subagents));
  };
  hdr.right.appendChild(toggle);
  el.appendChild(subsBox.root);
  el._hdr = hdr; el._toggle = toggle; el._subs = subsBox; el._subsOpen = false;
  return el;
}

// The row's sub-agent chip + expandable box. Split out so the toggle handler can repaint
// just this row without a full render.
function applyRowSubs(el, subs) {
  const n = subs.length;
  setCls(el._toggle, "hid", !n);
  const open = !!el._subsOpen && n > 0;
  if (n) {
    // ◂ when closed (at the row's right edge a ▸ reads as "navigate", not "expand")
    setText(el._toggle, `${n} sub-agent${n === 1 ? "" : "s"} ${open ? "▾" : "◂"}`);
    setAttr(el._toggle, "aria-expanded", String(open));
  }
  setCls(el, "subs-open", open);
  applySubagents(el._subs, open ? subs : []);
}

function applyRow(el, s, act) {
  setCls(el, "waiting", actOf(s) === "waiting");
  setCls(el, "sel", s.pane_id === act);
  applyPaneHeader(el._hdr, s, false);
  // Renderable entries only — the same filter the box uses, so the chip's count can never
  // disagree with it.
  applyRowSubs(el, realSubs(s.subagents));
}

// Rows in server order — same as the dock. That order is tmux's own session/window/pane
// order, so grouping windows under their session is just "insert a header where the
// session changes": no sorting, no client-side restructure.
//
// Headers and rows share ONE keyed list, because .sess-hdr:first-child is a structural
// selector — the header has to be a real sibling at the right index, not a wrapper.
// Hue ordinals are JS-computed over the FULL deck (never nth-of-type, never the filtered
// subset), so a header's hue matches that session's dock rail even when a filter hides
// sessions.
function applyList(host, states, subset, act) {
  const ord = new Map();
  states.forEach((s) => { if (!ord.has(s.session)) ord.set(s.session, ord.size); });
  // Headers only when the deck actually spans sessions — a lone header over every row
  // would be noise for the single-session common case.
  const items = [];
  subset.forEach((s, i) => {
    if (ord.size > 1 && (!i || subset[i - 1].session !== s.session))
      items.push({ hdr: true, session: s.session, c: (ord.get(s.session) % 4) + 1 });
    items.push({ hdr: false, s });
  });
  keyedList(host, items,
    (it) => (it.hdr ? "h:" + it.session : "r:" + it.s.pane_id),
    (it) => (it.hdr ? document.createElement("div") : buildRow(it.s.pane_id)),
    (node, it) => {
      if (it.hdr) {
        setAttr(node, "class", "sess-hdr c" + it.c);
        setText(node, it.session);
      } else {
        applyRow(node, it.s, act);
      }
    });
}



// ONE persistent card, retargeted to whichever pane is active — NOT one card per pane.
// `cardUI` holds its handles; `cardUI.pane` is the pane_id it currently shows.
let cardUI = null;

// Every handler here closes over NOTHING but the card itself, and reads the pane it
// currently targets from cardUI.pane at call time — so retargeting the card to another
// pane cannot leave a handler pointing at the old one.
function buildCard() {
  const el = document.createElement("div");
  el.className = "card";
  const ui = { root: el, pane: null };
  // Tapping a card makes it the target of the single bottom input bar.
  el.onclick = (e) => {
    // don't steal option/timeline taps
    if (e.target.closest("button, input, a, summary, details")) return;
    if (ui.pane) setActive(ui.pane);
  };
  const row = document.createElement("div");
  row.className = "row";
  // Shared header (see buildPaneHeader). The card adds the collapse caret and omits the
  // icon — its dock tab above IS the icon. The ▾/▸ caret collapses the card to just this
  // header row (still tab-joined), handing the live terminal the screen; collapse state
  // is view-wide (cardsCollapsed) so swiping panes keeps the chosen height.
  ui.hdr = buildPaneHeader(row, { caret: true, link: true }, (e) => {
    e.stopPropagation(); // don't also re-select the pane
    cardsCollapsed = !cardsCollapsed;
    render(Object.values(panesById));
  });
  el.appendChild(row);
  // Every subview is created ONCE, in its fixed order, and shown/hidden by class. Order
  // matters and is encoded here rather than by append order per render: tables render
  // BEFORE the question so they act as context above the options.
  ui.lm = document.createElement("div");
  ui.lm.className = "lm-convo";
  ui.sum = document.createElement("div");
  ui.sum.className = "sess-sum";
  ui.sum.onclick = (e) => {
    e.stopPropagation();
    // The summary is linkified, so it can hold anchors. A tap on one must NAVIGATE only:
    // toggling as well would collapse the text out from under the user as the link opens,
    // and on an element that is simultaneously the content AND the expand affordance that
    // reads as a glitch rather than two intended actions.
    if (e.target.closest("a")) return;
    ui.sum.classList.toggle("open");
  };
  ui.rewind = buildRewind();
  ui.tables = document.createElement("div");
  ui.tables.className = "tables";
  ui.q = buildQuestion(ui);
  ui.tasks = buildTasks("Tasks", "tasks");
  ui.subs = buildTasks("Sub-agents", "tasks subagents");
  ui.links = document.createElement("div");
  ui.links.className = "links";
  ui.copy = document.createElement("div");
  ui.copy.className = "copyables";
  ui.events = buildEvents();
  el.append(ui.lm, ui.sum, ui.rewind.root, ui.tables, ui.q.root, ui.tasks.root,
    ui.subs.root, ui.links, ui.copy, ui.events.root);
  // The swipe listens on the card for the life of the page; it reads the pane it acts on
  // from cardUI.pane at gesture time.
  swipeNav(el, ui);
  return ui;
}

// Point the one card at `s` and write its current state. Subviews that don't apply are
// hidden AND emptied (keyedList with an empty list removes their children), so the CSS
// :empty rules keep working and no stale content lurks behind a hidden class.
function applyCard(ui, s) {
  const el = ui.root;
  ui.pane = s.pane_id;
  const collapsed = cardsCollapsed;
  setCls(el, "waiting", actOf(s) === "waiting");
  setCls(el, "active", s.pane_id === activeId());
  setCls(el, "collapsed", collapsed);
  setCls(el, "reparsing", isReparsing(s)); // input sent, awaiting the forced re-parse
  applyPaneHeader(ui.hdr, s, collapsed);
  // Collapsed: the one-line form, everything below the header hidden. Live Mode: the
  // voice interface owns everything below the header, in place of the pane's summary,
  // question and event views.
  const lmOwns = !collapsed && !!lmWs && s.pane_id === activeId();
  const body = !collapsed && !lmOwns;
  setCls(ui.lm, "hid", !lmOwns);
  if (lmOwns) lmPaintInto(ui.lm); else keyedList(ui.lm, [], (x) => x, () => null);
  // The bootstrap "story so far" — orientation when picking a session up cold. Clamped
  // to a few lines; tap toggles the full text.
  // linkifyText, not textContent: a summary that mentions a PR or a URL should be tappable
  // here exactly as it is in the terminal below. It escapes everything it does not turn
  // into an anchor, so it is safe on this untrusted model text. setHtml's unchanged-guard
  // is what keeps a poll from rebuilding the subtree under a selection.
  setHtml(ui.sum, body && s.session_summary ? linkifyText(s.session_summary) : "");
  applyRewind(ui.rewind, body ? s : null);
  applyTables(ui.tables, body && Array.isArray(s.tables) ? s.tables : []);
  applyQuestion(ui.q, body && s.question ? s : null);
  applyTasks(ui.tasks, body && Array.isArray(s.tasks) ? s.tasks : []);
  applySubagents(ui.subs, body ? realSubs(s.subagents) : []);
  applyLinks(ui.links, body && Array.isArray(s.links) ? s.links : []);
  applyCopy(ui.copy, body && Array.isArray(s.copyables) ? s.copyables : []);
  const log = (eventLog[s.pane_id] || {}).events || [];
  applyEvents(ui.events, body ? log : [], s.pane_id, body ? s.summary : null);
  // No per-card input — one persistent bar at the bottom of the page handles
  // text/keys/images for whichever card is active (see the #bar element).
}

// Horizontal swipe on the active card switches panes (tmux window order, wraps), as a
// carousel: the NEIGHBOR card slides in alongside your finger (so it reads as paging, not
// dismissal), both animate home on commit, short/vertical drags snap back. Touches
// starting in a horizontal scroller (tables) keep their native gesture.
//
// Wired ONCE, for the life of the page: it reads the pane it acts on from `ui.pane` at
// gesture time rather than closing over an id, since the card is retargeted rather than
// rebuilt.
//
// The id list is SNAPSHOTTED at touchstart: a poll landing mid-swipe can reorder
// panesById, and without the snapshot the pane you commit to is not the one whose ghost
// you saw. Renders during the swipe need no other guard — they only rewrite text on the
// nodes the gesture is translating.
function swipeNav(el, ui) {
  let sx = null, sy = null, dx = 0, ghost = null, gdir = 0, ids = [];
  const neighbor = (dir) => { // dir -1: swiping left reveals the NEXT pane; +1: previous
    const i = ids.indexOf(ui.pane);
    if (i < 0) return null;
    return ids[(i + (dir < 0 ? 1 : ids.length - 1)) % ids.length];
  };
  const W = () => el.offsetWidth + 12; // card width + gap
  const clear = () => { if (ghost) ghost.remove(); ghost = null; gdir = 0; };
  el.addEventListener("touchstart", (e) => {
    sx = e.target.closest(".tbl-scroll, .bg-wrap") ? null : e.touches[0].clientX;
    sy = e.touches[0].clientY; dx = 0; clear();
    ids = Object.keys(panesById); // insertion order = server (tmux) order, frozen for this gesture
  }, { passive: true });
  el.addEventListener("touchmove", (e) => {
    if (sx == null) return;
    dx = e.touches[0].clientX - sx;
    if (Math.abs(dx) <= Math.abs(e.touches[0].clientY - sy) || Math.abs(dx) <= 10) return;
    el.style.transition = "none"; // track the finger 1:1, no easing lag
    el.style.transform = `translateX(${dx}px)`;
    const dir = dx < 0 ? -1 : 1;
    if (dir !== gdir && ids.length > 1) {
      clear(); gdir = dir;
      // A throwaway peek at the neighbour, not a second card: it exists for ~200ms, so it
      // shows the incoming pane's HEADER only — subviews would never be seen, and a full
      // card here would violate the one-persistent-card rule.
      const nb = panesById[neighbor(dir)];
      if (nb) {
        ghost = document.createElement("div");
        ghost.className = "card ghost";
        ghost.setAttribute("aria-hidden", "true"); // throwaway chrome is never a control
        const grow = document.createElement("div");
        grow.className = "row";
        applyPaneHeader(buildPaneHeader(grow, {}), nb, false);
        ghost.appendChild(grow);
        ghost.style.transition = "none";
        el.parentElement.appendChild(ghost);
      }
    }
    if (ghost) ghost.style.transform = `translateX(${dx - gdir * W()}px)`;
  }, { passive: true });
  el.addEventListener("touchend", (e) => {
    if (sx == null) return;
    const dy = e.changedTouches[0].clientY - sy;
    sx = null;
    el.style.transition = "";
    if (ghost) ghost.style.transition = "";
    if (Math.abs(dx) < 70 || Math.abs(dx) < 2 * Math.abs(dy) || ids.length < 2) {
      el.style.transform = ""; // snap back, neighbor retreats
      if (ghost) { ghost.style.transform = `translateX(${-gdir * W()}px)`; setTimeout(clear, 160); }
      return;
    }
    const dir = dx < 0 ? -1 : 1;
    const target = neighbor(dir);
    el.style.transform = `translateX(${dir * W()}px)`;
    if (ghost) ghost.style.transform = "translateX(0)";
    setTimeout(() => {
      // Clear the transform BEFORE retargeting: the one persistent card slides back to
      // its home position and the render points it at the new pane, so the swipe reads as
      // paging rather than the card snapping back with new content.
      el.style.transition = "none";
      el.style.transform = "";
      requestAnimationFrame(() => { el.style.transition = ""; });
      clear();
      if (target) setActive(target);
    }, 150);
  });
  // A cancelled gesture (OS interruption) must snap back.
  el.addEventListener("touchcancel", () => {
    sx = null;
    el.style.transition = "";
    el.style.transform = "";
    if (ghost) { ghost.style.transform = `translateX(${-gdir * W()}px)`; setTimeout(clear, 160); }
  });
}

// The deck's background terminal layer: the pane's latest capture, bottom-anchored so
// its last lines tuck BEHIND the fixed input bar (agent status-line/input chrome, not
// content — a parser field could size that per tool later). The card floats over the
// top; the live tail pokes out below it, pan/zoomable in place.
// Fetched only when the snapshot id changes; pinch/pan state persists per pane.
const peekCache = {}; // pane_id -> {html} — last live frame, shown gray on remount
let peekStop = null;        // stop() for the active peek's live stream (one at a time)
let peekStreamPane = null;  // which pane that stream is for (don't restart per render)
let peekBox = null, peekWrap = null; // current peek elements the stream paints into
let peekLive = false; // stream health — so a same-pane remount reflects real liveness
let screenOpen = false; // fullscreen overlay owns the pane's one stream; peek stands down
const bgZoom = {}; // pane_id -> {scale, tx, ty}
// "Home" = the user hasn't deliberately panned/zoomed the peek. Content updates
// (bursts, snapshots, resizes) may re-pin the scroll to the tail ONLY then —
// re-pinning a panned view yanks it back down while the user is reading.
// Epsilon compare: pinch/clamp math leaves float crumbs (scale 0.9999, tx 0.3) that
// are visually home — exact checks would permanently disable tail-follow after the
// first gesture.
const zHome = (id) => {
  const z = bgZoom[id];
  return !z || (Math.abs(z.scale - 1) < 0.01 && Math.abs(z.tx) < 2 && Math.abs(z.ty) < 2);
};

// Tuck the agent's OWN bottom chrome (input box + status rows) behind the bar, sized
// per frame: the input box's top border (╭─/┌─) is the seam — everything from it down
// is chrome, and its height varies with activity (spinner/interrupt/queue rows), so a
// fixed overlap either leaks footer or hides content. Falls back to the old fixed
// 60px when no border is found. Line height is read from the live style so the
// tuck math can't drift if .bg-term's font ever changes.
function tuckChrome(wrap, box) {
  if (wrap.classList.contains("shell")) return; // shells: the prompt IS the content
  const lines = (box.textContent || "").split("\n");
  let rows = 0;
  for (let i = lines.length - 1; i >= Math.max(0, lines.length - 12); i--)
    if (/^[╭┌]─/.test(lines[i])) { rows = lines.length - i; break; }
  const lineH = parseFloat(getComputedStyle(box).lineHeight) || 13.5;
  wrap.style.marginBottom =
    `calc(var(--bar-h, 150px) - ${rows ? Math.round(rows * lineH) + 6 : 60}px)`;
}
// One long-poll live stream (docs/design/live-view.md). Holds /live?frame=<hash> open;
// the daemon answers the moment the pane's screen differs (checked server-side every
// 250ms), or with just the hash after ~25s idle, and we immediately re-hold. Full
// COLORED frames — resize/reflow/alt-screen all reduce to "render the new frame".
// onFrame(coloredText) paints; onQuiet()/onLive() toggle the stale (decolor) look.
// Returns a stop() (AbortController) — call it when the surface goes away.
//
// LIVE vs STALE is about the CONNECTION, not screen activity: an idle pane produces
// no new frames for long stretches yet is perfectly live, so we stay colored as long
// as the server keeps ANSWERING (a "no change" reply is proof-of-life). Gray means
// the connection actually broke/hung — a fetch error, or no response at all for
// longer than one full hold (watchdog).
// One anonymous session id per page load — the summable spine for live-time /
// bandwidth telemetry (docs/design/live-telemetry.md). Not identity: a correlation key
// that groups this tab's live rounds. The invariant billing relies on is that two
// viewers NEVER share a session id, so the id must be cryptographically random —
// time+Math.random would collide across tabs opened the same instant. randomUUID is
// secure-context-only (https/localhost); getRandomValues covers the contexts it doesn't.
// Generation is TOTAL: it optional-chains crypto, swallows any throw, and yields ""
// when no CSPRNG exists — a telemetry nicety must never throw at module top level and
// kill the whole UI. Empty ⇒ un-attributable (the server treats a missing session as
// un-attributable, never bucketed), and we simply omit the param rather than send a
// shared "" / "null" that would violate the invariant.
const SESSION_ID = (() => {
  try {
    if (crypto?.randomUUID) return crypto.randomUUID();
    if (crypto?.getRandomValues) {
      const b = crypto.getRandomValues(new Uint8Array(16));
      return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    }
  } catch { /* no CSPRNG / blocked crypto ⇒ fall through to un-attributable */ }
  return "";
})();

// Ship a browser-side failure to the daemon → OTel (issue #57): mobile has no devtools,
// so a swallowed mic denial / ws close / poll catch / uncaught exception is otherwise
// invisible. Structural (kind, error name) + the free-text detail; the daemon drops the
// detail unless TMUXRC_QSDEBUG. Best-effort and NON-RECURSIVE: the fetch's own failure is
// swallowed (a dead backend must not spawn a report about the failed report), and a tight
// error loop is deduped + capped so it can't spam the daemon.
const _errSeen = new Set();      // kind|detail already reported this page-load ⇒ skip
let _errCount = 0;               // hard cap regardless of distinctness
function reportError(kind, detail) {
  try {
    const msg = detail == null ? "" : String(detail.message || detail);
    const key = kind + "|" + msg;
    if (_errSeen.has(key) || _errCount >= 50) return;
    _errSeen.add(key); _errCount++;
    // name distinguishes NotAllowedError (denied) from NotFoundError (no mic) etc.;
    // for a plain string detail it's absent. Session joins to live/parse telemetry.
    fetch("/api/client-error", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        kind,
        name: (detail && detail.name) || undefined,
        message: msg || undefined,
        endpoint: (detail && detail.endpoint) || undefined,
        session: SESSION_ID || undefined,
      }),
    }).catch(() => {}); // NEVER report a failed report — that's the recursion guard
  } catch { /* reporting must never throw into the caller's error path */ }
}
// Uncaught exceptions and rejected promises that reach the top — the catch-all for
// failures no explicit handler wrapped. Added once at module load.
window.addEventListener("error", (e) =>
  reportError("onerror", e.error || e.message));
window.addEventListener("unhandledrejection", (e) =>
  reportError("unhandledrejection", e.reason));

function liveStream(paneId, { onFrame, onLive, onQuiet }) {
  const ac = new AbortController();
  let watchdog = null;
  const alive = () => { // any server response resets the connection-health watchdog
    clearTimeout(watchdog);
    onLive && onLive();
    watchdog = setTimeout(() => onQuiet && onQuiet(), 35000); // > the 25s hold
  };
  (async () => {
    let frame = "";
    while (!ac.signal.aborted) {
      // Per-request timeout: a silently-dropped tunnel (no FIN, no error) would leave
      // `await fetch` hanging forever and the stream dead. Abort a request that outlives
      // the server's ~25s hold by a margin, so the loop re-issues and recovers.
      const t = setTimeout(() => ac2 && ac2.abort(), 40000);
      const ac2 = new AbortController();
      const onStop = () => ac2.abort();
      ac.signal.addEventListener("abort", onStop, { once: true });
      if (ac.signal.aborted) ac2.abort(); // aborted between the while-check and the
      // listener wiring above ⇒ propagate now so this fetch doesn't slip through
      try {
        // Append session ONLY when we have one — a crypto-less client stays
        // un-attributable rather than sending a shared ""/"null" that would collapse
        // distinct viewers onto one id (the server treats a missing session that way).
        const q = `frame=${encodeURIComponent(frame)}` +
          (SESSION_ID ? `&session=${encodeURIComponent(SESSION_ID)}` : "");
        const r = await fetch(
          `/api/panes/${encodeURIComponent(paneId)}/live?${q}`,
          { signal: ac2.signal });
        if (!r.ok) { onQuiet && onQuiet(); await sleep(2000); continue; } // pane gone / wedged
        const j = await r.json();
        alive(); // responded (new frame or "no change") ⇒ live
        if (j.text !== undefined) onFrame(j.text);
        frame = j.frame;
      } catch (e) {
        if (ac.signal.aborted) return; // stream stopped for good (close / pane switch)
        onQuiet && onQuiet(); // timeout or network error ⇒ stale; loop retries
        await sleep(2000);
      } finally {
        clearTimeout(t);
        ac.signal.removeEventListener("abort", onStop);
      }
    }
  })();
  return () => { ac.abort(); clearTimeout(watchdog); };
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ONE peek wrap/box for the life of the page, RETARGETED on pane switch rather than
// rebuilt per render. A pane switch is: point the transform state at the new pane,
// repaint once, re-pin.
let peekUI = null;
function buildPeek() {
  // Wrapper = the visible window (starts right below the card, ends near the bar); the
  // trimmed capture is top-anchored inside it, scrolled to its tail when longer. So a
  // 1-line shell prompt sits right under the card instead of drowning in the blank lines
  // tmux pads the capture with.
  const wrap = document.createElement("div");
  wrap.className = "bg-wrap";
  const box = document.createElement("pre");
  box.className = "bg-term";
  wrap.appendChild(box);
  // Desktop has no pan gesture — dragging a text selection auto-scrolls the window
  // sideways with nothing to bring it home (touch pans go through pinchZoom's clamp).
  // Ease scrollLeft back once the drag settles.
  let scrollIdle;
  wrap.addEventListener("scroll", () => {
    if (!wrap.scrollLeft) return;
    clearTimeout(scrollIdle);
    scrollIdle = setTimeout(() => wrap.scrollTo({ left: 0, behavior: "smooth" }), 500);
  });
  // Wired ONCE. pinchZoom returns a handle so a pane switch re-points the persisted
  // transform at bgZoom[newPane] instead of constructing another instance.
  const zoom = pinchZoom(wrap, box, null, true);
  peekRO.observe(wrap); // one wrap, one registration, for the life of the page
  return { wrap, box, zoom };
}

function bgTerm(s) {
  if (!peekUI) {
    peekUI = buildPeek();
    // The peek sits between the card and the ⤢ button: flex column, DOM order = visual order.
    _panesUI.deck.insertBefore(peekUI.wrap, _panesUI.fs);
  }
  const { wrap, box, zoom } = peekUI;
  // "shell" here means "no status chrome at the bottom of the capture" — agents get their
  // chrome tucked behind the bar, shells keep their prompt visible above it. (Don't key
  // this on LOGOS: shell has a logo too now.)
  setCls(wrap, "shell", !AGENT_TOOLS.has(s.tool));
  const switched = wrap.dataset.pane !== s.pane_id;
  setAttr(wrap, "data-pane", s.pane_id); // the ResizeObserver keys zHome off the OWNING pane
  // Re-point the pan/zoom state at THIS pane's persisted transform and apply it, so a
  // switch restores where that pane was left rather than inheriting the previous pane's.
  if (switched) zoom.retarget((bgZoom[s.pane_id] ||= { scale: 1, tx: 0, ty: 0 }));
  const toEnd = () => { wrap.scrollTop = wrap.scrollHeight; };
  // Only on a SWITCH does the box need re-seeding: same-pane polls leave the streamed
  // content alone entirely (the stream itself paints it), which is what keeps a selection
  // inside the terminal alive across a poll.
  if (switched) {
    // Instant frame on switch from cache. Its GRAY/live state must reflect the ACTUAL
    // stream health (peekLive), not a blanket "stale". A pane never viewed this session
    // has no cache — "(connecting…)" until the first frame.
    const c = peekCache[s.pane_id];
    if (c) {
      box.innerHTML = c.html;
      tuckChrome(wrap, box);
      setCls(wrap, "stale", !peekLive);
    } else {
      setText(box, "(connecting…)");
      setCls(wrap, "stale", false);
    }
    // ALWAYS pin to the tail on a switch — this is the coordinate BASELINE the persisted
    // pan/zoom transform overlays (scrollTop starts at 0; a panned user's offset is
    // relative to the tail, so skipping this would show the buffer TOP through their
    // transform). The zHome guards elsewhere apply only to CONTENT updates on the
    // already-shown pane, where moving the scroll is a yank.
    requestAnimationFrame(toEnd);
  }
  // The peek streams for the ACTIVE pane. The stream is keyed to the pane, NOT recreated
  // per render (that would restart the long-poll hold each time); its callbacks target the
  // now-permanent peekUI nodes. Restart only when the streamed pane actually changes.
  peekBox = box; peekWrap = wrap;
  // While the fullscreen overlay owns this pane's one stream, the peek stands down —
  // otherwise the poll would start a SECOND concurrent stream here. The peek resumes on
  // the next poll after the overlay closes (screenOpen back to false).
  if (screenOpen) { if (peekStop) { peekStop(); peekStop = null; peekStreamPane = null; } }
  else if (peekStreamPane !== s.pane_id) {
    peekStop && peekStop();
    peekStreamPane = s.pane_id;
    const streamPane = s.pane_id; // captured: ignore late frames after a pane switch
    peekStop = liveStream(streamPane, {
      onFrame: (txt) => {
        // LOAD-BEARING: peekUI is THE one box on screen, shared across panes, so a frame
        // that resolves just after a pane switch would paint the OLD pane's screen into
        // the NEW pane's window (and cache it there) without this guard.
        if (streamPane !== peekStreamPane) return;
        const html = renderCapture(txt.replace(/\s+$/, ""), { color: true });
        peekCache[streamPane] = { html }; // cache for the next stale-on-switch
        // NEVER repaint while the user is typing or selecting. This paint is a whole
        // innerHTML swap, which collapses the document Selection — and on a busy pane the
        // frames arrive continuously, so the caret gets wiped within milliseconds of being
        // placed. Skipping is safe: frames keep coming, so it self-corrects on the next one.
        //
        // TODO: this guard and _caretGrace are the last rendering workarounds left. Diffing
        // renderCapture's output into persistent per-line nodes would let both go.
        const sel = document.getSelection();
        const selecting = sel && !sel.isCollapsed
          && (peekBox?.contains(sel.anchorNode) || bar.input?.contains(sel.anchorNode));
        if (selecting || Date.now() - _caretGrace < 1200) return;
        if (peekBox && peekBox.innerHTML !== html) {
          peekBox.innerHTML = html;
          tuckChrome(peekWrap, peekBox);
          // Suppress the scroll-to-tail only while the user's own finger is panning this
          // surface — that is the one thing a re-pin fights.
          if (!zoom.panning() && zHome(streamPane)) peekWrap.scrollTop = peekWrap.scrollHeight;
        }
      },
      onLive: () => { if (streamPane === peekStreamPane) { peekLive = true; peekWrap && peekWrap.classList.remove("stale"); } },
      onQuiet: () => { if (streamPane === peekStreamPane) { peekLive = false; peekWrap && peekWrap.classList.add("stale"); } },
    });
  } else if (peekLive) {
    setCls(wrap, "stale", false); // stream already live for this pane
  }
}
// ONE shared observer, now for the ONE peek window (registered in buildPeek). The window's
// height changes as the card above it grows (events/images render, flex re-settles), which
// slid the view off the tail and half-clipped the last line — so re-pin on resize.
const peekRO = new ResizeObserver((entries) =>
  entries.forEach((e) => {
    // The wrap's OWN pane, not activeId() — a pending selection can diverge from the
    // rendered wrap and re-pin a pane the user has deliberately panned.
    if (zHome(e.target.dataset.pane)) e.target.scrollTop = e.target.scrollHeight;
  }));

// Display-safe text from an untrusted string (model output derived from pane content):
// strip bidi controls, then cap by CODE POINTS so no surrogate pair is split. Bidi
// controls matter because an unterminated RLO/LRO can visually reorder a row — reversing
// a link's host indicator, or making a copy row's preview read as different content than
// the clipboard carries. Use this for untrusted text set as textContent; the innerHTML
// paths (tasks/events/subagents) go through esc() instead, which escapes markup but does
// NOT strip bidi — those are plain rows where reordering is cosmetic, not deceptive.
function safeText(v, max) {
  return Array.from(
    String(v ?? "").replace(/[\u202A-\u202E\u2066-\u2069]/g, "").trim()
  ).slice(0, max).join("");
}

// Tap-to-open links the parser extracted (auth URLs, PRs, previews). The parser
// reassembles URLs that wrap across terminal lines, so these work where regexing the
// raw screen text can't. stopPropagation so tapping opens the link, not the card.
// Keyed by href, so a link that persists across polls keeps its node and stays tappable.
function applyLinks(box, links) {
  const valid = links.filter((l) => {
    if (!l || !l.href || !/^https?:\/\//i.test(l.href)) return false;
    try { new URL(l.href); return true; } catch { return false; }  // pre-cap: malformed can't eat slots
  }).slice(0, 3);
  // Index + href, for the same reason as the copyables below: the same URL can appear twice
  // with different labels, and a bare-href key collapsed both onto one cached node.
  keyedList(box, valid, (l, i) => i + "|" + l.href, () => {
    const a = document.createElement("a");
    a.className = "linkbtn";
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    // Lucide icon, not the 🔗 emoji: AGENTS.md bans emoji as UI chrome (emoji ignore
    // currentColor, so they can't theme, and they render differently per device).
    const licn = document.createElement("span");
    licn.className = "linkicon";
    licn.innerHTML = licon("link", 13);
    // The label is untrusted, so it rides in its own span as textContent, never innerHTML.
    a._txt = document.createElement("span");
    a._host = document.createElement("span");
    a._host.className = "linkhost";
    a.append(licn, a._txt, a._host);
    a.onclick = (e) => e.stopPropagation();
    return a;
  }, (a, l) => {
    // The label is MODEL OUTPUT derived from untrusted pane content — a hostile pane can
    // suggest a reassuring label for a phishing URL. Always show the destination host
    // next to the label so the user sees where the tap goes; cap the label so a hostile
    // pane can't bury the card's actionable UI under a wall of text.
    const host = new URL(l.href).host;
    setAttr(a, "href", l.href);
    setText(a._txt, safeText(l.text, 80) || host); // untrusted: bidi-stripped, capped
    setText(a._host, ` ${host}`);
  });
}

// Put text on the clipboard, resolving true/false so the caller can show the outcome
// (a silent no-op reads as "the button is broken"). navigator.clipboard needs a secure
// context: the PWA is served over HTTPS through the tunnel, but a plain-HTTP LAN visit
// (http://host:8080) has no Clipboard API at all — fall back to the legacy
// execCommand path so copy still works there.
async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through — denied permission or an insecure context */ }
  const ta = document.createElement("textarea");
  try {
    ta.value = text;
    // Off-screen but focusable, and readOnly so mobile keyboards don't pop up.
    ta.readOnly = true;
    ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length); // iOS ignores select() alone
    return document.execCommand("copy");
  } catch { return false; }
  // Remove in `finally`: if select()/setSelectionRange() throws we'd otherwise leak a
  // hidden textarea into the DOM on every failed attempt.
  finally { ta.remove(); }
}

// One-tap copy for text the parser lifted off the screen (commands to run elsewhere,
// drafted commit messages, generated tokens — see "copyables" in parser_prompt.txt).
// TWO lines per row: the model's LABEL as a heading, plus a one-line PREVIEW of the
// actual text under it. The preview is what makes the row decidable — a bare "API
// token" or "Commit message" doesn't say WHICH token or what the message reads, so the
// user had to copy-then-paste-somewhere just to look, which is the terminal problem all
// over again. Full payload still rides on the button; only the preview is clipped.
// Keyed by the payload text, so a copyable that persists across polls keeps its node —
// which is what lets the "Copied" confirmation actually last its full 1.6s.
function applyCopy(box, items) {
  const valid = items.filter((c) => c && typeof c.text === "string" && c.text.trim()).slice(0, 3);
  // Keyed by INDEX + payload, not payload alone. Two copyables can legitimately carry the
  // same text under different labels (an agent printing one command under two headings),
  // and a bare-content key made both items resolve to the SAME cached node: it was applied
  // twice with the last write winning, then re-inserted, so on the next poll the rows swapped
  // labels and one payload became unreachable. The index disambiguates while the payload
  // still prevents a row being reused for different content if the list shifts.
  keyedList(box, valid, (c, i) => i + "|" + c.text, () => {
    const b = document.createElement("button");
    b.className = "copybtn";
    // Icon is chrome (inline Lucide, themes via currentColor — AGENTS.md bans emoji
    // here). Swapped to a check when the copy lands.
    b._icon = document.createElement("span");
    b._icon.className = "copyicon";
    b._icon.innerHTML = licon("clipboard", 14);
    // The label is untrusted, so it rides in its own span as textContent, never innerHTML.
    b._label = document.createElement("span");
    b._label.className = "copylabel";
    b._prev = document.createElement("span");
    b._prev.className = "copyprev";
    const lines = document.createElement("span");
    lines.className = "copylines";
    lines.append(b._label, b._prev);
    b.append(b._icon, lines);
    b._revert = 0;
    b._confirm = ""; // non-empty while a copy confirmation is showing (see apply below)
    // Plain click, and it still carries the transient user activation that BOTH clipboard
    // paths need (navigator.clipboard.writeText and the execCommand fallback alike).
    b.onclick = async (e) => {
      e.stopPropagation(); // copying must not also re-select the pane
      const ok = await copyText(b._payload);
      // The card never shows the payload, so a failure has to send the user to where the
      // text actually is — the pane itself — not to a "text" that isn't on screen.
      b._confirm = ok ? "Copied" : "Copy failed — select it in the pane";
      b._icon.innerHTML = licon(ok ? "check" : "clipboard", 14);
      setText(b._label, b._confirm);
      setCls(b, "copied", ok);
      // Clear the pending timer first: on a rapid second tap the older one would fire
      // mid-confirmation and blank the state early.
      clearTimeout(b._revert);
      b._revert = setTimeout(() => {
        b._confirm = "";
        b._icon.innerHTML = licon("clipboard", 14);
        setText(b._label, b._labelText);
        b.classList.remove("copied");
      }, 1600);
    };
    return b;
  }, (b, c) => {
    b._payload = c.text; // verbatim for the copy — never the display-sanitized form
    // Label is MODEL OUTPUT derived from untrusted pane content: strip bidi controls (an
    // unterminated override would visually reorder the row) and cap by code points so a
    // hostile pane can't bury the card under a wall of text. Falls back to a generic name
    // when the model gave no usable label.
    b._labelText = safeText(c.label, 60) || "Text";
    // Don't stomp a live confirmation: a poll landing during the 1.6s window would
    // otherwise reset the label to its name while the check-mark is still showing.
    if (!b._confirm) setText(b._label, b._labelText);
    // Preview: the payload's own first line, so the row says what it actually holds.
    // Newlines collapse to a separator and runs of grid whitespace collapse, so a
    // multi-line payload reads as one line and terminal padding doesn't eat the preview.
    // Clipped by CSS; sliced first so a hostile payload can't cost real layout work.
    // safeText because a bidi control could make the row read as different content than
    // the clipboard actually carries.
    setText(b._prev, safeText(c.text.replace(/\s*\n+\s*/g, " · ").replace(/\s{2,}/g, " "), 200));
  });
}

// Tables are reconciled three levels deep (table, row, cell) so a table whose numbers
// tick keeps its nodes — and therefore the .tbl-scroll box keeps its scroll offset, which
// a rebuild reset to the left edge on every poll.
function applyTables(host, tables) {
  keyedList(host, tables, (t, i) => i + "|" + (t.title || ""), () => {
    const box = document.createElement("div");
    box.className = "tablewrap";
    box._title = document.createElement("div");
    box._title.className = "tbl-title";
    const scroll = document.createElement("div");
    scroll.className = "tbl-scroll";
    const table = document.createElement("table");
    box._thead = document.createElement("thead");
    box._headRow = document.createElement("tr");
    box._thead.appendChild(box._headRow);
    box._tbody = document.createElement("tbody");
    table.append(box._thead, box._tbody);
    scroll.appendChild(table);
    box.append(box._title, scroll);
    return box;
  }, (box, t) => {
    setText(box._title, t.title || "");
    const headers = t.headers || [];
    setCls(box._thead, "hid", !headers.length);
    keyedList(box._headRow, headers, (h, i) => i, () => document.createElement("th"), setText);
    keyedList(box._tbody, t.rows || [], (r, i) => i, () => document.createElement("tr"),
      (tr, r) => keyedList(tr, r || [], (c, i) => i, () => document.createElement("td"), setText));
  });
}

// The activity feed: "what the thing did". Each event's `text` is the primary line;
// optional metadata (a file diff, or a `meta` string) renders as a small, muted,
// right-justified side-note. A file edit is just an event whose metadata is a diff.
//
// APPEND-ONLY: keyedList keeps the existing event rows, so scroll position and each
// <details> open/closed state are never lost and need no side table to restore them.
function buildEvent() {
  const d = document.createElement("div");
  d.className = "ev";
  d._text = document.createElement("span");
  d._text.className = "ev-text";
  d._note = document.createElement("span");
  d._note.className = "ev-note";
  d._add = document.createElement("span");
  d._add.className = "add";
  d._del = document.createElement("span");
  d._del.className = "del";
  d._path = document.createElement("span");
  d._note.append(d._path, d._add, d._del);
  d.append(d._text, d._note);
  return d;
}

function applyEvent(d, e) {
  // historical = reconstructed from scrollback by the bootstrap pass, not observed live —
  // rendered dimmer so it never masquerades as watched fact.
  setCls(d, "ev-hist", !!e.historical);
  // linkifyText, not setText: an event that mentions a URL or a markdown [label](url) — a
  // PR the agent opened, a preview deploy — should be tappable here exactly as it is in
  // the terminal below. It escapes everything it doesn't turn into an anchor, so it is
  // safe on this untrusted model text; setHtml keeps the unchanged-write no-op.
  setHtml(d._text, e.text ? linkifyText(e.text) : "");
  const isFile = !!e.file;
  setCls(d._note, "ev-file", isFile);
  setText(d._path, isFile ? (e.file.path || "") + " " : (e.meta || ""));
  setText(d._add, isFile && e.file.added ? "+" + e.file.added : "");
  setText(d._del, isFile && e.file.removed ? "-" + e.file.removed : "");
  // Hide the note column outright when this event has no metadata. The span is permanent
  // and holds three permanent children, so :empty can never match it — and .ev is a flex
  // row with a gap, which an always-present empty column would silently widen.
  setCls(d._note, "hid", !(isFile || e.meta));
}

// A stable identity per event. The log is append-only and time-ordered server-side, so
// index-within-log is stable for everything already rendered; ts+text keeps a row from
// being reused for a different event if the server ever trims the head.
//
// The index must be the event's position in the WHOLE log, not in the slice it was passed
// in. The feed splits into two keyedLists at summary.count, and that boundary moves as the
// server folds more of a burst — a slice-local index would re-key every row below the
// fold on each move and rebuild the entire tail, which is the churn this file exists to
// avoid. Hence the explicit offset rather than keyedList's own index argument.
const evKeyAt = (offset) => (e, i) => `${offset + i}|${e.ts || ""}|${e.text || ""}`;

function buildEvents() {
  const root = document.createElement("div");
  root.className = "events";
  const det = document.createElement("details");
  det.className = "ev-summary";
  const sm = document.createElement("summary");
  const smText = document.createElement("span");
  const smCount = document.createElement("span");
  smCount.className = "dim";
  sm.append(document.createTextNode("▤ "), smText, document.createTextNode(" "), smCount);
  det.appendChild(sm);
  const folded = document.createElement("div");
  folded.className = "ev-folded";
  det.appendChild(folded);
  const rest = document.createElement("div");
  rest.className = "ev-rest";
  root.append(det, rest);
  // PER-PANE expansion state. The <details> is now ONE permanent node shared by every pane
  // (the card is retargeted, not rebuilt), so its `open` flag is not per-pane by itself —
  // expanding pane A's folded burst would leave pane B's summary rendering already-open,
  // showing a different pane's collapsed history as expanded. The old code got this from a
  // module-level Set keyed `paneId + ":sum"`; the state still has to be keyed by pane, it
  // just lives on this handle now instead of in a side table.
  const ui = { root, det, smText, smCount, folded, rest, openByPane: {} };
  det.ontoggle = () => { if (ui.pane) ui.openByPane[ui.pane] = det.open; };
  // Measure BEFORE the append, not after: whether the user was at the bottom is a fact
  // about the pre-append scroll state, and reading it after new rows land always says
  // "not at bottom". Recorded on scroll, and re-derived just before each apply.
  root.addEventListener("scroll", () => { ui.atBottom = atBottomOf(root); });
  return ui;
}

// Within 24px of the bottom counts as "at the bottom" — a user parked at the tail should
// keep following new activity, while someone who scrolled up to read history is left alone.
const atBottomOf = (el) => el.scrollHeight - el.scrollTop - el.clientHeight < 24;

function applyEvents(ui, events, paneId, summary) {
  const root = ui.root;
  setCls(root, "hid", !events.length);
  setAttr(root, "data-pane", paneId || "");
  // Pane switch: the new pane's feed is a different log, so follow ITS tail rather than
  // inheriting the previous pane's scroll position.
  const switched = ui.pane !== paneId;
  ui.pane = paneId;
  // Restore THIS pane's expansion (see openByPane in buildEvents). Assigned only on a
  // switch: writing it every poll would slam the burst shut under a user who just opened
  // it, since ontoggle records asynchronously relative to the poll.
  if (switched) ui.det.open = !!ui.openByPane[paneId];
  // Pre-measure. Do it here, before any row is inserted, so "was the user at the bottom"
  // reflects the state the user actually left the feed in.
  const stick = switched || ui.atBottom === undefined || ui.atBottom || atBottomOf(root);
  // When the pane went idle and the server summarized a burst of `count` events, collapse
  // the OLDEST `count` events under a summary line (expandable). We fold by count, not
  // timestamp, to avoid client-ms vs server-sec clock skew — the log is time-ordered so
  // the oldest N are the summarized burst.
  const folding = !!(summary && summary.text && summary.count > 1 && events.length > summary.count);
  setCls(ui.det, "hid", !folding);
  if (folding) {
    setText(ui.smText, summary.text);
    setText(ui.smCount, `(${summary.count})`);
  }
  const cut = folding ? summary.count : 0;
  keyedList(ui.folded, folding ? events.slice(0, cut) : [], evKeyAt(0), buildEvent, applyEvent);
  keyedList(ui.rest, folding ? events.slice(cut) : events, evKeyAt(cut), buildEvent, applyEvent);
  if (stick) {
    // Defer one frame so the just-inserted rows are laid out and scrollHeight is real.
    requestAnimationFrame(() => { root.scrollTop = root.scrollHeight; ui.atBottom = true; });
  }
}

// Task/TODO checklist the agent is tracking (done vs open) — from parser JSON tasks[].
// One box shape serves both this and the sub-agents list below (same .tasks chrome), so
// the heading text and the extra class are the only parameters.
function buildTasks(head, cls) {
  const root = document.createElement("div");
  root.className = cls;
  const h = document.createElement("div");
  h.className = "tasks-head";
  h.textContent = head;
  const list = document.createElement("div");
  list.className = "task-list";
  root.append(h, list);
  return { root, list };
}

function buildTask() {
  const d = document.createElement("div");
  d.className = "task";
  d._tick = document.createElement("span");
  d._tick.className = "tick";
  // A sub-agent's tick is either a check or a pulse dot; both are permanent nodes and the
  // class decides which shows, so a worker finishing doesn't rebuild the row.
  d._tickText = document.createElement("span");
  d._pulse = document.createElement("span");
  d._pulse.className = "pulse";
  d._tick.append(d._tickText, d._pulse);
  d._label = document.createElement("span");
  d._label.className = "sa-label";
  d._meter = document.createElement("span");
  d._meter.className = "worksub";
  d.append(d._tick, d._label, d._meter);
  return d;
}

function applyTasks(ui, tasks) {
  setCls(ui.root, "hid", !tasks.length);
  keyedList(ui.list, tasks, (t, i) => i + "|" + (t.text || ""), buildTask, (d, t) => {
    setCls(d, "done", !!t.done);
    setCls(d._pulse, "on", false); // plain tasks never pulse
    setText(d._tickText, t.done ? "✓" : "○");
    setText(d._label, t.text || "");
    setText(d._meter, "");
  });
}

// Background sub-agents this agent spawned (parser JSON subagents[]) — distinct from the
// agent's own TODO tasks above. Shares the .tasks box chrome; a running worker gets a
// pulse dot, a finished one a check, and any elapsed/tokens meter rides on the right.
// Renderable entries only (match classify.py: dicts only) — ONE definition, shared by
// the renderer and by the row's toggle count so the label can never disagree with the box.
const realSubs = (subs) =>
  (Array.isArray(subs) ? subs : []).filter((a) => a && typeof a === "object" && !Array.isArray(a));

function applySubagents(ui, subs) {
  setCls(ui.root, "hid", !subs.length);
  keyedList(ui.list, subs, (a, i) => i + "|" + (a.label || ""), buildTask, (d, a) => {
    const done = a.state === "done";
    setCls(d, "done", done);
    setText(d._tickText, done ? "✓" : "");
    setCls(d._pulse, "on", !done);
    setText(d._label, a.label || "");
    setText(d._meter, [a.elapsed, a.tokens && "↓" + a.tokens].filter(Boolean).join(" "));
  });
}

// Render Claude Code's Esc-Esc Rewind picker as a scrollable history list. The ❯
// entry is highlighted; ↑/↓ (the key buttons, which move the real terminal cursor)
// scroll the selection and the card reflects it each poll; Enter restores. A tap on a
// row is a convenience: send ↑ to reveal more when it's the top "N more above" marker.
// Colorize a Rewind note's diff stats: "+58" green, "-3" red; other text dim. Builds
// real nodes rather than a markup string, so the note is never re-parsed as HTML.
function applyDiffStat(host, note) {
  // Split on the signed-number runs, keeping them: even indices are plain text, odd are
  // the stats. The shape is stable for a given note, so keyedList reuses the spans.
  const parts = String(note ?? "").split(/([+-]\d+)/).filter((p) => p !== "");
  keyedList(host, parts, (p, i) => i, () => document.createElement("span"), (sp, p) => {
    const stat = /^[+-]\d+$/.test(p);
    setAttr(sp, "class", stat ? (p[0] === "+" ? "add" : "del") : "");
    setText(sp, p);
  });
}

// Claude Code's Esc-Esc Rewind picker as a scrollable history list. The ❯ entry is
// highlighted; ↑/↓ (the key buttons, which move the real terminal cursor) scroll the
// selection and the card reflects it each poll; Enter restores.
function buildRewind() {
  const root = document.createElement("div");
  root.className = "rewind";
  const head = document.createElement("div");
  head.className = "rw-head";
  head.appendChild(document.createTextNode("⟲ Rewind — restore to a previous point"));
  const more = document.createElement("span");
  more.className = "rw-more";
  head.appendChild(more);
  const list = document.createElement("div");
  list.className = "rw-list";
  const hint = document.createElement("div");
  hint.className = "rw-hint";
  hint.textContent = "Use ↑ / ↓ to move, Enter to restore, Esc to cancel";
  root.append(head, list, hint);
  return { root, more, list };
}

function applyRewind(ui, s) {
  const rw = s && s.rewind;
  setCls(ui.root, "hid", !rw);
  if (!rw) { keyedList(ui.list, [], (x) => x, () => null); return; }
  setText(ui.more, rw.more_above ? `↑ ${rw.more_above} more above` : "");
  keyedList(ui.list, rw.entries || [], (e, i) => i + "|" + (e.text || ""), () => {
    const d = document.createElement("div");
    d.className = "rw-entry";
    d._cur = document.createElement("span");
    d._cur.className = "rw-cursor";
    d._txt = document.createElement("span");
    d._txt.className = "rw-text";
    d._note = document.createElement("span");
    d._note.className = "rw-note";
    d.append(d._cur, d._txt, d._note);
    return d;
  }, (d, e) => {
    setCls(d, "sel", !!e.selected);
    setText(d._cur, e.selected ? "❯" : "");
    setText(d._txt, e.text);
    applyDiffStat(d._note, e.note || "");
  });
}

// Compact metadata chips: model, context bar, cost, mode badge, agent count. Shown in
// the bottom bar (below the input) for the ACTIVE pane only, not on every card. Only
// renders the chips that have values, so a plain shell shows nothing here.
// Returns chip DESCRIPTORS, not markup — applyChips builds/updates the nodes, keyed so a
// chip whose value changes keeps its node. Values go in as text, never markup.
const MODE_LABEL = { plan: "plan", "accept-edits": "accept edits", bypass: "bypass perms" };
function metaChips(s) {
  const chips = [];
  if (s.model) chips.push({ key: "model", text: s.model });
  if (s.context_pct != null)
    chips.push({ key: "ctx", cls: "ctxchip", pct: s.context_pct, text: `${s.context_pct}% ctx` });
  if (s.cost) chips.push({ key: "cost", text: s.cost });
  // Generic status-line entries the parser surfaced (usage-limit %, queue depth, …): one
  // chip each, no schema change per metric. LLM output — a non-array (e.g. a bare string)
  // would otherwise .slice() into characters.
  const entries = Array.isArray(s.status_entries) ? s.status_entries : [];
  entries.slice(0, 4).forEach((t, i) => {
    if (t && String(t).trim()) chips.push({ key: "st" + i, text: String(t) });
  });
  // `mode` is parser (LLM) output interpolated into a CLASS name. setAttr writes a DOM
  // property rather than markup so it cannot inject, but a junk value would still produce a
  // garbage class — so only known modes get the mode-* class, matching how ACTIVITIES
  // whitelists activity. An unknown mode still shows its label, just unstyled.
  if (s.mode && s.mode !== "normal" && s.mode !== "unknown")
    chips.push({
      key: "mode",
      cls: "mode" + (has(MODE_LABEL, s.mode) ? " mode-" + s.mode : ""),
      text: MODE_LABEL[s.mode] ?? String(s.mode),
    });
  if (s.agents > 0) chips.push({ key: "agents", cls: "agents", text: `⛓ ${s.agents} agents` });
  return chips;
}

// Always-available raw input: type any text into the pane, plus special keys. This
// is the escape hatch for anything the classifier didn't turn into a button.
// The single persistent bottom input bar. It's a static element in the HTML, wired
// ONCE here; it always targets the active pane. Being persistent (never re-rendered)
// means your typed text survives poll re-renders. All input/keys/images route to
// activePane via activeState().
const bar = {
  input: document.getElementById("bar-input"),
  send: document.getElementById("bar-send"),
  attach: document.getElementById("bar-attach"),
  file: document.getElementById("bar-file"),
  meta: document.getElementById("bar-meta"),
  keys: document.getElementById("bar-keys"),
  keysToggle: document.getElementById("bar-keys-toggle"),
};
function activeState() {
  const id = activeId();
  return panesById[id] || { pane_id: id, label: "" };
}
// Non-blocking failure notice pinned in the input bar. Lives OUTSIDE the card that
// render() replaces, so it survives re-renders (the reason the mic/send paths reached for
// alert() in the first place — see #101) without freezing the page like a modal does.
// Auto-clears on the next successful action or after a while; tapping it dismisses.
let _noteTimer = 0;
function barNote(msg) {
  const host = document.getElementById("bar");
  if (!host) return;
  let el = document.getElementById("bar-note");
  if (!el) {
    // A real <button>, not a clickable div: this is the app's failure channel, so the way
    // to dismiss it has to be reachable without a pointer — a div with onclick is neither
    // focusable nor announced as actionable. type=button so it can never submit anything.
    el = document.createElement("button");
    el.type = "button";
    el.id = "bar-note";
    el.className = "bar-note";
    // The message appears without any user action, so it also has to be SPOKEN.
    // aria-live=assertive rather than role=alert: role would REPLACE the button role and
    // the "tap to dismiss" affordance would stop being announced, whereas a live region on
    // the button keeps both — it reads on insertion and on every later text change, and it
    // still presents as a button. Missing "Not sent" means believing the message went
    // through, which is the worst outcome this notice has to prevent.
    // No aria-label: it would REPLACE the message as the accessible name, so the failure
    // itself would go unread. The message text is the name; the button role conveys that
    // activating it does something, and dismissal is not the part the user must not miss.
    el.setAttribute("aria-live", "assertive");
    el.onclick = () => el.remove();
    host.prepend(el); // first child of #bar: above the keys row and the composer
  }
  el.textContent = msg; // untrusted-ish (error text): textContent, never innerHTML
  clearTimeout(_noteTimer);
  _noteTimer = setTimeout(() => el.remove(), 12000);
}

function updateBar(s) {
  // The CSS :empty::before placeholder isn't an accessible name, so mirror it into
  // aria-label — screen readers announce the per-pane target instead of a bare textbox.
  const label = s ? `Type into ${s.label || "pane"}…` : "No pane";
  setAttr(bar.input, "data-placeholder", label);
  setAttr(bar.input, "aria-label", label);
  // Keyed, not innerHTML: the chips are re-derived every poll, and the context-percent chip
  // in particular has a growing inner bar whose width would otherwise restart from a fresh
  // node each time. .metarow:empty still works — keyedList removes leftovers.
  applyChips(bar.meta, s ? metaChips(s) : []);
}

// One chip. `bar` is the context-percent chip's inner fill, present on every chip node but
// only sized (and shown) for that one, so no chip type needs its own build function.
function applyChips(host, chips) {
  keyedList(host, chips, (c) => c.key, () => {
    const sp = document.createElement("span");
    sp._bar = document.createElement("i");
    sp._txt = document.createElement("span");
    sp.append(sp._bar, sp._txt);
    return sp;
  }, (sp, c) => {
    setAttr(sp, "class", "chip" + (c.cls ? " " + c.cls : ""));
    setCls(sp._bar, "on", c.pct != null);
    if (c.pct != null) sp._bar.style.width = c.pct + "%";
    setAttr(sp, "title", c.title || null);
    setText(sp._txt, c.text);
  });
}
// Stamped when the user is placing the caret in the composer. The live peek repaint
// (an innerHTML swap) collapses the Selection, so on a busy pane it wiped the caret as
// fast as you could tap; the repaint yields for this window instead. Time-bounded so a
// focused composer can never freeze the terminal stream.
let _caretGrace = 0;

if (bar.input) {
  // Send/Enter submits the composer: typed text and/or a staged image go to the pane
  // together, then one Enter (see submitComposer). Nothing reaches the pane at attach
  // time — the image waits here as a draft until you send, mirroring the phone's own
  // "type a caption, then send the photo" feel.
  // Send commits on pointerup, NOT plain click, and this is not about rebuilds: the
  // browser withholds `click` entirely if the finger drifts a few pixels between
  // touchstart and touchend — easy on a phone, and on a soft keyboard that shifts under
  // your thumb. A withheld click is a Send that silently does nothing.
  //
  // pointerup rather than pointerdown: it still fires regardless of drift, but unlike a
  // touch-down commit it cannot fire for a press the user slides away from and abandons.
  // submitComposer is re-entrancy-guarded by `sending`, so a stray double is harmless.
  let sendDown = 0; // the captured pointerId, or 0 when no press is in flight
  let sentAt = 0; // when pointerup last submitted, so the follow-up click can be ignored
  bar.send.addEventListener("pointerdown", (e) => {
    if (!(e.button == null || e.button === 0)) return; // left/touch only
    sendDown = e.pointerId || 1;
    // CAPTURE the pointer so this button receives the release even when the finger drifts
    // off it. Without capture, pointerup fires on whatever element the finger ended over,
    // so a press abandoned off-button left `sendDown` armed and the NEXT release on the
    // button sent unintentionally.
    try { bar.send.setPointerCapture(e.pointerId); } catch { /* capture is best-effort */ }
  });
  const endSend = (e, submit) => {
    if (!sendDown || (e.pointerId || 1) !== sendDown) return;
    sendDown = 0;
    // Release unconditionally, before any submit can throw — a conditional release leaks
    // the flag and wedges Send for the rest of the session.
    try { bar.send.releasePointerCapture(e.pointerId); } catch { /* already released */ }
    if (!submit) return;
    sentAt = Date.now();
    submitComposer(activeState());
  };
  // With capture the release always arrives here, so decide by COORDINATES: a release
  // near the button sends, one the user deliberately slid away does not. The 24px slop is
  // deliberately generous — dropping a real Send is far worse than an extra one, and
  // submitComposer is guarded by `sending` anyway.
  const SEND_SLOP = 24;
  bar.send.addEventListener("pointerup", (e) => {
    const r = bar.send.getBoundingClientRect();
    const near = e.clientX >= r.left - SEND_SLOP && e.clientX <= r.right + SEND_SLOP &&
                 e.clientY >= r.top - SEND_SLOP && e.clientY <= r.bottom + SEND_SLOP;
    endSend(e, near);
  });
  bar.send.addEventListener("pointercancel", (e) => endSend(e, false));
  // Keyboard/AT activation synthesizes a click with NO pointer events, so it needs its own
  // path. A real tap also produces a click right after our pointerup, so ignore one that
  // follows a submit we just did. Time-bounded rather than a latched flag: the click is not
  // guaranteed to arrive (so a flag could stay armed and eat the next keyboard activation),
  // but when it does arrive it is always in the same instant as the release.
  bar.send.addEventListener("click", () => {
    if (Date.now() - sentAt < 700) return;
    submitComposer(activeState());
  });
  // Any attempt to put the caret in the composer opens the repaint-grace window.
  ["pointerdown", "focus", "click"].forEach((ev) =>
    bar.input.addEventListener(ev, () => { _caretGrace = Date.now(); }, true));
  // Enter sends. We drive the send off `beforeinput`/insertParagraph rather than a
  // keydown "Enter" check because Android's soft keyboard (and IME composition) fires
  // keydown with keyCode 229 and NO usable `key` — so a keydown-only handler silently
  // fails to send on Android. beforeinput reports a semantic inputType on every
  // platform. preventDefault stops the browser inserting its own <div>/<br> paragraph.
  bar.input.addEventListener("beforeinput", (e) => {
    if (e.inputType === "insertParagraph") { e.preventDefault(); submitComposer(activeState()); }
    // insertLineBreak = Shift+Enter (desktop) → let the browser insert the newline.
  });
  // Any edit may have deleted a chip (Backspace/Delete/range-delete) — reconcile the
  // blob URLs so removed images don't leak (tap-remove revokes directly; this covers
  // keyboard deletion, which bypasses the chip's own onclick).
  bar.input.addEventListener("input", sweepChipUrls);
  // Desktop belt-and-suspenders: some browsers don't emit beforeinput for Enter in an
  // empty field. Shift+Enter falls through to the browser's newline insertion.
  bar.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitComposer(activeState()); }
  });
  // ⌨ toggles the special-keys row (hidden by default to save a row of height).
  bar.keysToggle.onclick = () => {
    bar.keys.hidden = !bar.keys.hidden;
    bar.keysToggle.setAttribute("aria-pressed", String(!bar.keys.hidden));
  };
  // Opening the file picker blurs the composer and drops the caret, so by onchange
  // insertNodeAtCaret would append at the end (the #70 misplacement). Snapshot the
  // caret here — insertNodeAtCaret restores it when the live selection is gone.
  bar.attach.onclick = () => { saveCaret(); bar.file.click(); };
  bar.file.onchange = () => {
    if (bar.file.files[0]) insertImage(bar.file.files[0]);
    bar.file.value = ""; // else picking the SAME photo again never fires change
  };
  // Paste into the composer. Two jobs: (1) an image on the clipboard becomes an inline
  // chip at the caret; (2) any OTHER paste is forced to PLAIN TEXT — the browser's
  // default contenteditable paste injects rich HTML (fonts, spans, even <img> from a
  // copied web page), which would pollute our DOM walk. We always preventDefault and
  // insert text ourselves so only text nodes + our own chips ever live in here.
  bar.input.onpaste = (e) => {
    e.preventDefault();
    const cd = e.clipboardData;
    const item = [...(cd?.items || [])].find((i) => i.type.startsWith("image/"));
    if (item) { insertImage(item.getAsFile()); return; }
    insertTextAtCaret(cd ? cd.getData("text/plain") : "");
  };
  // Drag/drop bypasses onpaste and would drop rich HTML / foreign <img src> straight
  // into the contenteditable (polluting the DOM walk, maybe fetching a remote image).
  // Block it — attaching is the 📎 button's job, not drop.
  bar.input.ondragover = bar.input.ondrop = (e) => e.preventDefault();
  // Special keys → the active pane (tmux key-names, sent literally).
  document.querySelectorAll("#bar .keys button").forEach((b) => {
    b.onclick = () => sendRaw(activeState(), b.dataset.k);
  });
}

// Submit the composer to the pane in ONE ordered burst, preserving INLINE position:
// walk the contenteditable's nodes in DOM order into segments (text runs and image
// files), then deliver each in that exact order — text run → /send (no Enter), image →
// /image (no Enter) — and finish with one Enter. So "err <img> fix <img>" lands in the
// agent's prompt with the images exactly where they sat between the words, like Claude
// Code's own composer. The DOM is the source of truth; there's no separate staged[].
// Empty composer is a no-op (a bare Enter would submit whatever's already in the agent).
let sending = false; // a send is IN FLIGHT. The ONLY guard flag left (see submitComposer)

const _sendQueue = []; // taps that arrived while a send was in flight (never dropped)
// One Enter can fire submitComposer twice (keydown AND beforeinput — see the guard below).
// Re-entry inside this window is that echo, not a second message.
const SUBMIT_DEDUPE_MS = 250;
let _lastSubmitAt = 0;

async function submitComposer(s, presetSegs) {
  // `sending` means ONE thing: a POST is in flight. It never doubled as a render freeze —
  // the old shared `busy` flag did, and guarding Send on it made Send silently do nothing
  // whenever a swipe or pinch happened to hold it ("sometimes the send button just does
  // nothing"). There is no freeze flag at all now; a poll render only writes text onto the
  // nodes already on screen, so nothing needs freezing. `sending` also does NOT serve as the
  // double-fire guard — the dedupe window below does, because this function now QUEUES
  // instead of returning, and a queued echo is a message sent twice.
  //
  // Deliberately NOT blocked when the target pane is busy working: agents queue typed
  // input, so sending mid-run is valid and useful — the user's message lands in the
  // agent's queue rather than being dropped.
  // NOTHING may silently swallow a Send. The old `if (sending) return` dropped the tap
  // with no trace whenever a previous send was still in flight — and since an unbounded
  // fetch could hang indefinitely, "in flight" could mean forever. Now a tap arriving
  // mid-send SNAPSHOTS the composer, clears it, and queues the text to go out as soon as
  // the current send finishes. The user's words are never lost and never require a retry.
  const segs = presetSegs || composerSegments();
  if (!segs.length) return;
  if (sending) {
    // The double-fire this used to absorb is now a DUPLICATE, not a no-op. One Enter can
    // reach here twice: keydown fires first and preventDefault normally suppresses
    // beforeinput/insertParagraph, but not on every engine — which is exactly why both
    // handlers exist (Android's soft keyboard gives keydown no usable `key`, so
    // beforeinput is the only reliable signal there). When `sending` merely returned, the
    // second call vanished harmlessly. Queuing it instead would send the message twice.
    //
    // So: a re-entry within one input event's worth of time (and not a queue drain, which
    // passes presetSegs) is the same keystroke arriving twice, and is dropped. A genuine
    // second tap can't beat a human reaction time to it. Anything later is a real new
    // message and still gets queued rather than dropped.
    if (!presetSegs && Date.now() - _lastSubmitAt < SUBMIT_DEDUPE_MS) return;
    _sendQueue.push({ s, segs });
    clearComposer();          // the message is committed — it must leave the box
    barNote("Queued — sending the previous message first.");
    return;
  }
  // Stamp only USER-initiated submits. A queue drain passes presetSegs, and stamping it
  // would open a 250ms window right after the drain in which a real Enter looks like an
  // echo of it and gets dropped — losing a message, which is the one outcome this whole
  // path exists to prevent. An echo only ever follows the keystroke that caused it.
  if (!presetSegs) _lastSubmitAt = Date.now();
  sending = true;
  // Immediate feedback: the Send button spins for the whole request — on a slow
  // connection (image uploads) this runs for seconds, and silence reads as hung.
  bar.send?.classList.add("sending");
  // Do NOT disable the button: `sending` already blocks re-entry, and a disabled button
  // is stranded unclickable if anything interrupts us before `finally` runs (a
  // backgrounded tab killing the in-flight fetch, a navigation). The spinner class is
  // the feedback; the guard is the correctness.
  try {
    // If any image fails to deliver, DON'T press Enter and DON'T clear the composer —
    // submitting now would send the surrounding text without its image and drop the
    // file. Everything stays in place so the user can retry. Ordering matters: any text
    // typed into the pane before the failing image is already there, but without the
    // final Enter it isn't submitted. uploadStagedImage throws on a bad response.
    for (const seg of segs) {
      if (seg.text != null) await postSend(s, { keys: seg.text, enter: false, literal: true });
      else await uploadStagedImage(s, seg.file);
    }
    await postSend(s, { keys: "Enter", enter: false, literal: false });
    clearComposer();
    // No burst needed: the visible raw surface streams via liveStream, so the sent
    // text/images show up in the next live frame on their own (docs/design/live-view.md).
  } catch (e) {
    // NOT alert(): a native modal is synchronous, so it halts the poll loop and the live
    // stream until dismissed — the app looks frozen, and on iOS the browser starts
    // offering "Suppress dialogs", after which this message vanishes silently forever
    // (see #101). An inline banner conveys the same thing without stopping the world.
    // The composer is deliberately NOT cleared, so the message is never lost.
    barNote(`Not sent — ${e.message}. Your text is still here; tap Send to retry.`);
    reportError("send", e); // so the failure rate is queryable, not just visible once
  } finally {
    // Release `sending` HERE, not on a timer: it's the re-entry guard, so a thrown
    // upload must not leave Send permanently dead (the failure path above tells the
    // user to retry — it has to actually be retryable).
    sending = false;
    // Drain: a tap that arrived mid-flight queued its text rather than being dropped, so
    // send it now. Deferred a tick so `sending` is observably clear first (and so a long
    // queue can't recurse into a deep stack).
    if (_sendQueue.length) {
      const next = _sendQueue.shift();
      setTimeout(() => submitComposer(next.s, next.segs), 0);
    }
    // Button un-spins NOW (the work is done) — no need to hold it a beat longer, since a
    // poll repainting mid-settle only writes text onto the same nodes.
    bar.send?.classList.remove("sending");
    bar.send && (bar.send.disabled = false); // clear any legacy disabled state
  }
}


// Walk the composer's children in DOM order into ordered send segments. A text node (or
// a <br>/<div> the browser inserts on Shift+Enter) contributes to a text run; an
// .attach-chip <img> flushes the pending run and emits its File. Adjacent text is
// coalesced so we don't fire a /send per keystroke-node. Returns [] when empty.
function composerSegments() {
  const segs = [];
  let run = "";
  const flush = () => { if (run) { segs.push({ text: run }); run = ""; } };
  // Serialize the contenteditable to text runs + inline image files. Newlines are the
  // tricky part (browsers model line breaks two different ways):
  //   • a <br> is a hard line break;
  //   • a <div> is a block browsers use to wrap each line after the first (Shift+Enter,
  //     pasted multiline) — entering a non-first block IS a line break.
  // A <div><br></div> (an empty line) would otherwise double-count, so a <br> that is the
  // LAST child of a NESTED div is treated as that block's line-filler and ignored — the
  // div boundary already counts the line. A trailing <br> directly under the composer
  // root is NOT filler: it's a real Shift+Enter newline (Firefox serializes an end-of-
  // text newline this way), so it must be kept. Breaks buffer in `pending`, realized
  // before the next content AND once at the end (a trailing newline the user typed).
  // Verified by hand against the real Chrome/Firefox DOM shapes for each of these cases
  // (plain text, Shift+Enter, bare <br>, blank lines, trailing filler, inline chips).
  let started = false; // any content emitted yet? (suppresses a leading newline)
  let pending = 0;     // line breaks requested but not yet written to `run`
  const content = () => { run += "\n".repeat(pending); pending = 0; started = true; };
  const walk = (node) => {
    const kids = node.childNodes;
    for (let i = 0; i < kids.length; i++) {
      const n = kids[i];
      if (n.nodeType === Node.TEXT_NODE) { if (n.nodeValue) { content(); run += n.nodeValue; } }
      else if (n.nodeName === "IMG" && n.classList.contains("attach-chip")) {
        const file = chipFiles.get(n);
        if (file) { content(); flush(); segs.push({ file }); } // no File ⇒ stray img: skip
      }
      else if (n.nodeName === "BR") {
        const nestedFiller = node !== bar.input && node.nodeName === "DIV" && i === kids.length - 1;
        if (started && !nestedFiller) pending++;
      }
      else if (n.nodeName === "DIV") { if (started) pending++; walk(n); }
      else walk(n); // spans etc. (shouldn't occur — paste is plain-text) — recurse for text
    }
  };
  walk(bar.input);
  run += "\n".repeat(pending); // realize a trailing newline (Shift+Enter at the very end)
  flush();
  return segs;
}

// POST one staged image to the pane (server stages it to disk and pastes/types it in,
// no Enter — submitComposer sends the single Enter). Kept separate from send() because
// it's a multipart body, not the JSON /send shape. Throws on a bad response so
// submitComposer aborts before the final Enter (see its catch).
async function uploadStagedImage(s, file) {
  const fd = new FormData();
  fd.append("file", file);
  // Bounded like postSend, but with room for a real upload on a phone connection.
  const r = await fetch(`/api/panes/${encodeURIComponent(s.pane_id)}/image`, {
    method: "POST", body: fd, signal: timeoutSignal(45000),
  });
  if (!r.ok) throw new Error("upload failed: " + r.status);
}

// The composer's contenteditable DOM IS the buffer: typed text and pasted/attached image
// chips accumulate here INLINE until Send/Enter walks it and flushes (see submitComposer).
// There's no separate array — position in the DOM is send position. Each chip's File is
// kept in a WeakMap keyed by the chip node (a File can't ride on an attribute), so a
// removed node is garbage-collected out of the map for free.
const chipFiles = new WeakMap(); // chip <img> node -> File
// Live object URLs, so a chip removed by ANY means (tap, Backspace, range-delete) gets
// its blob revoked. A WeakMap can't be enumerated to find orphans, hence this Set;
// sweepChipUrls() reconciles it against the chips still in the DOM (see the input hook).
const chipUrls = new Set();
function sweepChipUrls() {
  const live = new Set([...bar.input.querySelectorAll(".attach-chip")].map((c) => c.src));
  for (const url of chipUrls) if (!live.has(url)) { URL.revokeObjectURL(url); chipUrls.delete(url); }
}

// Whether the composer holds anything to send (text or an image chip). Used for the
// empty-check (no-op send) and the auto-update draft guard.
function composerEmpty() { return composerSegments().length === 0; }

// Insert an image chip at the current caret, interleaved with text. contenteditable=false
// makes the caret treat the chip as one atomic character (arrow keys / Backspace step
// over it, not into it). draggable=false so a long-press can't drag it out on touch. The
// chip is a focusable role=button so keyboard/AT users can remove it (Enter/Space), not
// just pointer tap or Backspace.
function insertImage(file) {
  if (!file) return;
  const chip = document.createElement("img");
  chip.className = "attach-chip";
  chip.contentEditable = "false";
  chip.draggable = false;
  chip.alt = "image in the composer";
  chip.setAttribute("role", "button");
  chip.tabIndex = 0;
  chip.setAttribute("aria-label", "Attached image — activate to remove");
  chip.title = "Sends with your next Send/Enter, in this position. Tap to remove.";
  chip.src = URL.createObjectURL(file);
  chipUrls.add(chip.src);
  const remove = () => {
    const wasFocused = document.activeElement === chip;
    chip.remove(); chipFiles.delete(chip); sweepChipUrls();
    if (wasFocused) bar.input.focus(); // don't strand keyboard/AT focus on <body>
  };
  chip.onclick = remove;
  chip.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); remove(); } };
  chipFiles.set(chip, file);
  insertNodeAtCaret(chip);
}

// Insert a node (chip or a text node) at the caret, keeping the caret AFTER it so the
// next keystroke/paste continues past the insertion. Falls back to appending when the
// selection isn't inside the composer (e.g. attach button was tapped with no caret).
function insertNodeAtCaret(node) {
  bar.input.focus();
  const sel = window.getSelection();
  let range;
  // A saved range WINS over the live selection: focus() above often synthesizes a caret
  // at the END of the field, which would look like a valid live selection and land the
  // chip at the end — the exact #70 bug saveCaret exists to prevent. So if we snapshotted
  // a caret before the picker blurred us, honor it first.
  if (_savedRange && bar.input.contains(_savedRange.startContainer)) {
    range = _savedRange;
    range.deleteContents(); // replace a saved SELECTION too, matching the live-caret path
  } else if (sel && sel.rangeCount && bar.input.contains(sel.anchorNode)) {
    range = sel.getRangeAt(0); // live caret/selection inside the composer (no snapshot)
    range.deleteContents();
  } else {
    range = document.createRange(); // no caret at all → append at the end
    range.selectNodeContents(bar.input);
    range.collapse(false);
  }
  _savedRange = null;
  range.insertNode(node);
  range.setStartAfter(node);
  range.collapse(true);
  if (sel) { sel.removeAllRanges(); sel.addRange(range); } // some mobile states have no Selection
}
function insertTextAtCaret(text) { if (text) insertNodeAtCaret(document.createTextNode(text)); }

// Snapshot the composer caret before an action that blurs it (opening the file picker).
// insertNodeAtCaret restores it so an attached image lands where the caret WAS, not at
// the end. Cloned because the live range mutates once focus leaves.
let _savedRange = null;
function saveCaret() {
  const sel = window.getSelection();
  _savedRange = sel && sel.rangeCount && bar.input.contains(sel.anchorNode)
    ? sel.getRangeAt(0).cloneRange() : null;
}

// Clear the composer: revoke every chip's object URL (else the blobs leak) and empty the
// element so :empty restores the placeholder.
function clearComposer() {
  bar.input.querySelectorAll(".attach-chip").forEach((c) => chipFiles.delete(c));
  bar.input.textContent = "";
  sweepChipUrls(); // emptied the field → every chip is now an orphan; revoke all blobs
}

// The question block, built once. The spinner must be a PERMANENT node: a fresh element's
// CSS animation always restarts at 0°, so a spinner recreated per render visibly snaps
// back to its first quarter-turn. Kept alive, the animation simply keeps running — no
// negative-animation-delay phase seeding needed.
function buildQuestion(cardUi) {
  const root = document.createElement("div");
  root.className = "q";
  const prompt = document.createElement("div");
  prompt.className = "prompt";
  const promptText = document.createElement("span");
  const spin = document.createElement("span");
  spin.className = "q-spin";
  spin.setAttribute("role", "status");
  spin.setAttribute("aria-label", "submitting");
  prompt.append(promptText, spin);
  const opts = document.createElement("div");
  opts.className = "opts";
  root.append(prompt, opts);
  // Keyed by their own text, so a menu that keeps the same options keeps the same button
  // nodes. The handler reads the CURRENT question off panesById at call time, so a stale
  // option node can never answer with a stale prompt's keystroke.
  return { root, prompt, promptText, spin, opts, cardUi };
}

function applyQuestion(ui, s) {
  setCls(ui.root, "hid", !s);
  if (!s) { keyedList(ui.opts, [], (x) => x, () => null); setText(ui.promptText, ""); return; }
  const spinning = isReparsing(s); // answer submitted — options locked, spinner shown
  setText(ui.promptText, s.question.prompt);
  setCls(ui.spin, "on", spinning);
  // Drop any "type something"/"Other" pseudo-option — the bottom bar covers free-text.
  const realOpts = (s.question.options || []).filter((o) => !_FREETEXT_OPT.test(o.trim()));
  const paneId = s.pane_id;
  keyedList(ui.opts, realOpts, (o, i) => i + " " + o, (opt) => {
    const b = document.createElement("button");
    b.className = "opt";
    b.onclick = () => {
      // Read the live state at CALL time: the node persists across polls, so closing over
      // `s` would answer with whatever question was on screen when the button was built.
      const cur = panesById[paneId];
      if (!cur || !cur.question) return;
      const i = b._optIndex;
      setActive(paneId);
      answer(cur, keyFor(cur.question, b._optText, i));
    };
    return b;
  }, (b, opt, i) => {
    b._optText = opt; b._optIndex = i;
    setText(b, opt);
    // Once an answer is in flight the options disable — a second tap would send a stray
    // keystroke into the agent while the first is still being processed.
    if (b.disabled !== spinning) b.disabled = spinning;
  });
  // Free-text reply goes through the single bottom bar (no per-card input anymore).
}

const _FREETEXT_OPT = /^(type\b|other\b|something else|let me|custom|free.?text|write )/i;

// Decide what keystroke represents the chosen option. y/n prompts want a letter;
// numbered menus want the number; otherwise send the literal option text.
// What to send when an option is tapped, per answer_style:
//   "menu"  — a real on-screen widget: options map to keystrokes (digit / y|n letter).
//   "text"  — a natural-language question (default): TYPE the option's text as a reply.
// Getting this wrong is what made tapping option 4 type a stray "4" into a prose
// question instead of answering it — so default to text unless it's truly a menu.
function keyFor(question, opt, i) {
  if (question.answer_style === "menu") {
    const lc = opt.toLowerCase();
    if (question.options.length === 2 && (lc === "yes" || lc === "no")) return lc[0];
    if (question.options.length > 2) return String(i + 1);
  }
  return opt; // text style (default): send the option's literal text
}

async function answer(s, keys) {
  // A staged image is composer state, sent only by submitComposer — answering a
  // question (option tap / free-text) leaves it queued for the user's own send.
  await send(s, { keys, enter: true, literal: true });
}

// Send a tmux key-name (Escape/Up/C-c) — not literal text, no appended Enter. Leaves
// any staged image in place (it's flushed only by submitComposer's Send/Enter).
async function sendRaw(s, keyName) {
  await send(s, { keys: keyName, enter: false, literal: false });
}

// POST keys to the pane. No burst needed: the visible raw surface streams via
// liveStream, so the keystroke shows up in the next live frame on its own
// (docs/design/live-view.md). Throws on a bad response (fetch only rejects on network
// error) so submitComposer's loop aborts before Enter/clear instead of dropping a
// segment silently. Pure — it owns no flag; `sending` belongs to its callers.
async function postSend(s, body) {
  // HARD TIMEOUT. Without one, a stalled request (phone radio handoff, tunnel/relay
  // hiccup) hangs this await forever — and because the caller holds `sending` across it,
  // EVERY later tap becomes a silent no-op. That is the "Send just hangs, then nothing
  // works" failure: not a dropped tap, a latched guard behind a fetch with no deadline.
  // 8s is far longer than a keystroke POST needs on a bad connection, and far shorter
  // than a user's patience. On timeout we throw, which unlatches the guard and shows the
  // inline notice, so the next tap can actually retry.
  const r = await fetch(`/api/panes/${encodeURIComponent(s.pane_id)}/send`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal: timeoutSignal(8000),
  });
  if (!r.ok) throw new Error("send failed: " + r.status);
}

async function send(s, body) {
  // `sending` guards a real in-flight POST against double-firing — a genuine concern, and
  // distinct from anything about rendering.
  //
  // This path DROPS rather than queues, unlike the composer: an option tap or a raw key is
  // only meaningful against the screen the user was looking at, so replaying it after an
  // unrelated send lands could answer a different prompt than the one they read. But
  // dropping it silently is exactly the "the button just does nothing" this branch exists
  // to eliminate, so say so.
  if (sending) return void barNote("Busy sending — that didn't go through. Tap again.");
  sending = true;
  markReparsing(s.pane_id); // spin the card until the server's forced reparse lands
  render(Object.values(panesById)); // reflect the spinning state immediately
  try {
    await postSend(s, body);
  } catch (e) {
    // postSend has a hard timeout now, so this path is reachable — without a catch it
    // would be an unhandled rejection and the user would see nothing at all.
    //
    // Un-spin FIRST. markReparsing() above put the card into "submitting" — spinner up,
    // answer options gated by isReparsing — and that state clears only when a parse lands
    // or REPARSE_TIMEOUT (12s) expires. Nothing was sent, so no parse is coming: the card
    // would sit there spinning with its options locked for 12s while the notice below
    // says "Tap again to retry", making the retry it asks for impossible.
    delete reparsing[s.pane_id];
    barNote(`Not sent — ${e.message}. Tap again to retry.`);
    reportError("send", e);
    render(Object.values(panesById)); // drop the spinner now, not on the next poll
  } finally {
    sending = false; // re-entry guard: released now, so a failed answer stays retryable
  }
}

// Full-screen live view of the pane (⤢ over the deck): the same long-poll stream as
// the peek, rendered big and pan/zoomable. Color = live; gray = the stream went quiet.
function openScreen(paneId, label) {
  const ov = document.createElement("div");
  ov.className = "screen-overlay";
  ov.innerHTML =
    `<div class="screen-head"><span>${esc(label || paneId)}</span><span class="hd-btns">` +
    `<button class="screen-sun" title="Sun mode — dark-on-light for outdoors" aria-label="Sun mode" aria-pressed="false">${licon("sun", 16)}</button>` +
    `<button class="screen-close">✕</button></span></div>` +
    `<div class="screen-body"><pre class="screen-pre">(connecting…)</pre></div>`;
  // Sun mode persists across opens — outdoors you want every pane light, not one.
  const sun = ov.querySelector(".screen-sun");
  const setSun = (on) => {
    ov.classList.toggle("light", on);
    sun.setAttribute("aria-pressed", String(on));
    try { localStorage.setItem("tmuxrc-sun", on ? "1" : ""); } catch {}
  };
  let sunOn = false;
  try { sunOn = !!localStorage.getItem("tmuxrc-sun"); } catch {} // private mode ⇒ default dark
  setSun(sunOn);
  sun.onclick = () => setSun(!ov.classList.contains("light"));
  const body = ov.querySelector(".screen-body");
  const pre = ov.querySelector(".screen-pre");
  // Seed with the pane's last-known frame (gray) so the view isn't blank while the
  // stream connects — same instant-stale trick as the peek.
  const cached = peekCache[paneId];
  if (cached) { pre.innerHTML = cached.html; pre.classList.add("stale"); }
  document.body.appendChild(ov);
  // Selection containment flag — a class, not :has() (unsupported on older iOS
  // Safari, which this codebase otherwise accounts for).
  document.body.classList.add("screen-open");
  pinchZoom(body, pre, null, false, true); // selectable: one finger = native select/copy
  // Only ONE stream per pane at a time. screenOpen makes bgTerm stand the peek down
  // for as long as the overlay lives — without it the 2s render() poll would keep
  // restarting a second peek stream. Stop the current peek now; the overlay owns the
  // stream, and the peek re-mounts on the poll after close (screenOpen back to false).
  screenOpen = true;
  // Also reset peekLive: the stopped peek is no longer live, so when it re-mounts
  // after close it starts stale (gray) until its own stream confirms, rather than
  // briefly showing colored-but-actually-stale on the first post-close poll.
  if (peekStop) { peekStop(); peekStop = null; peekStreamPane = null; peekLive = false; }
  const stop = liveStream(paneId, {
    onFrame: (txt) => {
      const html = renderCapture(txt.replace(/\s+$/, ""), { color: true });
      peekCache[paneId] = { html }; // shared cache with the peek
      // A live frame swap would destroy the selection the user is building — hold
      // updates while one exists in the terminal; the next frame after it's dismissed
      // catches up (frames keep coming).
      const sel = document.getSelection();
      if (sel && !sel.isCollapsed && pre.contains(sel.anchorNode)) return;
      if (pre.innerHTML !== html) pre.innerHTML = html; // no-op swap = flicker; skip it
    },
    onLive: () => pre.classList.remove("stale"),
    onQuiet: () => pre.classList.add("stale"),
  });
  ov.querySelector(".screen-close").onclick = () => {
    stop(); ov.remove(); screenOpen = false; // next poll re-mounts the peek stream
    document.body.classList.remove("screen-open");
  };
}

// Pinch-to-zoom + pan for just the terminal content (transform on the <pre>, not the
// page). One-finger drag pans; two-finger pinch zooms around the gesture midpoint.
// Pass `st` to persist the transform (the peek's per-pane bgZoom entry); pass null and
// it starts fresh at the BOTTOM-left — the end of a capture is the live state. (Captures
// shorter than the window stay top-aligned: that's the Math.min clamp.) `snapHome`:
// unzoomed pans spring back on release (drag-to-peek) instead of parking the content askew.
//
// Returns a handle:
//   retarget(next) — point the gesture state at a DIFFERENT persisted transform and apply
//     it. The peek is ONE element reused across panes (see bgTerm), so a pane switch
//     re-points this instead of constructing a new pinchZoom and leaking a listener set.
//   panning()      — is the user's finger on this surface right now. The live repaint uses
//     it to suppress its scroll-to-tail. Deliberately local, not a global flag: a stuck
//     gesture here must not be able to freeze anything else.
function pinchZoom(container, el, st, snapHome, selectable) {
  const fresh = () => ({ scale: 1, tx: 0, ty: Math.min(0, container.clientHeight - el.offsetHeight) });
  st = st || fresh();
  let start = null; // {dist, cx, cy} for pinch, or {x,y} for pan
  const apply = () => { el.style.transform = `translate(${st.tx}px,${st.ty}px) scale(${st.scale})`; };
  el.style.transformOrigin = "0 0";
  if (st.scale !== 1 || st.tx || st.ty) apply();
  const dist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  const mid = (t) => ({ x: (t[0].clientX + t[1].clientX) / 2, y: (t[0].clientY + t[1].clientY) / 2 });
  container.addEventListener("touchstart", (e) => {
    // `selectable`: ONE finger belongs to the browser — long-press → select → copy
    // (copying out of the terminal is a core use case). Our JS claims only two-finger
    // gestures there; without it (the peek) one-finger drag still pans.
    // targetTouches, not touches, everywhere in here: `touches` counts every finger on the
    // PAGE, so a thumb resting on the composer would make this container think a one-finger
    // pan was a two-finger pinch, and would hold `start` alive after the user lifted off it.
    // targetTouches counts only the fingers that started on this container.
    const t = e.targetTouches;
    if (selectable && t.length === 1) return;
    if (t.length === 2) { const m = mid(t); start = { dist: dist(t), s: st.scale, tx: st.tx, ty: st.ty, cx: m.x, cy: m.y }; }
    else if (t.length === 1) start = { pan: true, x: t[0].clientX - st.tx, y: t[0].clientY - st.ty };
  }, { passive: false });
  container.addEventListener("touchmove", (e) => {
    if (!start) return;
    e.preventDefault();
    const t = e.targetTouches;
    if (start.pan && t.length === 1) {
      st.tx = t[0].clientX - start.x; st.ty = t[0].clientY - start.y;
    } else if (t.length === 2) {
      const m = mid(t); // anchor to the LIVE midpoint: pinch zooms AND pans
      const f = dist(t) / start.dist;
      st.scale = Math.min(6, Math.max(0.4, start.s * f));
      // the content point under the start midpoint stays under the fingers
      st.tx = m.x - (start.cx - start.tx) * (st.scale / start.s);
      st.ty = m.y - (start.cy - start.ty) * (st.scale / start.s);
    }
    apply();
  }, { passive: false });
  const release = (e) => {
    if (e.targetTouches.length !== 0) {
      // Fingers still down, so the gesture continues — but it is a DIFFERENT gesture now.
      // Lifting one finger of a pinch leaves `start` describing two, and touchmove would
      // then preventDefault (start is truthy) while updating nothing (not .pan, and only
      // one touch), pinning the content unresponsive under the remaining finger. Re-seed
      // as a pan from where that finger actually is so it keeps working; anything else
      // (>2 fingers, or a selectable container's single finger) ends the gesture cleanly.
      const t = e.targetTouches;
      if (t.length === 1 && !selectable) {
        start = { pan: true, x: t[0].clientX - st.tx, y: t[0].clientY - st.ty };
      } else {
        start = null;
      }
      return;
    }
    start = null;
    if (!snapHome) return;
    // Clamp at ANY zoom so no edge ever shows a black gap: slide the content back
    // until it covers the window (or pins bottom-left when it's smaller than it).
    const c = container.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    // rect width LIES for the peek: it's a <pre> whose long lines overflow the box
    // sideways without growing it, so the rect reads window-width and the clamp
    // yanked every horizontal pan home. scrollWidth sees the real text extent.
    const cw = el.scrollWidth * st.scale;
    const right = r.left + cw;
    let dx = 0, dy = 0;
    if (cw <= c.width) dx = c.left - r.left;
    else if (r.left > c.left) dx = c.left - r.left;
    else if (right < c.right) dx = c.right - right;
    if (r.height <= c.height) dy = c.bottom - r.bottom; // short content: pin the tail
    else if (r.top > c.top) dy = c.top - r.top;
    else if (r.bottom < c.bottom) dy = c.bottom - r.bottom;
    if (!dx && !dy) return;
    st.tx += dx; st.ty += dy;
    el.style.transition = "transform .2s ease-out";
    apply();
    setTimeout(() => (el.style.transition = ""), 220);
  };
  container.addEventListener("touchend", release);
  container.addEventListener("touchcancel", release);
  // No document-level listener and nothing global to unwedge: `start` is per-instance. A
  // gesture interrupted by the OS just leaves `start` set on an instance nobody is
  // touching, which the next touchstart overwrites.
  return {
    retarget(next) { st = next || fresh(); start = null; apply(); },
    panning() { return start !== null; },
  };
}

function esc(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

// Purge any previously-installed service worker + caches. An old SW (from before we
// went cache-less) keeps serving a stale app.js on the phone even after edits — which
// is why new buttons didn't appear on reload. Unregister everything so the phone
// always fetches fresh from the network. (No SW ⇒ not installable, fine for the PoC.)
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations().then((rs) => rs.forEach((r) => r.unregister()));
}
if (window.caches) caches.keys().then((ks) => ks.forEach((k) => caches.delete(k)));
pollLoop(); // self-rescheduling long-poll (replaces the fixed 2s interval)
syncBadgeTick(); // live-tick idle/waiting durations while visible (paused when hidden)

// Auto-update: when the web assets change, reload to the new version (checked every
// 5s against /api/version; all durable state lives server-side). UNLESS the user has
// un-sent state — a typed draft or a staged image — which a silent reload would eat:
// then show a tap-to-reload banner instead and let them choose the moment.
let _ver = null;
setInterval(async () => {
  try {
    const { version, live_enabled } = await (await fetch("/api/version")).json();
    applyLiveEnabled(!!live_enabled);  // sync the mic button to the server flag
    if (_ver === null) _ver = version;
    else if (version !== _ver) {
      if (composerEmpty()) location.reload();
      else showUpdateBanner();
    }
  } catch {}
}, 5000);

function showUpdateBanner() {
  if (document.getElementById("upbanner")) return;
  const b = document.createElement("button");
  b.type = "button";
  b.id = "upbanner";
  b.textContent = "New version — tap to reload";
  b.onclick = () => location.reload();
  document.body.appendChild(b);
}

// Keyboard fit: CSS 100dvh / flex compression doesn't reliably clear the on-screen
// keyboard on mobile Chrome (the bar ends up half-covered). Explicitly PIN the bar to
// the bottom of the visual viewport (the visible area above the keyboard) via a fixed
// transform, and pad #panes so nothing hides behind it. This positions the whole bar —
// keys + input + send — right above the keyboard regardless of flex math.
const barEl = document.getElementById("bar");
// Publish the MEASURED heights of the fixed chrome as CSS vars — deck/peek/card
// layout math uses them instead of hardcoded px that drift per device and content
// (a taller chip row made the peek window end mid-line with dead black below it).
{
  const topBlock = document.getElementById("top");
  const sizes = () => {
    document.documentElement.style.setProperty("--bar-h", barEl.offsetHeight + "px");
    document.documentElement.style.setProperty("--top-h", topBlock.offsetHeight + "px");
  };
  const ro = new ResizeObserver(sizes);
  ro.observe(barEl);
  ro.observe(topBlock);
  sizes();
}
if (window.visualViewport && barEl) {
  const vv = window.visualViewport;
  // Keyboard height = how much the layout viewport exceeds the visible viewport. Lift
  // the fixed bar (bottom:0) by that amount so it rides just above the keyboard. No
  // offsetHeight reads (which mis-measured and pushed it off-screen) — just a bottom
  // offset, clamped to >=0.
  const fit = () => {
    const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    barEl.style.bottom = kb + "px";
  };
  vv.addEventListener("resize", fit);
  vv.addEventListener("scroll", fit);
  bar.input && bar.input.addEventListener("focus", () => setTimeout(fit, 100));
  fit();
}

// ═══════════════════ Live Mode: talk to your whole tmux session ═══════════════════
// One WebSocket to /api/live-mode. We stream mic PCM up; the daemon owns the Gemini
// Live session and streams back voice audio, both transcripts, and a "typed" event for
// every keystroke the model puts into a pane. Nothing overlays the app: the pulsing 🎙
// header pill is the status, and the rolling conversation renders in the active card's
// summary slot (see applyCard's `lmOwns`). Design: docs/design/live-mode.md.
const lm = { btn: document.getElementById("lm-btn") };
// The static buttons get their icons here (their HTML ships empty): mic without the
// word "live" — the pill + beta tag carry the meaning; keyboard/paperclip likewise.
if (lm.btn) lm.btn.innerHTML = licon("mic", 14) + '<sup class="lm-exp">beta</sup>';
bar.keysToggle.innerHTML = licon("keyboard", 17);
bar.attach.innerHTML = licon("paperclip", 15);
// Live Mode ships behind a server flag (TMUXRC_LIVE_MODE). Hide the mic button unless
// the server reports it enabled — one source of truth, so a stale tab can't offer a
// button the /api/live-mode route will just refuse. Hidden until confirmed.
function applyLiveEnabled(on) {
  if (lm.btn) lm.btn.hidden = !on;
}
applyLiveEnabled(false);
// Resolve the flag immediately on load (the 5s version poll also keeps it in sync).
fetch("/api/version").then((r) => r.json()).then((d) => applyLiveEnabled(!!d.live_enabled)).catch(() => {});
let lmWs = null, lmCtx = null, lmStream = null, lmNodes = [];
let lmPlay = null, lmPlayAt = 0; // playback context + scheduled-until clock
let lmLog = [];                  // rolling conversation: {role, text, done}
let lmListening = false;         // true only while the daemon reports "listening" — mic
                                 // frames are dropped otherwise so a reconnect (during
                                 // which the server stops reading) can't grow bufferedAmount

// Transcription arrives as fragments; grow the current entry for that role until the
// turn completes. Typed actions and errors are single whole entries.
function lmAdd(role, text) {
  const grow = role === "user" || role === "model";
  const last = lmLog[lmLog.length - 1];
  if (grow && last && last.role === role && !last.done) last.text += text;
  else lmLog.push({ role, text, done: !grow });
  while (lmLog.length > 8) lmLog.shift();
  lmPaint();
}

// Keyed by position+role, so a GROWING transcript entry (fragments append to the last
// entry of the same role) keeps its node and only its text changes — fragments arrive
// several times a second, and rebuilding would fight text selection in the transcript.
function lmPaintInto(box) {
  const atBottom = atBottomOf(box);
  keyedList(box, lmLog, (e, i) => i + ":" + e.role, () => document.createElement("div"),
    (d, e) => {
      setAttr(d, "class", "lm-" + e.role);
      setText(d, (e.role === "user" ? "🗣 " : "") + e.text);
    });
  // Follow the tail only when already there, so reading back through the transcript isn't
  // yanked down by the next incoming fragment.
  if (atBottom) box.scrollTop = box.scrollHeight;
}

// Repaint between renders, straight onto the card's PERMANENT .lm-convo node — the card
// is retargeted rather than rebuilt, so the box a transcript fragment paints into is the
// same one applyCard writes, and there is no re-inserted copy to go looking for.
function lmPaint() {
  const box = cardUI && cardUI.lm;
  if (box) lmPaintInto(box);
}

// The model's voice: base64 24kHz PCM16 chunks, scheduled back-to-back on a dedicated
// context (created in the button's click handler, satisfying autoplay policy).
function lmPlayChunk(b64) {
  if (!lmPlay) return;
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const pcm = new Int16Array(bytes.buffer);
  const buf = lmPlay.createBuffer(1, pcm.length, 24000);
  const ch = buf.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 0x8000;
  const src = lmPlay.createBufferSource();
  src.buffer = buf;
  src.connect(lmPlay.destination);
  lmPlayAt = Math.max(lmPlayAt, lmPlay.currentTime) ;
  src.start(lmPlayAt);
  lmPlayAt += buf.duration;
}

// Mic → 16kHz Int16 PCM → base64 frames. AudioWorklet (inline module) is the capture
// tap; if the context won't run at 16kHz (Safari ignores the request), we resample
// before sending — the wire format is always 16kHz.
async function lmCapture(ws) {
  lmStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
  });
  try { lmCtx = new AudioContext({ sampleRate: 16000 }); }
  catch { lmCtx = new AudioContext(); }
  const src = lmCtx.createMediaStreamSource(lmStream);
  const rate = lmCtx.sampleRate;
  let pend = new Float32Array(0);
  const push = (chunk) => {
    const joined = new Float32Array(pend.length + chunk.length);
    joined.set(pend); joined.set(chunk, pend.length);
    pend = joined;
    if (pend.length < 4096) return;
    let f = pend; pend = new Float32Array(0);
    if (rate !== 16000) { // linear resample to the wire rate
      const n = Math.round(f.length * 16000 / rate), r = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        const x = i * (f.length - 1) / (n - 1), lo = Math.floor(x);
        r[i] = f[lo] + (f[Math.min(lo + 1, f.length - 1)] - f[lo]) * (x - lo);
      }
      f = r;
    }
    const pcm = new Int16Array(f.length);
    for (let i = 0; i < f.length; i++) pcm[i] = Math.max(-1, Math.min(1, f[i])) * 0x7fff;
    let bin = "";
    const bytes = new Uint8Array(pcm.buffer);
    for (let i = 0; i < bytes.length; i += 0x8000)
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    // Only stream while the server is actively listening — during a reconnect it stops
    // reading, so sending would just pile up in the socket's client-side buffer.
    if (lmListening && ws.readyState === WebSocket.OPEN)
      ws.send(JSON.stringify({ action: "audio", data: btoa(bin) }));
  };
  const mod = URL.createObjectURL(new Blob([
    'registerProcessor("lm-tap", class extends AudioWorkletProcessor {',
    ' process(inputs) { const c = inputs[0][0]; if (c) this.port.postMessage(c.slice(0)); return true; } });',
  ], { type: "application/javascript" }));
  await lmCtx.audioWorklet.addModule(mod);
  URL.revokeObjectURL(mod);
  const tap = new AudioWorkletNode(lmCtx, "lm-tap");
  tap.port.onmessage = (e) => push(e.data);
  const mute = lmCtx.createGain(); // keep the graph alive without echoing the mic
  mute.gain.value = 0;
  src.connect(tap); tap.connect(mute); mute.connect(lmCtx.destination);
  lmNodes = [src, tap, mute];
}

// The pulsing mic IS the status line: red pill = session up, pulse = listening.
function lmStatus(s) {
  lmListening = s === "listening";  // gates mic streaming (see push())
  lm.btn.classList.toggle("listening", lmListening);
}

async function lmStart() {
  lm.btn.classList.add("on");
  lmLog = [];
  lmPlay = new AudioContext(); // in the click handler: autoplay-policy safe
  lmPlayAt = 0;
  // Same page-load session id as the live-view stream, so voice cost and screen
  // watch-time join under one key in telemetry (docs/design/live-telemetry.md).
  const q = SESSION_ID ? `?session=${encodeURIComponent(SESSION_ID)}` : "";
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/live-mode${q}`);
  lmWs = ws;
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "status") lmStatus(m.status);
    else if (m.type === "transcript") lmAdd(m.role, m.text);
    else if (m.type === "turn_complete") lmLog.forEach((e) => { e.done = true; });
    else if (m.type === "audio") lmPlayChunk(m.data);
    else if (m.type === "typed")
      lmAdd("typed", `⌨ ${m.label} (${m.pane_id})${m.submitted ? "" : " (not submitted)"}: ${m.text}`);
    else if (m.type === "error") lmAdd("err", m.message); // .lm-err red = the signal
  };
  ws.onclose = (e) => {
    if (lmWs !== ws) return;
    // An abnormal close (never opened / dropped mid-session) is exactly the ws failure
    // #57 wants visible; only a CLEAN close is skipped — 1000 (normal, we called stop)
    // and 1005 (no status). Everything else, INCLUDING code 0/1006 (failed handshake /
    // no close frame), is reported — those are the very failures this surfaces.
    if (e.code !== 1000 && e.code !== 1005)
      reportError("ws", { name: "close " + e.code, message: e.reason || "" });
    lmStop();
  };
  ws.onopen = async () => {
    if (lmWs !== ws) return; // stopped while connecting — don't touch the mic
    try { await lmCapture(ws); }
    catch (e) {
      // Surface the real reason PERSISTENTLY: lmStop() re-renders and wipes the card
      // feed, so a mere lmAdd flashes and vanishes (invisible on mobile). alert() so the
      // operator can actually read why the mic failed — name+message distinguish a
      // permission denial (NotAllowedError) from no-device (NotFoundError) etc. Also
      // report to telemetry so the failure rate is queryable by platform (#57).
      reportError("mic", e);
      lmStop();
      alert(`Live Mode mic error:\n${e.name || "Error"}: ${e.message}\n\n`
        + "If this is a permission issue: grant microphone access to this site/app "
        + "in your browser or Android app settings, then try again.");
    }
  };
  lm.btn.title = lm.btn.ariaLabel = "End Live Mode (experimental)";
  render(Object.values(panesById)); // swap the active card's summary for the convo box
}

function lmStop() {
  const ws = lmWs; lmWs = null;
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify({ action: "stop" })); } catch {}
    setTimeout(() => { try { ws.close(); } catch {} }, 250);
  } else if (ws) {
    try { ws.close(); } catch {} // CONNECTING: abort so a late open can't start capture
  }
  lmListening = false;
  lmNodes.forEach((n) => { try { n.disconnect(); } catch {} });
  lmNodes = [];
  if (lmStream) { lmStream.getTracks().forEach((t) => t.stop()); lmStream = null; }
  if (lmCtx) { try { lmCtx.close(); } catch {} lmCtx = null; }
  if (lmPlay) { try { lmPlay.close(); } catch {} lmPlay = null; }
  lm.btn.classList.remove("on", "listening");
  lm.btn.title = lm.btn.ariaLabel = "Start Live Mode (experimental)";
  render(Object.values(panesById)); // the active card gets its static summary back
}

if (lm.btn) lm.btn.onclick = () => (lmWs ? lmStop() : lmStart());
