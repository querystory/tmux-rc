// tmux-rc PWA. Polls /api/state, renders ONE pane card at a time (the dock — icon
// tabs, tally filters — and card swipes switch panes), and posts answers back.
// No framework, no build step (native ES module — index.html loads type=module).
import { renderCapture, linkifyText } from "./terminal.js";

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
const img = (src, alt) => `<img src="${src}" width="22" height="22" alt="${escAttr(alt)}" style="border-radius:5px" />`;
const iconFor = (tool) => img(has(LOGOS, tool) ? LOGOS[tool] : UNKNOWN_LOGO, tool || "pane");

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
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
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

// Track which pane's timeline is expanded so a re-render doesn't collapse it.
const openTimelines = new Set();
// Collapse is a VIEW-WIDE preference, not per-pane: collapse one card (caret ▸) and
// every pane — including ones you swipe to — shows its one-line header, handing the
// screen to the live terminal. Expanding anywhere expands them all.
let cardsCollapsed = false;
// The render freeze: while held, poll re-renders are suppressed so a rebuild can't replace
// the DOM under a finger or mid-send. It is ONLY a render freeze — re-entry into a send is
// guarded by `sending`, which those functions own. Conflating the two is what made Send
// silently do nothing whenever a gesture happened to hold this flag.
//
// TWO independent holders, because one shared boolean let either subsystem release the
// other's freeze: a send settling on its 400ms timer while a swipe was still in progress
// flipped the flag and let a render land mid-gesture — the "DOM replaced under the finger"
// bug this PR exists to fix, reintroduced through the flag meant to prevent it.
//
// Deliberately two booleans rather than a refcount: pinchZoom's release() MUST be able to
// clear its hold unconditionally (a gesture that ends without a clean zero-touch touchend
// used to leak the freeze forever and wedge the whole app), and "release no matter what" is
// not expressible in a refcount without leaking the count. Each subsystem owns exactly one
// flag it may set or clear freely, and neither can speak for the other.
let busySend = false;    // a send is flushing (composer or answer/key)
let busyGesture = false; // a swipe/pinch owns the screen
const isBusy = () => busySend || busyGesture;

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
// Wire an element so a tap commits on POINTERDOWN instead of click.
//
// Why: the dock and the list are rebuilt from scratch on every poll (~2s, and twice in a
// frame when several fields change). The browser only fires `click` when press and release
// land on the SAME element, so any rebuild between finger-down and finger-up silently
// swallows the tap — the "tapping sometimes does nothing, or feels laggy" bug, where the
// lag is really the wait until the user gives up and taps again. Swiping never had it: it
// listens on the card and holds the gesture freeze for the whole gesture.
//
// pointerdown can't be stolen by a rebuild, and acting early is safe here because these
// taps only ever SELECT (setActive is idempotent, and `pending` makes it authoritative
// until the server confirms). The click handler stays for keyboard/AT activation, which
// synthesizes a click with no pointer event, guarded so a real tap can't fire twice.
//
// The guard has to be armed per-gesture, not sticky: a latched flag would swallow every
// LATER keyboard activation of the same element, and — because the pointer's click still
// bubbles — an outer onTap target (the row wrapping .row-open) would see an unguarded
// click and fire a second time. So swallow the pointer's own click, on the element that
// handled the pointerdown, and disarm.
//
// Timestamp rather than a boolean because the disarm is not guaranteed: a press that
// releases off the element produces neither `click` nor `pointercancel`, so a flag set on
// pointerdown could stay armed indefinitely and eat the next keyboard activation. A click
// belonging to our pointerdown always lands in the same task-ish window as the release, so
// bounding the suppression in time is self-healing where an event-based reset isn't.
// `defer`: wait for pointerup instead of firing on pointerdown, and cancel if the finger
// travels more than SLOP first. EVERY tap target here lives in a scroller — rows in
// vertically-scrolling #panes, the dock and filter strips in their own overflow-x — so a
// scroll gesture necessarily begins with pointerdown on one of them, and acting there
// would navigate or re-filter mid-scroll. Deferring to pointerup keeps the rebuild-immunity
// that motivates this whole helper (we still never depend on press and release landing on
// the same NODE, only on the same pointer) while letting a scroll gesture win. `defer` stays
// a parameter rather than the only behavior so a target outside any scroller can still take
// the snappier path.
//
// The deferred path's move/up listeners go on `document`, NOT on `el`. Binding them to the
// element would hand the whole bug straight back: the element is exactly the thing a poll
// rebuild detaches, and a detached node's `pointerup` never fires, so the tap would be
// swallowed again — for rows, which are the surface that motivated this. `document` outlives
// every rebuild. They are keyed by pointerId and torn down on up/cancel so a gesture can
// never observe a different finger's release, and nothing accumulates between gestures.
const TAP_CLICK_MS = 700; // generous: a slow press-and-hold still releases well inside it
const TAP_SLOP = 10;      // px of travel that still counts as a tap, not a scroll
// When a pointer gesture last ran an onTap action, in event-timeStamp terms. Module
// scope on purpose: it must outlive any node a handler replaces (see the click branch).
let _lastPointerAction = -1e9;
// Releases a deferred tap already acted on — see done(). Weak and keyed by event identity,
// so it neither mutates a host Event nor retains one.
const _tapClaims = new WeakSet();

function onTap(el, fn, defer) {
  let downAt = 0;
  el.addEventListener("pointerdown", (e) => {
    if (e.button != null && e.button !== 0) return; // left/touch only
    downAt = e.timeStamp || performance.now();
    if (!defer) { _lastPointerAction = downAt; return void fn(e); }
    // Deferred: follow THIS pointer on document until it releases. The listeners are
    // per-gesture (added here, removed in done()) rather than one long-lived pair, so
    // there is no cross-gesture state to get out of sync and nothing left behind.
    const id = e.pointerId, sx = e.clientX, sy = e.clientY;
    // Measure the target NOW, while it is still in the document. A poll rebuild can detach
    // it before the release, and a detached node's rect is 0x0 — useless for deciding
    // whether the finger came up on it. See tapStillOn.
    const downRect = el.getBoundingClientRect();
    let moved = false;
    const move = (ev) => {
      if (ev.pointerId !== id) return;
      if (Math.hypot(ev.clientX - sx, ev.clientY - sy) > TAP_SLOP) moved = true;
    };
    const done = (ev) => {
      if (ev.pointerId !== id) return;
      document.removeEventListener("pointermove", move, true);
      document.removeEventListener("pointerup", done, true);
      document.removeEventListener("pointercancel", done, true);
      // A release that scrolled, or that dragged off the target, is not a tap on it.
      if (ev.type !== "pointerup" || moved) return;
      if (!tapStillOn(el, ev, downRect)) return;
      // One release, one action. .row-open is nested inside the row and BOTH are onTap
      // targets, so one finger arms two gestures. On document, DOM nesting no longer orders
      // the handlers and stopPropagation cannot suppress a sibling document listener, so
      // whichever runs first CLAIMS the release and the other stands down. First is the
      // inner target: pointerdown still bubbles element-first, so .row-open registered its
      // document listener before the row did. Both actions are idempotent anyway — the
      // claim is what keeps it to one /select POST instead of two.
      //
      // The claim is keyed by the EVENT IDENTITY, not by a derived key. A key built from
      // pointerId+timeStamp looked tidy but collides: timeStamp can be 0 (this function
      // already distrusts it — see `e.timeStamp || performance.now()` below), and every
      // release would then hash to the same "1:0" and silently drop the SECOND real tap.
      //
      // A WeakSet rather than a property ON the event: this module is strict mode, and a
      // host Event object is not guaranteed extensible, so `ev._x = true` can throw a
      // TypeError and take out deferred taps entirely — the whole mechanism, on whichever
      // engine does that. The WeakSet needs nothing of the event but its identity, and
      // holds it weakly, so it can't pin a row a rebuild just detached.
      if (_tapClaims.has(ev)) return;
      _tapClaims.add(ev);
      _lastPointerAction = ev.timeStamp || performance.now();
      fn(ev);
    };
    document.addEventListener("pointermove", move, true);
    document.addEventListener("pointerup", done, true);
    document.addEventListener("pointercancel", done, true);
  });
  el.addEventListener("click", (e) => {
    // Suppress the click that FOLLOWS a pointer gesture we already handled, and let a
    // genuine keyboard/AT activation (which has no preceding pointerdown) through.
    //
    // The "did we already handle this?" state must live on the GESTURE, not on this
    // element. `downAt` was per-element, so a handler that re-renders — the caret, whose
    // fn() calls render() and replaces its own button — destroyed the node holding the
    // guard; the click then arrived at a FRESH button with downAt === 0, was read as
    // keyboard activation, and fired fn a second time. One tap toggled collapse and
    // immediately toggled it back. _lastPointerAction is module-scoped and survives the
    // rebuild, so the follow-up click is recognized no matter which node receives it.
    //
    // But ask WHAT the click is before asking WHEN it arrived. detail is the click count:
    // 0 only for a synthesized activation (keyboard Enter/Space, AT), >=1 for anything a
    // pointer produced. Time alone can't tell those apart, and since the stamp is global it
    // would swallow a real keyboard activation on a DIFFERENT element merely for landing
    // within TAP_CLICK_MS of an unrelated tap — tap a dock tab, then immediately press
    // Enter on the caret, and the caret would do nothing. Checking detail first makes the
    // time window a backstop for pointer clicks only, which is all it was ever for.
    if (e.detail === 0) return void fn(e); // keyboard / AT: never a follow-up
    const ts = e.timeStamp || performance.now();
    if (ts - _lastPointerAction <= TAP_CLICK_MS) {
      e.stopPropagation(); // already actioned on pointerdown/up
      return;
    }
    if (downAt) { downAt = 0; e.stopPropagation(); return; } // this pointer's own click
    fn(e); // no pointerdown seen at all ⇒ keyboard / assistive activation
  });
}

// Did the finger come up still over the thing it pressed? `el` may have been replaced by a
// poll rebuild mid-gesture, so element identity is the wrong question — geometry is. If `el`
// is still connected, the plain containment check is exact.
//
// If it was swapped out, judge against `downRect` — its box CAPTURED AT POINTERDOWN, while
// it was still in the document. Measuring at pointerup instead cannot work: a detached node
// reports a 0x0 rect, which fell through to "no geometry, don't drop the tap" and quietly
// disabled the drag-off-to-cancel rule for precisely the rebuilt-node case this function
// exists to handle. A rebuild re-renders the SAME list in the same place, so the box the
// user aimed at is where its replacement now sits; releasing outside it is a drag-away and
// should cancel.
function tapStillOn(el, ev, downRect) {
  if (el.isConnected) {
    const t = document.elementFromPoint(ev.clientX, ev.clientY);
    return !!t && (el === t || el.contains(t));
  }
  const r = downRect;
  if (!r || (!r.width && !r.height)) return true; // never measured — don't drop the tap
  return ev.clientX >= r.left && ev.clientX <= r.right
      && ev.clientY >= r.top && ev.clientY <= r.bottom;
}

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

// The working sub-line — verb · elapsed · ↓tokens (e.g. "Waiting for review 43s ↓40.4k")
// — from the parser's `working` fields. Not gated on activity: a waiting pane still
// reports what it's waiting on. Empty string when the parser gave no working fields.
function workSub(s) {
  const w = s.working || {};
  const parts = [w.verb, w.elapsed, w.tokens && "↓" + w.tokens].filter(Boolean);
  return parts.length ? `<span class="worksub">${parts.map(esc).join(" ")}</span>` : "";
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

// `link`: may the headline contain anchors? NO for list rows, where row() moves this
// header INTO a <button> (.row-open) — an <a> inside a <button> is invalid HTML, and
// browsers disagree about which one a click or Enter activates, so the row's own
// open-the-card action becomes unreliable and AT announces it inconsistently. The card
// header is not inside a button, so it keeps its links.
function paneHeader(s, { caret = false, collapsed = false, icon = false, link = false } = {}) {
  const a = actOf(s);
  const nsub = nsubOf(s);
  // idle and waiting both show time-in-state ("idle 4m" / "waiting 4m"); the
  // data-since attr lets tickBadges() advance the text every second in place, so a
  // parked pane's clock stays honest without a full re-render (see stateDur).
  const timed = a === "idle" || a === "waiting";
  const badge = timed ? `${a} ${fmtIdle(stateDur(s))}`
    : a === "running" || a === "compacting" ? `<span class="pulse"></span>${a}` : a;
  // Numeric-coerced before it touches innerHTML: paneHeader returns markup, so a
  // quoted/junk state_since must never reach an attribute (same rule as nsubOf).
  const t0 = s.state_since == null ? NaN : +s.state_since;
  const since = timed && Number.isFinite(t0) ? ` data-since="${t0}"` : "";
  return (
    (caret ? `<button class="card-caret" aria-label="${collapsed ? "expand" : "collapse"}"`
      + ` aria-expanded="${!collapsed}">${collapsed ? "▸" : "▾"}</button>` : "")
    + (icon ? `<span class="icon">${iconFor(s.tool)}` +
        // aria-hidden: a bare "2" is meaningless to AT (and garbles any computed name).
        // The count is spoken via the sub-toggle chip's text / dock icons' aria-label.
        (nsub > 0 ? `<sub class="sacount" aria-hidden="true">${nsub}</sub>` : "") + `</span>` : "")
    // Rows (icon mode) lead with the tmux window number — the identity the user reads
    // off their own status bar — so the list scans as "the windows of this session".
    + `<div class="ph-meta"><div class="ph-name">`
    + (icon && s.window_index != null ? `<span class="wnum">${esc(String(s.window_index))}</span>` : "")
    + `${esc(s.title || s.label || s.pane_id)}</div>`
    + (s.headline ? `<div class="ph-sub">${link ? linkifyText(s.headline) : esc(s.headline)}</div>` : "")
    + `</div><div class="ph-right">${workSub(s)}<span class="badge b-${a}"${since}>${badge}</span></div>`
  );
}

// Advance every idle/waiting badge's text once a second, in place, from its data-since.
// The server bumps the deck version only when something it renders changes, so a parked
// pane wouldn't otherwise re-render — this keeps its clock climbing between the sparse
// re-parses without repainting the whole deck (which would disrupt peek/scroll/animation).
function tickBadges() {
  for (const el of document.querySelectorAll(".badge[data-since]")) {
    const a = el.classList.contains("b-waiting") ? "waiting" : "idle";
    el.textContent = `${a} ${fmtIdle(stateDur({ state_since: +el.dataset.since }))}`;
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
// sends never poll (they just hold the freeze; pollLoop resumes when it clears). Returns true
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
      panesEl.innerHTML = `<div class="empty">backend unavailable (${r.status})` +
        (body ? `: ${esc(body)}` : "") + (hint ? `<br><small>${esc(hint)}</small>` : "") + `</div>`;
      return false;  // back off — without a gap pollLoop would re-request instantly and hammer
    }
    const data = await r.json();
    // The freeze may have gone up WHILE this request was in flight (a send/gesture
    // started mid-fetch). Applying the response now would replace the card under the
    // user — the very thing the freeze exists to prevent. Drop it without touching
    // _stateVersion, so the next (post-freeze) hold re-fetches from the same version and
    // renders in order.
    //
    // Returns FALSE, not true, so pollLoop puts a gap in before re-requesting. Dropping
    // the body leaves _stateVersion pointing at a version the server has already moved
    // past, so the next request cannot HOLD — the server holds only while v equals its
    // current version — and is answered immediately. Reporting success there re-requests
    // with zero gap, and once the freeze lifts pollLoop's own busy-sleep is no longer
    // there to bound it either: an instantly-answered request in a zero-gap loop is a hot
    // spin against the backend for as long as the race lasts.
    //
    // The version deliberately stays STALE rather than being advanced-without-rendering.
    // Advancing it would re-hold on a version whose content was never drawn, so if nothing
    // changed again the server would hold ~25s with the card still stale. Keeping it behind
    // is what guarantees the next poll re-fetches and renders.
    if (isBusy()) return false;
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
      // The literal Ctrl-B button exists because a remapped prefix (this host uses C-a)
      // leaves no way to send a real Ctrl-B, which nested tmux and apps that bind it
      // themselves need. On a STOCK tmux the prefix already IS C-b, so the two buttons
      // would be the same keystroke under two labels — hide the literal one there rather
      // than ship a duplicate. Keyed off the server's detected prefix, so it follows a
      // config change without a reload.
    }
    // OUTSIDE the `data.prefix` guard on purpose. The button starts hidden in the HTML so a
    // stock-tmux user never sees it flash as a duplicate of Prefix — which means the reveal
    // is the only thing that can ever show it. Gated on a truthy prefix, a legacy or partial
    // /api/state that omits the field would hide Ctrl-B FOREVER, turning a cosmetic flicker
    // fix into a missing key. Absent prefix ⇒ we cannot know it is C-b ⇒ show it.
    const cb = document.getElementById("bar-ctrl-b");
    if (cb) cb.hidden = data.prefix === "C-b";
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
    // app resume. Full replacement only when nothing is rendered yet (cold load) or
    // the failure isn't transient.
    if (transient && panesEl.children.length && !panesEl.querySelector(".empty")) return false;
    const hint = transient ? "reconnecting…" : "often a stale cached app.js — hard-refresh";
    panesEl.innerHTML = `<div class="empty">poll error: ${esc(String(e && e.message || e))}<br>` +
      `<small>(${hint})</small></div>`;
    return false;
  }
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
// ONLY caller of poll(), so no concurrent-fetch coordination is needed. While `busy` (a
// send/gesture froze re-renders) skip the fetch and wait a beat; poll() returning false
// (backend down / legacy daemon) also backs off, so we never tight-loop.
async function pollLoop() {
  let heldSince = 0;
  for (;;) {
    if (isBusy()) {
      // WATCHDOG. The freeze is owned by whichever gesture/send set it, and
      // a missed release wedges the whole app: the poll stops, the deck goes stale, and the
      // composer paths that coordinate through it stop working — indistinguishable from a
      // dead app, unfixable without a reload. Every known leak is fixed, but the failure
      // mode is severe and the flag is set from many places, so refuse to honor it beyond
      // any plausible gesture or send (a slow image upload is seconds, not ten).
      // Self-healing beats correct-in-theory here.
      //
      // The FREEZE only — never `sending`. A >10s hold is usually a leak, but it is also exactly
      // what a large image upload on a bad network looks like, and that send's fetch is
      // still in flight. `sending` is the re-entry guard its owner releases in a finally;
      // clearing it here would re-open Send mid-request and let the same message go twice.
      // Unfreezing renders is always safe, so the watchdog's remit stops there.
      heldSince = heldSince || Date.now();
      if (Date.now() - heldSince > 10000) {
        console.warn("[tmux-rc] busy held >10s — releasing (leaked gesture/send flag)");
        reportError("busy-stuck", { name: "BusyWatchdog", message: "busy held >10s; force-released" });
        busySend = false; busyGesture = false; heldSince = 0;
      } else {
        await pollSleep(250); continue; // a send/gesture froze re-renders; re-check soon
      }
    }
    heldSince = 0;
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
  // Backgrounded mid-gesture, the OS may never deliver touchend, so the swipe/pinch that
  // set the freeze never releases it and the returning user finds a frozen app. A real
  // gesture can't survive backgrounding, so clearing here is free — and it belongs on this
  // one already-registered listener rather than one per pinchZoom instance. Clears the SEND
  // hold too: an in-flight fetch was aborted by the suspend, and its finally may never run.
  busySend = false; busyGesture = false;
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
// The docs link rides in the popover (rebuilt on every showUsage), not the top bar —
// header space is too tight on phones for a rarely-tapped link.
const DOCS_LINK = '<span class="u-row u-docs"><a id="docs-link" href="/docs/" target="_blank"' +
  ' rel="noopener" title="Design docs (opens in a new tab)">design docs ↗</a></span>';
// One labeled menu row: what the number IS on the left, the number on the right.
const uRow = (label, val, cls = "") =>
  `<span class="u-row"><span>${label}</span><span class="u-val${cls}">${val}</span></span>`;
function showUsage(u, err) {
  if (!u) {
    // Gone (reconnect, fresh daemon): drop the stale stats but keep the docs link —
    // and DON'T touch hidden/aria-expanded: force-closing on every poll would slam
    // the popover shut under a user who opened it for the docs link.
    usageEl.innerHTML = DOCS_LINK; usageEl.title = "";
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
    uRow("LLM tokens (parser + voice)", `${tok}k`),
    uRow("spend this run", `$${u.cost.toFixed(3)}`, u.errors ? " warn" : ""),
    uRow("parser calls", `${u.rate_per_min}/min`),
  ];
  if (live.sessions) {
    rows.push(uRow("voice sessions", String(live.sessions)));
    rows.push(uRow("voice spend (of total)", `$${live.cost.toFixed(3)}`));
    rows.push(uRow("parser spend (of total)", `$${parser.toFixed(3)}`));
  }
  if (err) rows.push(uRow("last LLM error", `<span class="warn" title="${escAttr(err)}">${licon("alert", 12)}</span>`));
  rows.push(DOCS_LINK);
  usageEl.innerHTML = rows.join("");
  usageEl.title = ""; // the labels ARE the explanation now — no tooltip needed
}
// Full attribute escaping: & FIRST (so introduced entities aren't re-escaped), then the
// quote/angle set. A partial escape (only ") lets a value like `&quot;` decode back into
// a quote and break out of the attribute — these values come from parser JSON (untrusted).
function escAttr(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// What the rendered UI actually depends on. If this is unchanged, a re-render would
// rebuild identical DOM — so we skip it entirely and every live node (with its handlers,
// focus, caret, scroll position and in-flight gesture) survives.
//
// THE RULE THIS ENFORCES: nothing in the UI may break because a pane is updating. Every
// control here is created fresh by render() and wired per render, so a rebuild landing
// between a finger going down and coming up silently swallowed the tap — the caret, the
// fullscreen button, answer options, the sub-agent toggle, Send, dock tabs, filters. That
// was fixed control-by-control with onTap, which is real but only ever protects the
// controls someone remembered. Skipping the pointless rebuild protects ALL of them,
// including any added later, and is also strictly less work.
//
// Deliberately EXCLUDES the streaming terminal frame (the peek repaints itself via its
// own stream, not through render) and volatile counters, so a busy pane whose output is
// scrolling does NOT churn the card. Includes everything the card/dock/list actually
// draw: identity, activity, badges, question shape, event sequence, and view mode.
function _renderFp(states) {
  return JSON.stringify([
    listFilter, cardsCollapsed, activeId(),
    states.map((s) => [
      s.pane_id, s.session, s.window_index, s.label, s.title, s.headline,
      actOf(s), s.waiting_on, s.tool, s.mode, s.model, s.context_pct, s.cost,
      s.session_active, s.tmux_active, s.events_seq, s.snapshot_id,
      // parsed_at is load-bearing, not decoration. The lists below are fingerprinted by
      // LENGTH, so a re-parse that REWRITES content without changing counts — a task's
      // text, a link's label, a copyable's payload — was invisible here and the render got
      // skipped, leaving the card stale against a parse the server had already published.
      // The daemon bumps the state version on parsed_at (watcher.py _deck_fp), so keying on
      // it makes this skip a strict subset of the server's "something changed" condition,
      // which is correct by construction instead of by remembering to list every drawn
      // field. It only advances on an actual re-parse, so it costs no extra rebuilds.
      s.parsed_at,
      s.agents, (s.subagents || []).length, (s.tasks || []).length,
      (s.copyables || []).length, (s.links || []).length, (s.tables || []).length,
      s.question ? [s.question.prompt, (s.question.options || []).length] : 0,
      s.rewind ? 1 : 0, s.session_summary, isReparsing(s),
      // idle/waiting badges tick from data-since in place (tickBadges), so the SECOND
      // must not enter the fingerprint or a parked pane would rebuild once a second.
      s.state_since ?? null,
      subsOpen.has(s.pane_id), // a Set — .has, not a spread + linear scan per pane per poll
    ]),
  ]);
}
let _renderFpLast = null;

function render(states) {
  panesById = Object.fromEntries(states.map((s) => [s.pane_id, s]));
  // Refetch each pane's server-side activity log if its events_seq advanced — AFTER
  // panesById is set, because syncEvents' async completion checks activeId() (which
  // reads panesById) to decide whether to re-render the visible feed.
  states.forEach(syncEvents);
  // Prune per-pane caches when panes vanish — otherwise pane churn grows them
  // without bound over a long-running session.
  for (const m of [eventLog, eventScroll, peekCache, bgZoom])
    for (const k of Object.keys(m)) if (!has(panesById, k)) delete m[k];
  setFavicon(states.some((s) => actOf(s) === "waiting"));
  // No card visible (empty / list mode) ⇒ no peek stream should be running. bgTerm
  // restarts it when a card renders; here we make sure it's stopped otherwise.
  const stopPeek = () => {
    if (peekStop) { peekStop(); peekStop = null; }
    peekStreamPane = peekBox = peekWrap = null;
    peekLive = false;
  };
  if (!states.length) {
    stopPeek();
    // Sweep the tab-join fillets too: they're parented to #top (to escape the dock's
    // overflow clip), so replacing #panes/#dock leaves them dangling over the empty
    // screen — the two stray blue curves seen during a daemon reload's brief no-panes.
    document.querySelectorAll(".tab-fillet").forEach((e) => e.remove());
    _joinRO.disconnect(); // stop watching the card we're about to drop
    dockEl.replaceChildren();
    filtersEl.replaceChildren(); // no panes ⇒ no tallies to filter by

    // Drop the card-view dock state too: its onscroll pin closes over the now-dead
    // card nodes, and the seam classes would style a dock that no longer has a card.
    dockEl.onscroll = null;
    dockEl.classList.remove("edge-l", "has-sel");
    // Empty deck has two causes: still loading (server booting / initial pane parses in
    // flight) vs. genuinely no panes. Only claim "no panes" once the server has booted —
    // otherwise show a spinner, since panes may exist and just aren't parsed yet.
    // Keyed by data-empty so we only rewrite innerHTML when the STATE changes: the
    // loading window spans several polls, and re-setting innerHTML each time recreated
    // the .spinner element, restarting its CSS animation → visible jitter. Same key =
    // leave the existing (still-spinning) node alone.
    const key = _booted ? "none" : "loading";
    if (panesEl.dataset.empty !== key) {
      panesEl.dataset.empty = key;
      panesEl.innerHTML = _booted
        ? '<div class="empty">No tmux pane found.<br>Start a session and it will appear here.</div>'
        : '<div class="empty"><span class="spinner" aria-hidden="true"></span><br>Loading panes…</div>';
    }
    // The empty/loading state is neither mode, and it returns BEFORE the branches below —
    // so clear the flag here or a stale cardmode from the last render squeezes the message
    // against the bar.
    panesEl.classList.remove("cardmode");
    updateBar(null);
    return;
  }
  delete panesEl.dataset.empty; // re-arm the empty-state guard for the next empty deck
  // Only the ACTIVE pane gets a full card. Other AGENT panes (and anything waiting)
  // each get a compact row above it; plain shells fold into one summary line so a
  // big fleet doesn't shove the active card off screen.
  // Nothing the UI draws has changed ⇒ do not touch the DOM. This is what makes the app
  // safe to use while panes are updating: handlers, focus, caret and gestures all live on.
  //
  // A skip here CANNOT hot-spin the poll loop, and the reason is an ordering that must be
  // preserved: poll() assigns _stateVersion from the response BEFORE calling render(). So
  // the version the server just published is always recorded, whether or not this render
  // runs, and the next long-poll re-holds on it. Were the assignment moved after render —
  // or made conditional on the render happening — every skipped render would re-request
  // with a version the server had already passed, the hold condition (v == current) would
  // never be met, and each reply would return instantly: a genuine hot loop, measured at
  // ~1000 req/s against the daemon's ~3.5/s change rate.
  const fp = _renderFp(states);
  if (fp === _renderFpLast && panesEl.firstChild) { updateBar(panesById[activeId()]); return; }
  _renderFpLast = fp;

  const act = activeId();
  // List mode (a dock tally badge or "all" was tapped): just those panes as
  // one-liners; the dock stays up (tap an icon or a row to open that pane's card).
  // "recent" is not an activity — it deliberately spans them (see isRecent: anything not
  // idle counts however old, plus idle panes younger than PARKED_IDLE_SECS). So it gets its
  // own arm rather than being compared against actOf().
  const subset = listFilter && states.filter((s) =>
    listFilter === "all" ? true : listFilter === "recent" ? isRecent(s) : actOf(s) === listFilter);
  if (subset && subset.length) {
    stopPeek(); // list mode: no card, no peek stream
    dock(states, act); // dock stays up in list mode — icon tap jumps to that card
    // Rows in server order — same as the dock. That order is tmux's own
    // session/window/pane order, so grouping windows under their session is just
    // "insert a header where the session changes": no sorting, no client-side
    // restructure. Headers only when the deck actually spans sessions — a lone
    // header over every row would be noise for the single-session common case.
    // Ordinals come from the FULL deck (not the filtered subset) so a header's hue
    // always matches that session's dock rail even when a filter hides sessions.
    const ord = new Map();
    states.forEach((s) => { if (!ord.has(s.session)) ord.set(s.session, ord.size); });
    panesEl.classList.remove("cardmode"); // list rows need the bar padding to clear the bar
    panesEl.replaceChildren(...subset.flatMap((s, i) => {
      const r = row(s, act);
      if (ord.size < 2 || (i && subset[i - 1].session === s.session)) return [r];
      const h = document.createElement("div");
      h.className = "sess-hdr c" + ((ord.get(s.session) % 4) + 1);
      h.textContent = s.session;
      return [h, r];
    }));
    updateBar(panesById[act]);
    flipIn(panesEl);
    return;
  }
  listFilter = null; // filter emptied out (e.g. last waiting pane answered) — card view
  dock(states, act); // sticky top bar — constant height, content swaps below it
  {
    // The deck is a positioning context: the pane's capture as background, the card
    // floating over its top (swipe ghosts overlay here too), ⤢ for the full view.
    const deck = document.createElement("div");
    deck.className = "deck";
    const a = panesById[act];
    if (a) {
      const fs = document.createElement("button");
      fs.className = "fsbtn";
      // Inline SVG, not the ⤢ glyph: the phone's font fallback renders the char with
      // odd metrics (tiny ink, wide advance), distorting the button — confirmed by
      // A/B on device. SVG renders identically everywhere.
      fs.innerHTML =
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"' +
        ' stroke-width="2" stroke-linecap="round" aria-hidden="true">' +
        '<path d="M14 4h6v6M20 4l-7 7M10 20H4v-6M4 20l7-7"/></svg>';
      fs.title = "Full screen";
      fs.setAttribute("aria-label", "Full screen");
      onTap(fs, () => openScreen(a.pane_id, a.title || a.label));
      deck.append(card(a), bgTerm(a), fs); // flex column: DOM order = visual order
    }
    panesEl.classList.add("cardmode"); // drops #panes' bar padding — see the CSS
    panesEl.replaceChildren(deck);
    joinTab(deck);
  }
  updateBar(panesById[act]);
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
  const sel = dockEl.querySelector(".dock-icon.sel");
  if (!sel) return;
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

// List filter: null = card view. Otherwise a one-liner list of matching panes, tapped from
// the header tallies: an ACTIVITY ("waiting"/"running"/"compacting"/"unknown"), "recent"
// (spans activities — see isRecent), or "all". No "idle": on a real deck most panes are
// idle, so that tally was the largest and least actionable number on the strip.
let listFilter = null;

// How long a pane must sit IDLE before the dock folds it away as parked.
//
// 10 minutes, chosen against how the deck is actually used: a pane you stepped away
// from mid-thought is back inside a couple of minutes, and the daemon's own activity
// classification settles within seconds — so anything past ten minutes is a pane you
// left, not a pane you're using. Long enough that a coffee break doesn't fold your
// work away; short enough that a 21-pane deck (measured: 16 of them idle for over an
// hour) collapses to the handful in play. Deliberately NOT tuned to the strip width:
// the fold means "you are done with this", and that shouldn't change with the viewport.
const PARKED_IDLE_SECS = 600;

// Is this pane still in play? Anything the agent is DOING (running/compacting) or that
// is WAITING on the user counts as recent no matter how old — a question that has gone
// unanswered for an hour is the most important thing on the deck, not the stalest.
// Only genuinely idle panes age out, so "recent" is really "not parked".
function isRecent(s) {
  return actOf(s) !== "idle" || stateDur(s) < PARKED_IDLE_SECS;
}

// Sessions whose parked panes the user expanded by tapping the fold chip. Module state
// so the expansion survives the rebuild every poll triggers (same reason as subsOpen).
// Keyed by session name: the fold is per-session, so expanding one leaves the rest folded.
const foldOpen = new Set();
// Panes whose list-row sub-agent box is expanded — module state, so the expansion
// survives the re-render every poll triggers (rows are rebuilt from scratch each time).
const subsOpen = new Set();
const dockEl = document.getElementById("dock");
const filtersEl = document.getElementById("filters"); // pane filters, homed in the header
function dock(states, act) {
  const el = dockEl;
  el.replaceChildren();
  // Card view only: the selected icon joins to the card below it (see .has-sel CSS).
  // In list mode there's no card under the dock, so no seam to open — and no fillets,
  // and no scroll re-pin handler (it closes over the dead card's nodes).
  const joined = el.classList.toggle("has-sel", !listFilter && states.some((s) => s.pane_id === act));
  if (!joined) {
    document.querySelectorAll(".tab-fillet").forEach((e) => e.remove());
    el.onscroll = null;
    el.classList.remove("edge-l");
  }
  // Chrome-tab-group-style session trays: each session's run of icons shares one
  // .dock-group span; the tray CSS paints a colored rail + session name above it.
  // The array is already in tmux session order, so a group is just "same session as
  // the previous icon". All tray chrome keys off the .grouped class toggled below —
  // single-session decks never get it, so nothing changes until sessions multiply.
  //
  // Parked panes (idle past PARKED_IDLE_SECS) fold to the END of their own session's
  // tray behind one "+N" chip, so the panes actually in play stay on screen. Why the
  // tray's tail and not in place: a session's parked panes are INTERLEAVED with its
  // live ones (measured on a real 21-pane deck: stale,stale,live,stale,live,live,live,
  // stale,stale,stale in one session), so folding each contiguous run in place yields
  // five chips scattered through the strip — 544px of content, still overflowing a
  // 364px phone strip, and the chip positions encode nothing the user thinks in (they
  // are an artifact of tmux window numbering). One chip per session reads as a fact —
  // "6 parked here" — and measured 412px, which fits all three sessions on screen at
  // 390px. See the PR for the reordering trade-off this accepts.
  let group = null, ngroups = 0;
  // Folding is a DOCK-ONLY view concern: `states` is never reordered or filtered, so the
  // list, the swipe carousel (which walks panesById's server order) and the tally badges
  // all still see tmux's own session/window/pane order. The dock trades strict order
  // WITHIN a session tray for density; nothing else does.
  const trays = new Map();  // session -> its .dock-group element (so the chip pass needs no re-query)
  const parked = new Map(); // session -> count of its parked panes
  // A tray must never fold to NOTHING. Measured on the real deck: one session had all four
  // of its panes parked, leaving a bare "+4" chip under a labelled rail — a session you can
  // see no state for at all, which is worse than one stale icon. So each session keeps its
  // freshest pane visible as a floor. Computed from the same duration the fold uses, so the
  // kept pane is genuinely the most recently active one.
  const freshest = new Map(); // session -> pane_id of its least-idle pane
  for (const s of states) {
    // ONE stateDur() call: it reads the clock, so calling it twice in the comparison both
    // doubles the work on a per-poll path and lets the compared value differ from the
    // stored one if the two reads straddle a second boundary.
    const k = s.session ?? "", cur = freshest.get(k), d = stateDur(s);
    if (!cur || d < cur.d) freshest.set(k, { id: s.pane_id, d });
  }
  // `unfolded` is a parameter, not a read of foldOpen, so a caller can ask the SAME question
  // about a hypothetically-closed tray without mutating shared state during render (the
  // chevron below needs exactly that). Defaults to the real state.
  const folded = (s, unfolded = foldOpen.has(s.session ?? "")) =>
    !isRecent(s) && !unfolded &&
    // The session's floor: keep its freshest pane on screen even when everything is parked.
    freshest.get(s.session ?? "")?.id !== s.pane_id &&
    // Never fold the SELECTED pane: in card view its icon IS the tab joined to the card
    // below, so folding it away would leave the card joined to nothing. This is also what
    // keeps the fold coherent with swiping: swipeNav walks the full server order (folded
    // panes included), so a swipe can land on a parked pane — and because `act` comes from
    // the one activeId() that dock() is handed, that pane is always un-folded back into
    // its tray. You can swipe into the parked set; the dock follows you there.
    s.pane_id !== act;
  for (const s of states) {
    if (!group || group.dataset.sess !== (s.session ?? "")) {
      group = document.createElement("span");
      group.className = "dock-group";
      group.dataset.sess = s.session ?? "";
      el.appendChild(group);
      trays.set(group.dataset.sess, group);
      ngroups++;
    }
    if (folded(s)) {
      const k = s.session ?? "";
      parked.set(k, (parked.get(k) || 0) + 1);
      continue;
    }
    const b = document.createElement("button");
    b.className = "dock-icon" + (s.pane_id === act ? " sel" : "");
    b.dataset.pane = s.pane_id;
    // Badge dot overlaps the logo's corner (like the favicon dot); idle panes get
    // none — quiet is the default, only busy states (running/waiting/compacting) earn a signal.
    const a = actOf(s);
    // Subscript count of RUNNING background sub-agents — a glanceable "3 workers busy
    // here", in the opposite corner from the activity dot (shared nsubOf coercion).
    const nsub = nsubOf(s);
    b.innerHTML = iconFor(s.tool) +
      (a === "running" || a === "waiting" || a === "compacting" ? `<i class="ddot d-${a}" aria-hidden="true"></i>` : "") +
      (nsub > 0 ? `<sub class="sacount" aria-hidden="true">${nsub}</sub>` : ""); // count is in aria-label below
    b.title = s.title || s.label || s.pane_id;
    // Fold the sub-agent count into the button's own label so assistive tech announces it.
    b.setAttribute("aria-label", b.title + (nsub > 0 ? `, ${nsub} sub-agent${nsub === 1 ? "" : "s"}` : ""));
    // Jump to that pane's CARD — including from list mode (a dock tap means "show
    // me this pane", not "re-highlight it inside the list").
    // onTap because the dock is rebuilt every poll and a plain `click` gets eaten; deferred
    // because .prow.dock is an overflow-x scroller, so a horizontal swipe to reach off-screen
    // panes must scroll rather than switch pane on touch-down.
    onTap(b, () => { listFilter = null; setActive(s.pane_id); }, true);
    group.appendChild(b);
  }
  // The fold chip at the tail of a session's tray: "+N" when its parked panes are hidden,
  // "‹" to hide them again. A <button> among the tray's icon <button>s, so it can't
  // disturb the group hue cycling (.dock-group:nth-of-type counts <span>s) nor the strip's
  // height (same 36px box as an icon — the join's geometry contract requires the tray add
  // zero height).
  // Tap handling is DEFERRED (acts on pointerup, cancels past slop) and pointer-captured;
  // see the block on the handler below for why both halves are needed.
  const foldChip = (sess, text, label, open) => {
    const g = trays.get(sess);
    if (!g) return;
    const c = document.createElement("button");
    c.className = "dock-fold" + (open ? " open" : "");
    c.textContent = text;
    c.title = label;
    c.setAttribute("aria-label", label);
    c.setAttribute("aria-expanded", String(open));
    // DEFERRED, and pointer-captured so deferral is safe here. Two requirements pull against
    // each other: the dock is an overflow-x scroller, so a horizontal drag starting on the
    // chip must scroll rather than toggle — that needs waiting for pointerup. But the dock is
    // rebuilt every poll, and a deferred listener bound to the node dies with it, which is
    // what made the chip dead to the touch (tap, nothing, tap again, then a rebuild put a
    // different icon under the thumb and switched panes).
    // setPointerCapture routes the remainder of THIS gesture to this element even after the
    // strip re-renders, so the tap survives the rebuild while slop still cancels a scroll.
    c.addEventListener("pointerdown", (e) => {
      if (e.button != null && e.button !== 0) return; // left/touch only
      try { c.setPointerCapture(e.pointerId); } catch { /* unsupported: plain deferral */ }
    });
    onTap(c, () => {
      // No captureIconRects(): that primes the list view's FLIP, and folding is a
      // DOCK-only concern — the list's rows are identical before and after, so priming
      // it only risks animating rows that did not move.
      if (open) foldOpen.delete(sess); else foldOpen.add(sess);
      render(Object.values(panesById));
    }, true);
    g.appendChild(c);
  };
  for (const [sess, n] of parked)
    foldChip(sess, "+" + n, `Show ${n} parked pane${n === 1 ? "" : "s"} in ${sess || "this session"}`, false);
  // An expanded tray gets a re-fold control, or unfolding is a one-way door for the session
  // (nothing re-parks it until a reload). Rendered as a small chevron (U+2039), NOT a
  // second full-size chip: a same-size button showing a bare glyph gave no clue what it did.
  // Only when something is actually parked — a session whose panes all went busy again has
  // nothing to re-fold, and a dead control would confuse.
  // Ask `folded()` itself — with the session's unfold temporarily discounted — rather than
  // re-deriving "is anything parked here". The hand-written version omitted the freshest-pane
  // FLOOR, so a session whose only parked pane is also its freshest got a chevron that would
  // hide nothing when tapped. Reusing the predicate means the control and the fold can never
  // disagree again.
  // Pass unfolded=false to ask "would this pane fold if the tray were closed?" — no
  // delete/add on foldOpen, so render stays free of side effects and Set insertion order
  // is untouched.
  const wouldFold = (sess) =>
    states.some((s) => (s.session ?? "") === sess && folded(s, false));
  for (const sess of foldOpen)
    if (wouldFold(sess))
      foldChip(sess, "\u2039", `Hide parked panes in ${sess || "this session"}`, true);
  // Tray chrome (rails + labels + the padding that hosts them) only when the deck
  // actually spans sessions — the CSS keys off this class, not group count.
  el.classList.toggle("grouped", ngroups > 1);
  // Drop unfold state for sessions that no longer exist, so killing and recreating a
  // session under the same name doesn't come back pre-expanded (and the set can't grow
  // without bound over a long-lived page) — same discipline as render()'s cache pruning.
  for (const sess of foldOpen) if (!trays.has(sess)) foldOpen.delete(sess);
  // Density + navigation: per-activity tallies homed in the header title bar (#filters) —
  // always visible no matter how many dock icons crowd the strip. Each one FILTERS the
  // list view to those panes; "all" lists everything.
  filtersEl.replaceChildren();
  const n = {};
  states.forEach((s) => (n[actOf(s)] = (n[actOf(s)] || 0) + 1));
  const filt = (label, key) => {
    const b = document.createElement("button");
    b.className = "badge b-" + key;
    b.textContent = label;
    // onTap because #filters is rebuilt every poll, so a rebuild between press and release
    // swallows a plain click — worst exactly when a pane is BUSY, since a changing pane makes
    // the deck version bump constantly. Deferred: #filters is an overflow-x scroller too.
    onTap(b, () => { captureIconRects(); listFilter = key; render(Object.values(panesById)); }, true);
    filtersEl.appendChild(b);
  };
  // "idle" is deliberately absent: on a real deck most panes are idle, so the number is
  // both the largest and the least actionable on the strip, and "N recent" below now
  // carries the half that matters. The idle panes are still reachable — "all" lists
  // everything, and a session's parked ones sit behind its "+N" chip.
  ["waiting", "running", "compacting", "unknown"].filter((a) => n[a]).forEach((a) => filt(`${n[a]} ${a}`, a));
  // "N recent" — the count the user actually wants at a glance: how much of the fleet is
  // live right now, which no single activity badge answers (a waiting pane and a running
  // pane are both recent). Same predicate the dock folds on, so the number and the strip
  // can never disagree. Shown only when it is informative: if every pane is recent it just
  // restates the deck size, and if none are the whole fleet is parked and "0 recent" adds
  // nothing an empty strip hasn't said.
  const nRecent = states.filter(isRecent).length;
  if (nRecent && nRecent < states.length) filt(`${nRecent} recent`, "recent");
  filt("all", "all");

  // With many panes the dock scrolls horizontally, and the selected icon can sit off
  // screen — its card then joins to a tab that isn't visible (looks severed). Center the
  // selected icon in the strip so it and its tab-join are always on screen. Deferred a
  // frame: the icons were just (re)appended, so their positions aren't laid out yet.
  // Gate on `joined` (card view AND the selected tab is joined to a card) — NOT just a
  // .sel, which also exists in list mode where there's no card to keep aligned. Only
  // scroll when the icon is actually clipped by the strip's edges — otherwise every
  // render (an activity tick, new events) would re-fire a scroll and fight the resting
  // position; when it's already fully visible we leave the dock where the user left it.
  const sel = joined && el.querySelector(".dock-icon.sel");
  if (sel) requestAnimationFrame(() => {
    if (!sel.isConnected) return; // a re-render in the same frame swept this icon
    const i = sel.getBoundingClientRect(), c = el.getBoundingClientRect();
    if (i.left < c.left || i.right > c.right)
      sel.scrollIntoView({
        inline: "center", block: "nearest",
        // Respect reduced-motion: jump instead of glide.
        behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      });
  });
}

// The list transition is a FLIP keyed on what actually changed between the two filter
// states. Captured at filter-tap time (before the re-render): the dock icons' rects
// (where ENTERING rows fly from) AND the currently-visible rows' rects (where SURVIVING
// rows slide from). Survivors are keyed off having a prior-row rect, so entering the list
// from card view (empty→list, no prior rows captured) stays a full icon-fly for every row.
let flipFrom = null; // pane_id -> dock-icon DOMRect (for entering rows)
let flipPrev = null; // pane_id -> { rect, node } of the outgoing row (survivors + leavers)
function captureIconRects() {
  flipFrom = {};
  dockEl.querySelectorAll(".dock-icon").forEach((b) => {
    if (b.dataset.pane) flipFrom[b.dataset.pane] = b.getBoundingClientRect();
  });
  flipPrev = {};
  // Clone each live row NOW: after replaceChildren the originals are gone, but a leaving
  // row must linger to animate out — the clone (positioned fixed at its old rect) does
  // that without fighting the re-render, mirroring the icon-fly's clone approach.
  panesEl.querySelectorAll(".prow").forEach((r) => {
    flipPrev[r.dataset.pane] = { rect: r.getBoundingClientRect(), node: r.cloneNode(true) };
  });
}
// FLIP delta as CENTER-to-center — a dock icon and a row icon are different sizes, so a
// top-left delta would land the clone offset by half their size difference. Survivors use
// this too (same-size rects, so it reduces to the plain delta) to keep one convention.
const cx = (r) => (r.left + r.right) / 2, cy = (r) => (r.top + r.bottom) / 2;
const flipDelta = (from, to) => `translate(${cx(to) - cx(from)}px,${cy(to) - cy(from)}px)`;
// Fly a clone from `from` to `to` and, if `fade`, opacity out — the one WAI engine shared
// by all three list cases. WAI not CSS-class transitions: keyframes take effect the moment
// they're created, so the start state actually paints (the class-toggle version lost the
// race and text popped in early). The clone keeps its OWN natural size (`box`, default the
// `from` rect — the leaver IS its from rect; the entrant icon passes its own smaller box so
// it isn't stretched to the dock icon's), CENTERED on `from`, then flown center→center.
function flyClone(node, from, to, fade, done, box = from) {
  Object.assign(node.style, {
    position: "fixed", left: cx(from) - box.width / 2 + "px", top: cy(from) - box.height / 2 + "px",
    width: box.width + "px", height: box.height + "px", margin: "0", zIndex: 30, pointerEvents: "none",
  });
  // A clone is throwaway chrome — never a control. Leaving-row clones are cloned .prow
  // nodes (role=button, tabIndex=0 from row()), so strip interactivity and hide from AT
  // before it enters the DOM, or a mid-flight clone becomes tab-focusable / announced.
  node.setAttribute("aria-hidden", "true");
  node.tabIndex = -1;
  node.removeAttribute("role");
  document.body.appendChild(node);
  // Idempotent teardown: onfinish and the safety timer race, but whichever fires first
  // clears the other so the clone is removed and `done` runs exactly once (same one-shot
  // guard style as the icon-fly's reveal). Interrupted flights still can't strand a clone.
  let timer;
  const end = () => { clearTimeout(timer); if (!node.isConnected) return; node.remove(); if (done) done(); };
  node.animate(
    [{ transform: "translate(0,0)", ...(fade && { opacity: 1 }) },
     { transform: flipDelta(from, to), ...(fade && { opacity: 0 }) }],
    { duration: 250, easing: "ease-out", fill: "forwards" }
  ).onfinish = end;
  timer = setTimeout(end, 400);
}
function flipIn(root) {
  if (!flipFrom) return;
  const from = flipFrom, prev = flipPrev;
  flipFrom = flipPrev = null;
  const now = new Set([...root.querySelectorAll(".prow")].map((r) => r.dataset.pane));
  // Leaving rows: in the old list but not the new. Fade+drift their clone out in place —
  // `to` is the same box nudged down 12px (all four edges, so the center delta is a clean
  // 12px drop). flipDelta reads left/right/top/bottom, so give it a full rect-like.
  for (const [id, p] of Object.entries(prev))
    if (!now.has(id)) {
      const r = p.rect;
      flyClone(p.node, r, { left: r.left, right: r.right, top: r.top + 12, bottom: r.bottom + 12 }, true);
    }
  root.querySelectorAll(".prow").forEach((r) => {
    const old = prev[r.dataset.pane];
    // SURVIVOR: already on screen in the old filter — no icon-fly, no invisibility. FLIP:
    // start it at its old position (First→Invert) and slide the delta to its new spot (Play).
    if (old) {
      r.animate(
        [{ transform: flipDelta(r.getBoundingClientRect(), old.rect) }, { transform: "translate(0,0)" }],
        { duration: 250, easing: "ease-out" }
      );
      return;
    }
    // ENTERING (or empty→list, where nothing was captured so every row lands here): fly
    // the pane's dock icon down to the row's icon spot. The row stays INVISIBLE until its
    // icon lands — reveal on finish, drawing the rest of the summary only then.
    const src = from[r.dataset.pane];
    const icon = r.querySelector(".icon");
    if (!src || !icon) return;
    r.style.opacity = "0";
    const dst = icon.getBoundingClientRect(); // the clone is icon-sized, so freeze its box to this
    flyClone(icon.cloneNode(true), src, dst, false, () => {
      if (r.style.opacity !== "0") return;
      r.style.opacity = "";
      r.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 100 });
    }, dst);
  });
}

function row(s, act) {
  const a = actOf(s);
  const el = document.createElement("div");
  el.className = "prow" + (a === "waiting" ? " waiting" : "")
    + (s.pane_id === act ? " sel" : "");
  el.dataset.pane = s.pane_id;
  // Tapping a row opens that pane's card (drops back out of list view). Structure:
  // TWO SIBLING buttons, never nested — role=button on the row itself would be invalid
  // ARIA once the sub-agents toggle (a real <button>) moved in. The icon+name+headline
  // area becomes .row-open, a real button that opens the card (keyboard operable);
  // the toggle sits beside it in .ph-right. The row div stays a pointer target so
  // taps on its padding still open the card.
  const goCard = () => { listFilter = null; setActive(s.pane_id); };
  // defer: rows are rebuilt every poll (so `click` alone loses taps) but they also sit in
  // vertically-scrolling #panes, so navigation waits for a pointerup that didn't scroll.
  onTap(el, goCard, true);
  el.innerHTML = paneHeader(s, { icon: true });
  const openBtn = document.createElement("button");
  openBtn.className = "row-open";
  openBtn.append(...[...el.children].filter((n) => !n.classList.contains("ph-right")));
  el.prepend(openBtn);
  // stopPropagation covers the KEYBOARD path — onTap already swallows the pointer's click,
  // but an AT-synthesized click on the button would still bubble to the row and re-fire.
  onTap(openBtn, (e) => { e.stopPropagation(); goCard(); }, true); // same scroll container
  // A pane with sub-agents gets a labeled chip under the activity badge (reusing the
  // card meta's .chip.agents look) that toggles the SAME subagentsView box the card
  // shows — one component, two surfaces. Toggle state lives in subsOpen so it survives
  // the per-poll row rebuild; toggling re-renders through render(), the one normal
  // path, not a bespoke DOM patch that drifts from it.
  const subs = realSubs(s.subagents); // renderable entries only — same filter the box uses,
  if (subs.length) {                  // so the chip's count can never disagree with it
    const open = subsOpen.has(s.pane_id);
    const t = document.createElement("button");
    t.className = "badge sub-toggle"; // same pill as the activity badge, agents purple
    // ◂ when closed (at the row's right edge a ▸ reads as "navigate", not "expand")
    t.textContent = `${subs.length} sub-agent${subs.length === 1 ? "" : "s"} ${open ? "▾" : "◂"}`;
    t.setAttribute("aria-expanded", String(open));
    onTap(t, (e) => {
      e.stopPropagation(); // a toggle tap expands the box — it must not open the card
      subsOpen.has(s.pane_id) ? subsOpen.delete(s.pane_id) : subsOpen.add(s.pane_id);
      render(Object.values(panesById));
    }, true); // defer: inside the vertical scroller
    el.querySelector(".ph-right").append(t);
    if (open) { el.classList.add("subs-open"); el.appendChild(subagentsView(subs)); }
  }
  return el;
}


// Horizontal swipe on the active card switches panes (tmux window order, wraps), as a
// carousel: the NEIGHBOR card slides in alongside your finger (so it reads as paging,
// not dismissal), both animate home on commit, short/vertical drags snap back.
// Touches starting in a horizontal scroller (tables) keep their native gesture.
function swipeNav(el, id) {
  let sx = null, sy = null, dx = 0, ghost = null, gdir = 0;
  const ids = () => Object.keys(panesById); // insertion order = server (tmux) order
  const neighbor = (dir) => { // dir -1: swiping left reveals the NEXT pane; +1: previous
    const a = ids(), i = a.indexOf(id);
    return a[(i + (dir < 0 ? 1 : a.length - 1)) % a.length];
  };
  const W = () => el.offsetWidth + 12; // card width + gap
  const clear = () => { if (ghost) ghost.remove(); ghost = null; gdir = 0; };
  el.addEventListener("touchstart", (e) => {
    sx = e.target.closest(".tbl-scroll, .bg-wrap") ? null : e.touches[0].clientX;
    sy = e.touches[0].clientY; dx = 0; clear();
  }, { passive: true });
  el.addEventListener("touchmove", (e) => {
    if (sx == null) return;
    dx = e.touches[0].clientX - sx;
    if (Math.abs(dx) <= Math.abs(e.touches[0].clientY - sy) || Math.abs(dx) <= 10) return;
    busyGesture = true; // freeze poll re-renders mid-drag — they'd replace the card under the finger
    el.style.transition = "none"; // track the finger 1:1, no easing lag
    el.style.transform = `translateX(${dx}px)`;
    const dir = dx < 0 ? -1 : 1;
    if (dir !== gdir && ids().length > 1) {
      clear(); gdir = dir;
      ghost = card(panesById[neighbor(dir)]);
      ghost.classList.add("ghost");
      ghost.style.transition = "none";
      el.parentElement.appendChild(ghost);
    }
    if (ghost) ghost.style.transform = `translateX(${dx - gdir * W()}px)`;
  }, { passive: true });
  el.addEventListener("touchend", (e) => {
    if (sx == null) return;
    const dy = e.changedTouches[0].clientY - sy;
    sx = null;
    el.style.transition = "";
    if (ghost) ghost.style.transition = "";
    if (Math.abs(dx) < 70 || Math.abs(dx) < 2 * Math.abs(dy) || ids().length < 2) {
      el.style.transform = ""; // snap back, neighbor retreats
      if (ghost) { ghost.style.transform = `translateX(${-gdir * W()}px)`; setTimeout(clear, 160); }
      busyGesture = false;
      return;
    }
    const dir = dx < 0 ? -1 : 1;
    el.style.transform = `translateX(${dir * W()}px)`;
    if (ghost) ghost.style.transform = "translateX(0)";
    setTimeout(() => { busyGesture = false; setActive(neighbor(dir)); }, 150);
  });
  // A cancelled gesture (OS interruption) must release the poll freeze and snap back,
  // or polling stays frozen indefinitely.
  el.addEventListener("touchcancel", () => {
    sx = null; busyGesture = false;
    el.style.transition = "";
    el.style.transform = "";
    if (ghost) { ghost.style.transform = `translateX(${-gdir * W()}px)`; setTimeout(clear, 160); }
  });
}

function card(s) {
  const el = document.createElement("div");
  const collapsed = cardsCollapsed;
  el.className = "card" + (actOf(s) === "waiting" ? " waiting" : "")
    + (s.pane_id === activeId() ? " active" : "") + (collapsed ? " collapsed" : "")
    + (isReparsing(s) ? " reparsing" : ""); // input sent, awaiting the forced re-parse
  swipeNav(el, s.pane_id);
  // Tapping a card makes it the target of the single bottom input bar.
  onTap(el, (e) => {
    if (e.target.closest("button, input, a, summary, details")) return; // don't steal option/timeline taps
    setActive(s.pane_id);
  }, true); // defer: the card scrolls, so only a tap that didn't drag counts

  const row = document.createElement("div");
  row.className = "row";
  // Shared header layout (see paneHeader). The card adds the collapse caret and omits
  // the icon — its dock tab above IS the icon. The ▾/▸ caret collapses the card to just
  // this header row (still tab-joined), handing the live terminal the screen; collapse
  // state is view-wide (cardsCollapsed) so swiping panes keeps the chosen height.
  row.innerHTML = paneHeader(s, { caret: true, collapsed, icon: false, link: true });
  onTap(row.querySelector(".card-caret"), (e) => {
    e.stopPropagation(); // don't also re-select the pane
    cardsCollapsed = !collapsed;
    render(Object.values(panesById));
  }, true); // defer: inside a vertical scroller — a scroll must not fire it
  el.appendChild(row);
  if (collapsed) return el; // one-line form: header only, everything below is hidden

  // While a voice session runs, Live Mode owns the active card: everything below the
  // header is the live interface — the rolling conversation with every typed action —
  // in place of the pane's summary, question, and event views.
  if (lmWs && s.pane_id === activeId()) {
    el.appendChild(lmConvoView());
    return el;
  }

  // The bootstrap "story so far" — orientation when picking a session up cold.
  // Clamped to a few lines; tap toggles the full text.
  if (s.session_summary) {
    const sum = document.createElement("div");
    sum.className = "sess-sum";
    sum.innerHTML = linkifyText(s.session_summary); // linkifyText escapes non-anchors
    onTap(sum, (e) => {
      e.stopPropagation();
      // The summary is linkified now, so it can contain anchors. A tap on one must NAVIGATE
      // only — toggling as well would collapse the text out from under the user as the link
      // opens, and on a target that is also the expand affordance that reads as a glitch.
      if (e.target.closest("a")) return;
      sum.classList.toggle("open");
    }, true); // defer: card scroller
    el.appendChild(sum);
  }

  if (s.rewind) el.appendChild(rewindView(s));
  // Tables render BEFORE the question so they act as context above the options.
  if (Array.isArray(s.tables)) s.tables.forEach((t) => el.appendChild(tableView(t)));
  if (s.question) el.appendChild(question(s));
  if (Array.isArray(s.tasks) && s.tasks.length) el.appendChild(tasksView(s.tasks));
  { const subs = realSubs(s.subagents); if (subs.length) el.appendChild(subagentsView(subs)); }
  if (Array.isArray(s.links) && s.links.length) el.appendChild(linksView(s.links));
  if (Array.isArray(s.copyables) && s.copyables.length) el.appendChild(copyView(s.copyables));
  const log = (eventLog[s.pane_id] || {}).events || [];
  if (log.length) el.appendChild(eventsView(log, s.pane_id, s.summary));
  // No per-card input anymore — a single persistent bar at the bottom of the page
  // handles text/keys/images for whichever card is active (see the #bar element).
  return el;
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
  // NEGATIVE, and no longer containing --bar-h: that term was cancelling the deck's own
  // overhang past the bar, not tucking anything. The deck now ends AT the bar, so keeping
  // --bar-h here cut a whole bar's height out of the visible peek. What's left is the real
  // job — pull the wrap's bottom up by exactly the chrome rows we want hidden.
  wrap.style.marginBottom = `${-(rows ? Math.round(rows * lineH) + 6 : 60)}px`;
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

function bgTerm(s) {
  // Wrapper = the visible window (starts right below the card, ends near the bar);
  // the trimmed capture is top-anchored inside it, scrolled to its tail when longer.
  // So a 1-line shell prompt sits right under the card instead of drowning in the
  // blank lines tmux pads the capture with.
  const wrap = document.createElement("div");
  // "shell" here means "no status chrome at the bottom of the capture" — agents get
  // their chrome tucked behind the bar, shells keep their prompt visible above it.
  // (Don't key this on LOGOS: shell has a logo too now.)
  wrap.className = "bg-wrap" + (AGENT_TOOLS.has(s.tool) ? "" : " shell");
  wrap.dataset.pane = s.pane_id; // the ResizeObserver keys zHome off the OWNING pane
  const box = document.createElement("pre");
  box.className = "bg-term";
  wrap.appendChild(box);
  const toEnd = () => { wrap.scrollTop = wrap.scrollHeight; };
  const paint = (html) => {
    box.innerHTML = html;
    tuckChrome(wrap, box);
    if (zHome(s.pane_id)) toEnd();
  };
  // Instant frame on (re)mount from cache. Its GRAY/live state must reflect the
  // ACTUAL stream health (peekLive), not a blanket "stale": on a same-pane re-render
  // (every 2s) the stream is already connected, so a fresh wrap must NOT flash gray
  // and then wait up to a full 25s hold for the next onLive to clear it. A pane never
  // viewed this session has no cache — "(connecting…)" until the first frame.
  const c = peekCache[s.pane_id];
  if (c) { paint(c.html); if (!peekLive) wrap.classList.add("stale"); }
  else box.textContent = "(connecting…)";
  // The peek streams for the ACTIVE pane. render() rebuilds the deck every poll, so
  // the stream is NOT re-created per render (that would restart the long-poll hold
  // each time) — it's keyed to the pane and its callbacks target the CURRENT elements
  // via the module-level peekBox refs, which each bgTerm updates. Restart only when
  // the streamed pane actually changes.
  peekBox = box; peekWrap = wrap;
  // While the fullscreen overlay owns this pane's one stream, the peek stands down —
  // otherwise the 2s render() poll would start a SECOND concurrent stream here. The
  // peek re-mounts on the next poll after the overlay closes (screenOpen back to false).
  if (screenOpen) { if (peekStop) { peekStop(); peekStop = null; peekStreamPane = null; } }
  else if (peekStreamPane !== s.pane_id) {
    peekStop && peekStop();
    peekStreamPane = s.pane_id;
    const streamPane = s.pane_id; // captured: ignore late frames after a pane switch
    peekStop = liveStream(streamPane, {
      onFrame: (txt) => {
        // A response that resolved just as the user switched panes must not paint or
        // cache into the new pane (the module-level peek* refs now point elsewhere).
        if (streamPane !== peekStreamPane) return;
        const html = renderCapture(txt.replace(/\s+$/, ""), { color: true });
        peekCache[streamPane] = { html }; // cache for the next stale-on-mount
        // ALWAYS paint the newest frame — dropping it while busy would strand it: the
        // stream advances its hash regardless, so the next poll returns "no change" and
        // this frame would never render. Only the scroll-to-tail is suppressed during a
        // gesture (that's what fights the user's finger). Diff-skip avoids the reflow
        // when the content is actually identical (the flicker source).
        // NEVER repaint while the user is typing or selecting. An innerHTML swap tears
        // down and rebuilds the subtree, which collapses the document Selection — and on
        // a BUSY pane these frames arrive continuously, so every attempt to place the
        // caret in the composer was wiped within milliseconds. That is the "I can't even
        // tap into the input box on a busy pane; I have to switch panes and back" bug
        // (switching panes stops this stream, which is why it appeared to fix it).
        // The fullscreen path already guarded its own selection this way; the peek didn't.
        // Frames keep coming, so a skipped paint self-corrects on the next one.
        // Only skip a paint while the caret is being PLACED or a selection is live, not
        // for the whole time the composer holds focus — the terminal must keep streaming
        // while you type. `_caretGrace` is stamped on composer pointerdown/focus, so the
        // guard covers the vulnerable window (the tap and the moments after it) and then
        // expires; a live selection is honored for as long as it exists.
        const sel = document.getSelection();
        const selecting = sel && !sel.isCollapsed
          && (peekBox?.contains(sel.anchorNode) || bar.input?.contains(sel.anchorNode));
        if (selecting || Date.now() - _caretGrace < 1200) return;
        if (peekBox && peekBox.innerHTML !== html) {
          peekBox.innerHTML = html;
          tuckChrome(peekWrap, peekBox);
          if (!isBusy() && zHome(streamPane)) peekWrap.scrollTop = peekWrap.scrollHeight;
        }
      },
      onLive: () => { if (streamPane === peekStreamPane) { peekLive = true; peekWrap && peekWrap.classList.remove("stale"); } },
      onQuiet: () => { if (streamPane === peekStreamPane) { peekLive = false; peekWrap && peekWrap.classList.add("stale"); } },
    });
  } else if (peekLive) {
    wrap.classList.remove("stale"); // same-pane remount, stream already live
  }
  // Desktop has no pan gesture — dragging a text selection auto-scrolls the window
  // sideways with nothing to bring it home (touch pans go through pinchZoom's clamp).
  // Ease scrollLeft back once the drag settles.
  let scrollIdle;
  wrap.addEventListener("scroll", () => {
    if (!wrap.scrollLeft) return;
    clearTimeout(scrollIdle);
    scrollIdle = setTimeout(() => wrap.scrollTo({ left: 0, behavior: "smooth" }), 500);
  });
  pinchZoom(wrap, box, (bgZoom[s.pane_id] ||= { scale: 1, tx: 0, ty: 0 }), true);
  // ALWAYS pin fresh elements to the tail — this is the coordinate BASELINE the
  // persisted pan/zoom transform overlays (each rebuild starts at scrollTop 0; a
  // panned user's offset is relative to the tail, so skipping this would show the
  // buffer TOP through their transform). The zHome guards above apply only to
  // CONTENT updates on an existing element, where moving the scroll is a yank.
  requestAnimationFrame(toEnd);
  // The window's height changes after we pin the scroll (the card above grows as
  // events/images render, flex re-settles) — each change slid the view off the tail,
  // half-clipping the last line. Re-pin whenever the wrap is resized.
  if (peekPrev) peekRO.unobserve(peekPrev);
  peekRO.observe(wrap);
  peekPrev = wrap;
  return wrap;
}
// ONE shared observer for every peek window; each render explicitly unobserves the
// pane's previous (now detached) wrap, so tracked targets stay bounded at one per pane.
const peekRO = new ResizeObserver((entries) =>
  entries.forEach((e) => {
    // The wrap's OWN pane, not activeId() — a pending selection can diverge from
    // the rendered wrap and re-pin a pane the user has deliberately panned.
    if (zHome(e.target.dataset.pane)) e.target.scrollTop = e.target.scrollHeight;
  }));
let peekPrev = null; // the one previously observed wrap (only one is in the DOM at a time)

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
function linksView(links) {
  const box = document.createElement("div");
  box.className = "links";
  const valid = links.filter((l) => {
    if (!l || !l.href || !/^https?:\/\//i.test(l.href)) return false;
    try { new URL(l.href); return true; } catch { return false; }  // pre-cap: malformed can't eat slots
  });
  for (const l of valid.slice(0, 3)) {
    // The label is MODEL OUTPUT derived from untrusted pane content — a hostile pane
    // can suggest a reassuring label for a phishing URL. Always show the destination
    // host next to the label so the user sees where the tap goes; cap the label so a
    // hostile pane can't bury the card's actionable UI under a wall of text.
    const host = new URL(l.href).host;
    const a = document.createElement("a");
    a.className = "linkbtn";
    a.href = l.href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    const label = safeText(l.text, 80);  // untrusted: bidi-stripped, code-point capped
    // Lucide icon, not the 🔗 emoji: AGENTS.md bans emoji as UI chrome (emoji ignore
    // currentColor, so they can't theme, and they render differently per device). The
    // label is untrusted, so it rides in its own span as textContent — never innerHTML.
    const licn = document.createElement("span");
    licn.className = "linkicon";
    licn.innerHTML = licon("link", 13);
    const ltxt = document.createElement("span");
    ltxt.textContent = label || host;
    a.append(licn, ltxt);
    const hostEl = document.createElement("span");
    hostEl.className = "linkhost";
    hostEl.textContent = ` ${host}`;
    a.appendChild(hostEl);
    a.onclick = (e) => e.stopPropagation();
    box.appendChild(a);
  }
  return box;
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
function copyView(items) {
  const box = document.createElement("div");
  box.className = "copyables";
  const valid = items.filter((c) => c && typeof c.text === "string" && c.text.trim());
  for (const c of valid.slice(0, 3)) {
    const b = document.createElement("button");
    b.className = "copybtn";
    // Label is MODEL OUTPUT derived from untrusted pane content: strip bidi controls
    // (an unterminated override would visually reorder the row) and cap by code points
    // so a hostile pane can't bury the card under a wall of text. textContent, never
    // innerHTML. Falls back to a generic name when the model gave no usable label.
    const label = safeText(c.label, 60) || "Text";
    // Icon is chrome (inline Lucide, themes via currentColor — AGENTS.md bans emoji
    // here); the label is untrusted, so it rides in its own span as textContent and
    // never touches innerHTML. Swap to a check when the copy lands.
    const icon = document.createElement("span");
    icon.className = "copyicon";
    icon.innerHTML = licon("clipboard", 14);
    // Preview: the payload's own first line, so the row says what it actually holds.
    // Newlines collapse to a pilcrow-ish separator (a multi-line commit message must
    // read as one line here) and runs of grid whitespace collapse so terminal padding
    // doesn't eat the preview. Clipped by CSS, not here — the full text stays on the
    // clipboard. Sliced generously (200) before CSS ellipsis so a hostile payload
    // can't cost real layout work.
    // safeText for the same reason the label gets it, and MORE so: this is the payload
    // itself, so a bidi control here could make the row read as different content than
    // the clipboard actually carries. Display only — c.text stays verbatim for the copy.
    const preview = safeText(c.text.replace(/\s*\n+\s*/g, " · ").replace(/\s{2,}/g, " "), 200);
    const text = document.createElement("span");
    text.className = "copylabel";
    const prev = document.createElement("span");
    prev.className = "copyprev";
    prev.textContent = preview;
    const lines = document.createElement("span");
    lines.className = "copylines";
    lines.append(text, prev);
    const set = (t, done = false) => {
      icon.innerHTML = licon(done ? "check" : "clipboard", 14);
      text.textContent = t;
    };
    set(label);
    b.append(icon, lines);
    let revert = 0;
    // onTap(defer), not onclick: the card is rebuilt on the ~2s poll, so a plain `click` is
    // lost whenever a rebuild lands between finger-down and finger-up — the same swallowed-tap
    // bug the dock had, and worse here because the payload is the whole point of the row.
    // Deferred to pointerup: the card scrolls, so a scroll flick must not copy — and pointerup
    // still carries the transient user activation that BOTH clipboard paths need
    // (navigator.clipboard.writeText and the execCommand fallback alike).
    onTap(b, async (e) => {
      e.stopPropagation(); // copying must not also re-select the pane
      const ok = await copyText(c.text);
      // The card never shows the payload, so a failure has to send the user to where the
      // text actually is — the pane itself — not to a "text" that isn't on screen.
      set(ok ? "Copied" : "Copy failed — select it in the pane", ok);
      b.classList.toggle("copied", ok);
      // Revert so the row keeps naming its content (and a second copy is obvious). Clear
      // the pending timer first: on a rapid second tap the older one would otherwise fire
      // mid-confirmation and blank the state early.
      clearTimeout(revert);
      revert = setTimeout(() => { set(label); b.classList.remove("copied"); }, 1600);
    }, true);
    box.appendChild(b);
  }
  return box;
}

// Render structured table data as a real HTML table in a box that scrolls both ways
// (horizontal for wide tables, vertical for tall ones) — from the parser's `tables`.
function tableView(t) {
  const box = document.createElement("div");
  box.className = "tablewrap";
  const head = (t.headers || []).map((h) => `<th>${esc(h)}</th>`).join("");
  const body = (t.rows || [])
    .map((r) => `<tr>${(r || []).map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`)
    .join("");
  box.innerHTML =
    (t.title ? `<div class="tbl-title">${esc(t.title)}</div>` : "") +
    `<div class="tbl-scroll"><table>${head ? `<thead><tr>${head}</tr></thead>` : ""}<tbody>${body}</tbody></table></div>`;
  return box;
}

// The activity feed: "what the thing did". Each event's `text` is the primary line;
// optional metadata (a file diff, or a `meta` string) renders as a small, muted,
// right-justified side-note. A file edit is just an event whose metadata is a diff.
function evHtml(e) {
  let note = "";
  if (e.file) {
    const add = e.file.added ? `<span class="add">+${e.file.added}</span>` : "";
    const del = e.file.removed ? `<span class="del">-${e.file.removed}</span>` : "";
    note = `<span class="ev-note ev-file">${esc(e.file.path || "")} ${add}${del}</span>`;
  } else if (e.meta) {
    note = `<span class="ev-note">${esc(e.meta)}</span>`;
  }
  // historical = reconstructed from scrollback by the bootstrap pass, not observed
  // live — rendered dimmer so it never masquerades as watched fact.
  // linkifyText, not esc: an event that mentions a URL or a markdown [label](url) — a PR
  // the agent opened, a preview deploy — should be tappable here exactly as it is in the
  // terminal below. It escapes everything it doesn't turn into an anchor, so it is a safe
  // drop-in for esc() on this untrusted model text.
  return `<div class="ev${e.historical ? " ev-hist" : ""}"><span class="ev-text">${linkifyText(e.text || "")}</span>${note}</div>`;
}

function eventsView(events, paneId, summary) {
  const box = document.createElement("div");
  box.className = "events";
  box.dataset.pane = paneId || "";
  // When the pane went idle and the server summarized a burst of `count` events,
  // collapse the OLDEST `count` events under a summary line (expandable). We fold by
  // count, not timestamp, to avoid client-ms vs server-sec clock skew — the log is
  // time-ordered so the oldest N are the summarized burst.
  let head = "";
  let rest = events;
  if (summary && summary.text && summary.count > 1 && events.length > summary.count) {
    const folded = events.slice(0, summary.count);
    rest = events.slice(summary.count);
    const open = openTimelines.has(paneId + ":sum");
    head =
      `<details class="ev-summary"${open ? " open" : ""} data-sum="${esc(paneId)}">` +
      `<summary>▤ ${esc(summary.text)} <span class="dim">(${summary.count})</span></summary>` +
      folded.map(evHtml).join("") +
      `</details>`;
  }
  box.innerHTML = head + rest.map(evHtml).join("");
  // Newest events are at the bottom. The card re-renders every poll, so after it's in
  // the DOM, restore the user's scroll — but if they were at (or near) the bottom,
  // stick to the bottom so new activity stays in view. Scrolling up to read history
  // is preserved and won't get yanked back down.
  const prev = eventScroll[paneId];
  requestAnimationFrame(() => {
    if (prev == null || prev.atBottom) box.scrollTop = box.scrollHeight;
    else box.scrollTop = prev.top;
  });
  box.addEventListener("scroll", () => {
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    eventScroll[paneId] = { top: box.scrollTop, atBottom };
  });
  const det = box.querySelector("details.ev-summary");
  if (det) det.ontoggle = () =>
    det.open ? openTimelines.add(paneId + ":sum") : openTimelines.delete(paneId + ":sum");
  return box;
}
const eventScroll = {}; // pane_id -> {top, atBottom} to preserve scroll across re-renders

// Task/TODO checklist the agent is tracking (done vs open) — from parser JSON tasks[].
function tasksView(tasks) {
  const box = document.createElement("div");
  box.className = "tasks";
  box.innerHTML =
    `<div class="tasks-head">Tasks</div>` +
    tasks
      .map(
        (t) =>
          `<div class="task${t.done ? " done" : ""}">` +
          `<span class="tick">${t.done ? "✓" : "○"}</span>${esc(t.text || "")}</div>`
      )
      .join("");
  return box;
}

// Background sub-agents this agent spawned (parser JSON subagents[]) — distinct from the
// agent's own TODO tasks above. Shares the .tasks box chrome; a running worker gets a
// pulse dot, a finished one a check, and any elapsed/tokens meter rides on the right.
// Renderable entries only (match classify.py: dicts only) — ONE definition, shared by
// the renderer and by row()'s toggle count so the label can never disagree with the box.
const realSubs = (subs) =>
  (Array.isArray(subs) ? subs : []).filter((a) => a && typeof a === "object" && !Array.isArray(a));

function subagentsView(subs) {
  const box = document.createElement("div");
  box.className = "tasks subagents";
  box.innerHTML =
    `<div class="tasks-head">Sub-agents</div>` +
    subs
      .map((a) => {
        const done = a.state === "done";
        const meter = [a.elapsed, a.tokens && "↓" + a.tokens].filter(Boolean).map(esc).join(" ");
        return (
          `<div class="task${done ? " done" : ""}">` +
          `<span class="tick">${done ? "✓" : '<span class="pulse"></span>'}</span>` +
          `<span class="sa-label">${esc(a.label || "")}</span>` +
          (meter ? `<span class="worksub">${meter}</span>` : "") +
          `</div>`
        );
      })
      .join("");
  return box;
}

// Render Claude Code's Esc-Esc Rewind picker as a scrollable history list. The ❯
// entry is highlighted; ↑/↓ (the key buttons, which move the real terminal cursor)
// scroll the selection and the card reflects it each poll; Enter restores. A tap on a
// row is a convenience: send ↑ to reveal more when it's the top "N more above" marker.
// Colorize a Rewind note's diff stats: "+58" green, "-3" red; other text dim.
function diffStat(note) {
  return esc(note).replace(/([+-]\d+)/g, (m) =>
    `<span class="${m[0] === "+" ? "add" : "del"}">${m}</span>`
  );
}

function rewindView(s) {
  const box = document.createElement("div");
  box.className = "rewind";
  const rows = s.rewind.entries
    .map(
      (e) =>
        `<div class="rw-entry${e.selected ? " sel" : ""}">` +
        `<span class="rw-cursor">${e.selected ? "❯" : ""}</span>` +
        `<span class="rw-text">${esc(e.text)}</span>` +
        (e.note ? `<span class="rw-note">${diffStat(e.note)}</span>` : "") +
        `</div>`
    )
    .join("");
  box.innerHTML =
    `<div class="rw-head">⟲ Rewind — restore to a previous point` +
    (s.rewind.more_above ? ` <span class="rw-more">↑ ${s.rewind.more_above} more above</span>` : "") +
    `</div>${rows}` +
    `<div class="rw-hint">Use ↑ / ↓ to move, Enter to restore, Esc to cancel</div>`;
  return box;
}

// Compact metadata chips: model, context bar, cost, mode badge, agent count. Shown in
// the bottom bar (below the input) for the ACTIVE pane only, not on every card. Only
// renders the chips that have values, so a plain shell shows nothing here.
const MODE_LABEL = { plan: "plan", "accept-edits": "accept edits", bypass: "bypass perms" };
function metaChips(s) {
  const chips = [];
  if (s.model) chips.push(`<span class="chip">${esc(s.model)}</span>`);
  if (s.context_pct != null)
    chips.push(
      `<span class="chip ctxchip"><i style="width:${s.context_pct}%"></i>${s.context_pct}% ctx</span>`
    );
  if (s.cost) chips.push(`<span class="chip">${esc(s.cost)}</span>`);
  // Generic status-line entries the parser surfaced (usage-limit %, queue depth, …):
  // one chip each, no schema change per metric. LLM output — a non-array (e.g. a bare
  // string) would otherwise .slice() into characters.
  const entries = Array.isArray(s.status_entries) ? s.status_entries : [];
  for (const t of entries.slice(0, 4))
    if (t && String(t).trim()) chips.push(`<span class="chip">${esc(t)}</span>`);
  if (s.mode && s.mode !== "normal" && s.mode !== "unknown")
    chips.push(`<span class="chip mode mode-${s.mode}">${MODE_LABEL[s.mode] ?? s.mode}</span>`);
  if (s.agents > 0) chips.push(`<span class="chip agents">⛓ ${s.agents} agents</span>`);
  return chips.join("");
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
  bar.input.dataset.placeholder = label;
  bar.input.setAttribute("aria-label", label);
  bar.meta.innerHTML = s ? metaChips(s) : "";
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
  // onTap, not onclick: on a touchscreen the browser withholds `click` entirely if the
  // finger drifts a few pixels between touchstart and touchend (easy on a phone, and on
  // a soft-keyboard layout that shifts under your thumb). A withheld click is a Send that
  // silently does nothing — the single worst failure this app can have, since the user's
  // typed message just sits there. pointerdown always fires, and submitComposer is
  // already re-entrancy-guarded by `sending`, so committing on touch-down is safe.
  onTap(bar.send, () => submitComposer(activeState()));
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
let sending = false; // a send is IN FLIGHT — distinct from the render freeze (see submitComposer)

const _sendQueue = []; // taps that arrived while a send was in flight (never dropped)
// One Enter can fire submitComposer twice (keydown AND beforeinput — see the guard below).
// Re-entry inside this window is that echo, not a second message.
const SUBMIT_DEDUPE_MS = 250;
let _lastSubmitAt = 0;

async function submitComposer(s, presetSegs) {
  // Guard on `sending`, NOT the render freeze. That freeze is also set by swipes, pinch/drag
  // gestures and option taps to freeze poll re-renders, so guarding on it made Send do
  // nothing — silently — whenever a gesture had it held (a swipe keeps it for 150ms after
  // release). "Sometimes the send button just does nothing" was that. `sending` is owned by
  // this function alone, so it means only what it says: a send is in flight. It no longer
  // doubles as the double-fire guard — the dedupe window below does that, because `sending`
  // now queues instead of returning and a queued echo is a message sent twice.
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
  busySend = true; // also freeze poll re-renders while the composer is mid-flush
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
    // Button un-spins NOW (the work is done); the freeze holds a beat longer so the poll
    // doesn't repaint mid-settle.
    bar.send?.classList.remove("sending");
    bar.send && (bar.send.disabled = false); // clear any legacy disabled state
    releaseBusySoon();
  }
}

// Let the render-freeze lapse a beat after a send settles, so the poll doesn't repaint
// mid-settle. Deliberately NOT an unconditional clear: a queued send starts on a
// 0ms timer, so the previous send's 400ms timer would land squarely inside the next one and
// unfreeze the deck while it was still in flight — replacing the card under the user, which
// is exactly what the freeze exists to prevent. Whoever is still sending owns the flag and
// will schedule its own release; this one just stands down.
//
// It touches busySend ONLY, so it can no longer end a swipe's or pinch's freeze — a send
// settling mid-gesture used to flip the one shared flag and let a render land under the
// finger, which is the same failure by a different route.
function releaseBusySoon() {
  setTimeout(() => { if (!sending) busySend = false; }, 400);
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

function question(s) {
  const q = document.createElement("div");
  q.className = "q";
  const spinning = isReparsing(s); // answer submitted — options locked, spinner shown
  const prompt = document.createElement("div");
  prompt.className = "prompt";
  prompt.textContent = s.question.prompt;
  if (spinning) {
    // Built as a DOM node (not innerHTML +=) so the escaped prompt text isn't reparsed
    // as HTML each render. Negative animation-delay = (Date.now() mod period): a freshly
    // -created element's CSS animation always starts at 0°, and render() rebuilds the
    // card every fast reparse-poll (~500ms < the 0.7s spin), so a plain spinner kept
    // snapping back to the first quarter-turn. Seeding the delay to the current phase
    // makes each rebuilt spinner RESUME where the last frame left off — one smooth spin.
    const spin = document.createElement("span");
    spin.className = "q-spin";
    spin.setAttribute("role", "status");
    spin.setAttribute("aria-label", "submitting");
    spin.style.animationDelay = `${-((Date.now() % 700) / 1000)}s`;
    prompt.append(" ", spin);
  }
  q.appendChild(prompt);

  // Option buttons (drop any "type something"/"Other" pseudo-option — the bottom bar
  // covers free-text). Tapping an option also makes this pane active, then answers it.
  // Once an answer is in flight (spinning) the options disable — a second tap would
  // send a stray keystroke into the agent while the first is still being processed.
  const realOpts = (s.question.options || []).filter((o) => !_FREETEXT_OPT.test(o.trim()));
  if (realOpts.length) {
    const opts = document.createElement("div");
    opts.className = "opts";
    realOpts.forEach((opt, i) => {
      const b = document.createElement("button");
      b.className = "opt";
      b.textContent = opt;
      b.disabled = spinning;
      onTap(b, () => { setActive(s.pane_id); answer(s, keyFor(s.question, opt, i)); }, true); // defer: a scroll must never submit an answer
      opts.appendChild(b);
    });
    q.appendChild(opts);
  }
  // Free-text reply goes through the single bottom bar (no per-card input anymore).
  return q;
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
// segment silently. Pure — callers own the render freeze around it.
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
  // `sending`, not the render freeze (which swipes/gestures also hold) — same reason as
  // submitComposer: guarding on the freeze made a legitimate answer tap silently do nothing
  // when a gesture happened to own the flag. Still blocks the real double-fire.
  //
  // This path DROPS rather than queues, unlike the composer: an option tap or a raw key
  // is only meaningful against the screen the user was looking at, so replaying it after
  // an unrelated send lands could answer a different prompt than the one they read. But
  // dropping it silently is what "the button just does nothing" felt like, so say so —
  // the whole point of this PR is that no send disappears without a trace.
  if (sending) return void barNote("Busy sending — that didn't go through. Tap again.");
  sending = true;
  busySend = true;
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
    releaseBusySoon(); // guarded: a composer send may already be running (see the helper)
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
    `<button class="screen-close" title="Close" aria-label="Close">${licon("x", 16)}</button></span></div>` +
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
// Pass `st` to persist the transform across re-renders (the card's background layer
// is rebuilt every poll); omitted (the full-screen overlay), it starts fresh at the
// BOTTOM-left — the end of a capture is the live state. (Captures shorter than the
// window stay top-aligned: that's the Math.min clamp.) `snapHome`: unzoomed pans
// spring back on release (drag-to-peek) instead of parking the content askew.
function pinchZoom(container, el, st, snapHome, selectable) {
  st = st || { scale: 1, tx: 0, ty: Math.min(0, container.clientHeight - el.offsetHeight) };
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
    busyGesture = true; // freeze poll re-renders mid-gesture
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
    // ALWAYS release the poll freeze, even while fingers remain down. This used to be
    // gated behind the `touches.length !== 0` early-return below, which leaked the freeze
    // FOREVER whenever a gesture ended without a clean zero-touch touchend — a second
    // finger lifting, a touch that starts here and ends elsewhere, an OS interruption.
    // A leaked freeze stops the poll loop permanently: the deck stops updating, and the
    // composer/Send paths that coordinate through it wedge, so the app looks dead (can't
    // send, can't even focus the input) until a reload. Reproduced by dispatching
    // touchstart then a touchend still reporting one touch: zero dock rebuilds in 5s.
    // This unconditional clear is why the freeze is two booleans and not a refcount —
    // "release no matter what" cannot be expressed in a count without leaking it.
    busyGesture = false;
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
  // The backgrounded-mid-gesture unwedge lives in onResume, NOT here: pinchZoom runs on the
  // per-poll card/peek rebuild, so a `document` listener per call leaked one every ~2s (the
  // `container` ones die with the detached DOM; a document one never does). Only `busy`
  // needs the global reset — `start` is per-instance and a stale instance's container is
  // already detached, so its gesture can neither continue nor be re-entered.
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
// summary slot (see card() and lmConvoView). Design: docs/design/live-mode.md.
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

function lmPaintInto(box) {
  box.replaceChildren(...lmLog.map((e) => {
    const d = document.createElement("div");
    d.className = "lm-" + e.role;
    d.textContent = (e.role === "user" ? "🗣 " : "") + e.text;
    return d;
  }));
  box.scrollTop = box.scrollHeight;
}

// Repaint in place between renders; card() re-inserts the box on every full render.
function lmPaint() {
  const box = document.querySelector(".card.active .lm-convo");
  if (box) lmPaintInto(box);
}

function lmConvoView() {
  const box = document.createElement("div");
  box.className = "lm-convo";
  // No aria-live: we repaint the whole box (replaceChildren) on every transcript
  // fragment, which a live region would re-announce in full — deafeningly noisy while
  // streaming. The transcript is a visible running log, not an announce-on-change alert.
  lmPaintInto(box);
  return box;
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
