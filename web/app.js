// tmux-rc PWA. Polls /api/state, renders ONE pane card at a time (the dock — icon
// tabs, tally filters — and card swipes switch panes), and posts answers back.
// No framework, no build step (native ES module — index.html loads type=module).
import { renderCapture } from "./terminal.js";

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
const actOf = (s) => (ACTIVITIES.has(s.activity) ? s.activity : "unknown");
const img = (src, alt) => `<img src="${src}" width="22" height="22" alt="${escAttr(alt)}" style="border-radius:5px" />`;
const iconFor = (tool) => img(has(LOGOS, tool) ? LOGOS[tool] : UNKNOWN_LOGO, tool || "pane");
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

// Track which pane's timeline is expanded so a re-render doesn't collapse it.
const openTimelines = new Set();
// Collapse is a VIEW-WIDE preference, not per-pane: collapse one card (caret ▸) and
// every pane — including ones you swipe to — shows its one-line header, handing the
// screen to the live terminal. Expanding anywhere expands them all.
let cardsCollapsed = false;
// `busy` freezes polling re-renders while a mutation is mid-flight: an answer/composer
// send (so a fresh parse doesn't replace the card under the user) and swipe/pinch
// gestures. While set, send() also no-ops to avoid double-firing.
let busy = false;
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
function activeId() {
  if (pending) {
    const s = panesById[pending.id];
    // Only SERVER data may confirm (panesById is never mutated locally): earlier this
    // also checked a locally-set tmux_active flag, which "confirmed" the pending pick
    // instantly — so the next stale poll yanked the selection back to the old pane.
    if (s && s.tmux_active) pending = null; // server caught up — its truth takes over
    else if (!s || Date.now() - pending.ts > 8000) pending = null; // pane gone / select never landed
    else return pending.id;
  }
  const focused = Object.values(panesById).find((s) => s.tmux_active);
  return focused ? focused.pane_id : Object.keys(panesById)[0] || null;
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
function paneHeader(s, { caret = false, collapsed = false, icon = false } = {}) {
  const a = actOf(s);
  const badge = a === "idle" ? "idle " + fmtIdle(s.idle_seconds)
    : a === "running" || a === "compacting" ? `<span class="pulse"></span>${a}` : a;
  return (
    (caret ? `<button class="card-caret" aria-label="${collapsed ? "expand" : "collapse"}"`
      + ` aria-expanded="${!collapsed}">${collapsed ? "▸" : "▾"}</button>` : "")
    + (icon ? `<span class="icon">${iconFor(s.tool)}</span>` : "")
    + `<div class="ph-meta"><div class="ph-name">${esc(s.title || s.label || s.pane_id)}</div>`
    + (s.headline ? `<div class="ph-sub">${esc(s.headline)}</div>` : "")
    + `</div><div class="ph-right">${workSub(s)}<span class="badge b-${a}">${badge}</span></div>`
  );
}

let _stateVersion = null;  // last deck version the server gave us — sent back to long-poll;
                           // null until the first reply so cold load asks for state outright
let _booted = false;       // server has completed its first tick — an empty deck is only
                           // "no panes" once this is true (before it, initial parses run)
// Long-poll /api/state: the request HOLDS on the server until the deck changes (pane
// switch, add/remove, label/activity, new events) or ~25s, then returns. pollLoop is the
// ONLY caller and runs one at a time, so there's no concurrent-fetch state to track —
// sends never poll (they just set `busy`; pollLoop resumes when it clears). Returns true
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
    // `busy` may have flipped true WHILE this request was in flight (a send/gesture
    // started mid-fetch). Applying the response now would replace the card under the
    // user — the very thing `busy` freezes. Drop it without touching _stateVersion, so
    // the next (post-busy) hold re-fetches from the same version and renders in order.
    if (busy) return true;
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
    liveEl.className = "dot off";
    const transient = /failed to fetch|networkerror|load failed/i.test(String(e && e.message || e));
    // Report the NON-transient poll failures (a resume/network blip is expected noise);
    // a persistent JSON/parse fault is the invisible-on-mobile bug #57 is about.
    if (!transient) reportError("poll", e);
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
  for (;;) {
    if (busy) { await pollSleep(250); continue; } // a send/gesture froze re-renders; re-check soon
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
function showUsage(u, err) {
  if (!u) {
    // Gone (reconnect, fresh daemon): clear the leftovers too — a stale tooltip on an
    // empty span, or a popover left open with nothing to show.
    usageEl.textContent = ""; usageEl.title = "";
    usageEl.hidden = true; liveEl.setAttribute("aria-expanded", "false");
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
  const parts = [
    `${tok}k tok`,
    `<span${u.errors ? ' class="warn"' : ""}>$${u.cost.toFixed(3)}</span>`,
    `${u.rate_per_min}/min`,  // parser calls/min (voice isn't a per-tick call) — see tooltip
  ];
  if (live.sessions) parts.push(`🎙${live.sessions}`); // voice sessions this run
  if (err) parts.push(`<span class="warn" title="${escAttr(err)}">⚠</span>`);
  usageEl.innerHTML = parts.join(" · ");
  // Tooltip: total, then the parser/voice split so the combined cost is explainable.
  const parser = (u.parser_cost ?? u.cost);
  usageEl.title = `LLM telemetry · ${tok}k tokens · $${u.cost.toFixed(3)} total`
    + ` (parser $${parser.toFixed(3)}`
    + (live.sessions ? `, voice $${live.cost.toFixed(3)} over ${live.sessions} session${live.sessions > 1 ? "s" : ""}` : "")
    + `) · ${u.rate_per_min} parser calls/min` + (u.errors ? ` · ${u.errors} errors` : "");
}
// Full attribute escaping: & FIRST (so introduced entities aren't re-escaped), then the
// quote/angle set. A partial escape (only ") lets a value like `&quot;` decode back into
// a quote and break out of the attribute — these values come from parser JSON (untrusted).
function escAttr(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

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
    updateBar(null);
    return;
  }
  delete panesEl.dataset.empty; // re-arm the empty-state guard for the next empty deck
  // Only the ACTIVE pane gets a full card. Other AGENT panes (and anything waiting)
  // each get a compact row above it; plain shells fold into one summary line so a
  // big fleet doesn't shove the active card off screen.
  const act = activeId();
  // List mode (a dock tally badge or "all" was tapped): just those panes as
  // one-liners; the dock stays up (tap an icon or a row to open that pane's card).
  const subset = listFilter && states.filter((s) => listFilter === "all" || actOf(s) === listFilter);
  if (subset && subset.length) {
    stopPeek(); // list mode: no card, no peek stream
    dock(states, act); // dock stays up in list mode — icon tap jumps to that card
    panesEl.replaceChildren(...subset.map((s) => row(s, act))); // server order — same as the dock
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
      fs.onclick = () => openScreen(a.pane_id, a.title || a.label);
      deck.append(card(a), bgTerm(a), fs); // flex column: DOM order = visual order
    }
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
      d = deck.getBoundingClientRect(), t = top.getBoundingClientRect();
    // FIRST tab selected: flush-left, no flare — the rail's left border vanishes so
    // the tab's own blue border IS the line, collinear with the card's below (see
    // .edge-l CSS). Only the first icon gets this; a mid-list icon scrolled near the
    // edge merely squares the corner under its flare (sq-l below).
    const edgeL = sel === dockEl.querySelector(".dock-icon") && s.left - d.left - 7 < 14;
    dockEl.classList.toggle("edge-l", edgeL);
    fl.style.display = edgeL ? "none" : "";
    n.style.left = s.left - d.left + 1 + "px"; // inset 1px each side: the fillets own
    n.style.width = s.width - 2 + "px";        // the corner pixels
    // A fillet needs a FLAT border line under it; inside the card's corner-radius
    // zone the border curves away and nothing lines up. When an edge tab's flare
    // would land there, square that corner (14 = the .card border-radius).
    const card = deck.querySelector(".card.active");
    if (card) {
      card.classList.toggle("sq-l", s.left - d.left - 7 < 14);
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
  for (const s of states) {
    const b = document.createElement("button");
    b.className = "dock-icon" + (s.pane_id === act ? " sel" : "");
    b.dataset.pane = s.pane_id;
    // Badge dot overlaps the logo's corner (like the favicon dot); idle panes get
    // none — quiet is the default, only busy states (running/waiting/compacting) earn a signal.
    const a = actOf(s);
    b.innerHTML = iconFor(s.tool) +
      (a === "running" || a === "waiting" || a === "compacting" ? `<i class="ddot d-${a}" aria-hidden="true"></i>` : "");
    b.title = s.title || s.label || s.pane_id;
    b.setAttribute("aria-label", b.title);
    // Jump to that pane's CARD — including from list mode (a dock tap means "show
    // me this pane", not "re-highlight it inside the list").
    b.onclick = () => { listFilter = null; setActive(s.pane_id); };
    el.appendChild(b);
  }
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
    b.onclick = () => { captureIconRects(); listFilter = key; render(Object.values(panesById)); };
    filtersEl.appendChild(b);
  };
  ["waiting", "running", "compacting", "idle", "unknown"].filter((a) => n[a]).forEach((a) => filt(`${n[a]} ${a}`, a));
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
  // Tapping a row opens that pane's card (drops back out of list view). Keyboard
  // reachable too: it's a div, so it needs button semantics spelled out.
  el.setAttribute("role", "button");
  el.tabIndex = 0;
  el.onclick = () => { listFilter = null; setActive(s.pane_id); };
  el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); el.onclick(); } };
  // Same shared header as the card (see paneHeader) — a row shows the icon and no caret.
  el.innerHTML = paneHeader(s, { icon: true });
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
    busy = true; // freeze poll re-renders mid-drag — they'd replace the card under the finger
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
      busy = false;
      return;
    }
    const dir = dx < 0 ? -1 : 1;
    el.style.transform = `translateX(${dir * W()}px)`;
    if (ghost) ghost.style.transform = "translateX(0)";
    setTimeout(() => { busy = false; setActive(neighbor(dir)); }, 150);
  });
  // A cancelled gesture (OS interruption) must release the poll freeze and snap back,
  // or polling stays frozen indefinitely.
  el.addEventListener("touchcancel", () => {
    sx = null; busy = false;
    el.style.transition = "";
    el.style.transform = "";
    if (ghost) { ghost.style.transform = `translateX(${-gdir * W()}px)`; setTimeout(clear, 160); }
  });
}

function card(s) {
  const el = document.createElement("div");
  const collapsed = cardsCollapsed;
  el.className = "card" + (s.activity === "waiting" ? " waiting" : "")
    + (s.pane_id === activeId() ? " active" : "") + (collapsed ? " collapsed" : "")
    + (isReparsing(s) ? " reparsing" : ""); // input sent, awaiting the forced re-parse
  swipeNav(el, s.pane_id);
  // Tapping a card makes it the target of the single bottom input bar.
  el.onclick = (e) => {
    if (e.target.closest("button, input, a, summary, details")) return; // don't steal option/timeline taps
    setActive(s.pane_id);
  };

  const row = document.createElement("div");
  row.className = "row";
  // Shared header layout (see paneHeader). The card adds the collapse caret and omits
  // the icon — its dock tab above IS the icon. The ▾/▸ caret collapses the card to just
  // this header row (still tab-joined), handing the live terminal the screen; collapse
  // state is view-wide (cardsCollapsed) so swiping panes keeps the chosen height.
  row.innerHTML = paneHeader(s, { caret: true, collapsed, icon: false });
  row.querySelector(".card-caret").onclick = (e) => {
    e.stopPropagation(); // don't also re-select the pane
    cardsCollapsed = !collapsed;
    render(Object.values(panesById));
  };
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
    sum.textContent = s.session_summary;
    sum.onclick = (e) => { e.stopPropagation(); sum.classList.toggle("open"); };
    el.appendChild(sum);
  }

  if (s.rewind) el.appendChild(rewindView(s));
  // Tables render BEFORE the question so they act as context above the options.
  if (Array.isArray(s.tables)) s.tables.forEach((t) => el.appendChild(tableView(t)));
  if (s.question) el.appendChild(question(s));
  if (Array.isArray(s.tasks) && s.tasks.length) el.appendChild(tasksView(s.tasks));
  if (Array.isArray(s.links) && s.links.length) el.appendChild(linksView(s.links));
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
        if (peekBox && peekBox.innerHTML !== html) {
          peekBox.innerHTML = html;
          tuckChrome(peekWrap, peekBox);
          if (!busy && zHome(streamPane)) peekWrap.scrollTop = peekWrap.scrollHeight;
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
    // Strip bidi controls (an unterminated RLO in a model label could visually
    // reverse the host indicator) and cap by code points (no split surrogates).
    const label = Array.from(
      String(l.text || "").replace(/[\u202A-\u202E\u2066-\u2069]/g, "").trim()
    ).slice(0, 80).join("");
    a.textContent = `\u{1F517} ${label || host}`;
    const hostEl = document.createElement("span");
    hostEl.className = "linkhost";
    hostEl.textContent = ` ${host}`;
    a.appendChild(hostEl);
    a.onclick = (e) => e.stopPropagation();
    box.appendChild(a);
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
  return `<div class="ev${e.historical ? " ev-hist" : ""}"><span class="ev-text">${esc(e.text || "")}</span>${note}</div>`;
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
function updateBar(s) {
  // The CSS :empty::before placeholder isn't an accessible name, so mirror it into
  // aria-label — screen readers announce the per-pane target instead of a bare textbox.
  const label = s ? `Type into ${s.label || "pane"}…` : "No pane";
  bar.input.dataset.placeholder = label;
  bar.input.setAttribute("aria-label", label);
  bar.meta.innerHTML = s ? metaChips(s) : "";
}
if (bar.input) {
  // Send/Enter submits the composer: typed text and/or a staged image go to the pane
  // together, then one Enter (see submitComposer). Nothing reaches the pane at attach
  // time — the image waits here as a draft until you send, mirroring the phone's own
  // "type a caption, then send the photo" feel.
  bar.send.onclick = () => submitComposer(activeState());
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
async function submitComposer(s) {
  if (busy) return; // a send is already in flight — don't double-fire (keydown+beforeinput)
  const segs = composerSegments();
  if (!segs.length) return;
  busy = true;
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
    alert("Image upload failed — not sent. Your text and images are still in the composer.\n\n" + e.message);
  } finally {
    setTimeout(() => { busy = false; }, 400); // pollLoop resumes on its own once busy clears
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
  const r = await fetch(`/api/panes/${encodeURIComponent(s.pane_id)}/image`, { method: "POST", body: fd });
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
      b.onclick = () => { setActive(s.pane_id); answer(s, keyFor(s.question, opt, i)); };
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
// segment silently. Pure — callers own the `busy` freeze around it.
async function postSend(s, body) {
  const r = await fetch(`/api/panes/${encodeURIComponent(s.pane_id)}/send`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error("send failed: " + r.status);
}

async function send(s, body) {
  if (busy) return; // a send is already in flight — a double-tapped option must not double-fire
  busy = true;
  markReparsing(s.pane_id); // spin the card until the server's forced reparse lands
  render(Object.values(panesById)); // reflect the spinning state immediately
  try {
    await postSend(s, body);
  } finally {
    setTimeout(() => { busy = false; }, 400); // pollLoop resumes on its own once busy clears
  }
}

// Full-screen live view of the pane (⤢ over the deck): the same long-poll stream as
// the peek, rendered big and pan/zoomable. Color = live; gray = the stream went quiet.
function openScreen(paneId, label) {
  const ov = document.createElement("div");
  ov.className = "screen-overlay";
  ov.innerHTML =
    `<div class="screen-head"><span>${esc(label || paneId)}</span><button class="screen-close">✕</button></div>` +
    `<div class="screen-body"><pre class="screen-pre">(connecting…)</pre></div>`;
  const body = ov.querySelector(".screen-body");
  const pre = ov.querySelector(".screen-pre");
  // Seed with the pane's last-known frame (gray) so the view isn't blank while the
  // stream connects — same instant-stale trick as the peek.
  const cached = peekCache[paneId];
  if (cached) { pre.innerHTML = cached.html; pre.classList.add("stale"); }
  document.body.appendChild(ov);
  pinchZoom(body, pre);
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
      if (pre.innerHTML !== html) pre.innerHTML = html; // no-op swap = flicker; skip it
      peekCache[paneId] = { html }; // shared cache with the peek
    },
    onLive: () => pre.classList.remove("stale"),
    onQuiet: () => pre.classList.add("stale"),
  });
  ov.querySelector(".screen-close").onclick = () => {
    stop(); ov.remove(); screenOpen = false; // next poll re-mounts the peek stream
  };
}

// Pinch-to-zoom + pan for just the terminal content (transform on the <pre>, not the
// page). One-finger drag pans; two-finger pinch zooms around the gesture midpoint.
// Pass `st` to persist the transform across re-renders (the card's background layer
// is rebuilt every poll); omitted (the full-screen overlay), it starts fresh at the
// BOTTOM-left — the end of a capture is the live state. (Captures shorter than the
// window stay top-aligned: that's the Math.min clamp.) `snapHome`: unzoomed pans
// spring back on release (drag-to-peek) instead of parking the content askew.
function pinchZoom(container, el, st, snapHome) {
  st = st || { scale: 1, tx: 0, ty: Math.min(0, container.clientHeight - el.offsetHeight) };
  let start = null; // {dist, cx, cy} for pinch, or {x,y} for pan
  const apply = () => { el.style.transform = `translate(${st.tx}px,${st.ty}px) scale(${st.scale})`; };
  el.style.transformOrigin = "0 0";
  if (st.scale !== 1 || st.tx || st.ty) apply();
  const dist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  const mid = (t) => ({ x: (t[0].clientX + t[1].clientX) / 2, y: (t[0].clientY + t[1].clientY) / 2 });
  container.addEventListener("touchstart", (e) => {
    busy = true; // freeze poll re-renders mid-gesture
    if (e.touches.length === 2) { const m = mid(e.touches); start = { dist: dist(e.touches), s: st.scale, tx: st.tx, ty: st.ty, cx: m.x, cy: m.y }; }
    else if (e.touches.length === 1) start = { pan: true, x: e.touches[0].clientX - st.tx, y: e.touches[0].clientY - st.ty };
  }, { passive: false });
  container.addEventListener("touchmove", (e) => {
    if (!start) return;
    e.preventDefault();
    if (start.pan && e.touches.length === 1) {
      st.tx = e.touches[0].clientX - start.x; st.ty = e.touches[0].clientY - start.y;
    } else if (e.touches.length === 2) {
      const f = dist(e.touches) / start.dist;
      st.scale = Math.min(6, Math.max(0.4, start.s * f));
      // keep the pinch midpoint stationary
      st.tx = start.cx - (start.cx - start.tx) * (st.scale / start.s);
      st.ty = start.cy - (start.cy - start.ty) * (st.scale / start.s);
    }
    apply();
  }, { passive: false });
  const release = (e) => {
    if (e.touches.length !== 0) return;
    start = null; busy = false;
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

// Auto-update: when the web assets change, reload to the new version (checked every
// 5s against /api/version; all durable state lives server-side). UNLESS the user has
// un-sent state — a typed draft or a staged image — which a silent reload would eat:
// then show a tap-to-reload banner instead and let them choose the moment.
let _ver = null;
setInterval(async () => {
  try {
    const { version } = await (await fetch("/api/version")).json();
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
    else if (m.type === "error") lmAdd("err", `⚠ ${m.message}`);
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
  lm.btn.title = lm.btn.ariaLabel = "End Live Mode";
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
  lm.btn.title = lm.btn.ariaLabel = "Start Live Mode";
  render(Object.values(panesById)); // the active card gets its static summary back
}

if (lm.btn) lm.btn.onclick = () => (lmWs ? lmStop() : lmStart());
