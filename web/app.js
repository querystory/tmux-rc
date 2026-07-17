// tmux-rc PWA. Polls /api/state, renders ONE pane card at a time (the dock — icon
// tabs, tally filters — and card swipes switch panes), and posts answers back.
// No framework, no build step.

// Real brand marks per agent (served from web/). One img template so every logo-backed
// tool renders identically; emoji/text fallback for the rest. `tool` comes from parser
// JSON, so look it up with hasOwnProperty (a value like "toString"/"constructor" would
// otherwise resolve up the prototype chain and render garbage) and escape it into alt.
const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const LOGOS = { claude: "/claude.png", codex: "/openai.svg", gemini: "/gemini.svg",
  shell: "/bash.png" }; // official Bash logo (MIT — see bash-logo.LICENSE)
// Unidentified panes get the tmux logomark ("some tmux pane") instead of a bare dot.
const UNKNOWN_LOGO = "/tmux-logomark.svg";
// activity comes from parser (LLM) output and gets interpolated into class names —
// whitelist it so an unexpected value can't inject markup/classes.
const ACTIVITIES = new Set(["running", "waiting", "idle", "unknown"]);
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
  if (!favLink || waiting === favWaiting) return;
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
let busy = false; // suppress polling flicker while an answer is in flight

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
  fetch(`/api/panes/${encodeURIComponent(id)}/select`, { method: "POST" }).catch(() => {});
  // pending makes the switch instant in the UI (the next poll is 2s away, and the
  // watcher's view of tmux focus lags a tick or two behind that).
  pending = { id, ts: Date.now() };
  render(Object.values(panesById));
}

// Client-side accumulated activity log per pane. Each parse only returns the events
// currently on screen (1-2); we append new ones here so you can scroll back over the
// last several minutes and see everything the LLM summarized. Dedup by text so the
// same on-screen event across polls isn't logged repeatedly. Bounded so it can't grow
// unbounded. (A durable server-side log is the future feature; this is the cheap
// version that needs no backend.)
const eventLog = {}; // pane_id -> [{text, file, meta, ts}]
const EVENT_LOG_MAX = 500;

function accumulateEvents(paneId, events) {
  if (!Array.isArray(events) || !events.length) return;
  const log = (eventLog[paneId] ||= []);
  const seen = new Set(log.slice(-40).map((e) => e.text));
  for (const e of events) {
    if (e && e.text && !seen.has(e.text)) {
      log.push({ ...e, ts: Date.now() });
      seen.add(e.text);
    }
  }
  if (log.length > EVENT_LOG_MAX) log.splice(0, log.length - EVENT_LOG_MAX);
}

function fmtIdle(s) {
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  return Math.floor(s / 3600) + "h";
}

async function poll() {
  if (busy) return;
  try {
    const r = await fetch("/api/state");
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
      return;
    }
    const data = await r.json();
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
    render(data.panes || []);
  } catch (e) {
    // Surface the real error instead of silently sitting on "Connecting…" forever.
    liveEl.className = "dot off";
    panesEl.innerHTML = `<div class="empty">poll error: ${esc(String(e && e.message || e))}<br>` +
      `<small>(often a stale cached app.js — hard-refresh)</small></div>`;
    return;
  }
}

const usageEl = document.getElementById("usage");
function showUsage(u, err) {
  if (!u) { usageEl.textContent = ""; return; }
  // Rate is a plain session average (calls/uptime) computed server-side — stable.
  const tok = ((u.in_tokens + u.out_tokens) / 1000).toFixed(0);
  const total = u.calls + u.errors;
  // Show SUCCEEDED/total (e.g. 648/657) — reads as "almost all fine", not the
  // alarming failed/total. Only tint red when there actually are failures.
  const okColor = u.errors ? ' class="warn"' : "";
  const parts = [
    `${tok}k tok`,
    `$${u.cost.toFixed(3)}`,
    `<span${okColor}>${u.calls}/${total} ok</span>`,
    `${u.rate_per_min}/min`,
  ];
  if (err) parts.push(`<span class="warn" title="${escAttr(err)}">⚠</span>`);
  usageEl.innerHTML = parts.join(" · ");
}
// Full attribute escaping: & FIRST (so introduced entities aren't re-escaped), then the
// quote/angle set. A partial escape (only ") lets a value like `&quot;` decode back into
// a quote and break out of the attribute — these values come from parser JSON (untrusted).
function escAttr(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function render(states) {
  // Accumulate this poll's events into each pane's running client-side log first.
  states.forEach((s) => accumulateEvents(s.pane_id, s.events));
  panesById = Object.fromEntries(states.map((s) => [s.pane_id, s]));
  // Prune per-pane caches when panes vanish — otherwise pane churn grows them
  // without bound over a long-running session.
  for (const m of [eventLog, eventScroll, peekCache, bgZoom])
    for (const k of Object.keys(m)) if (!has(panesById, k)) delete m[k];
  setFavicon(states.some((s) => actOf(s) === "waiting"));
  if (!states.length) {
    dockEl.replaceChildren();
    panesEl.innerHTML = '<div class="empty">No tmux pane found.<br>Start a session and it will appear here.</div>';
    updateBar(null);
    return;
  }
  // Only the ACTIVE pane gets a full card. Other AGENT panes (and anything waiting)
  // each get a compact row above it; plain shells fold into one summary line so a
  // big fleet doesn't shove the active card off screen.
  const act = activeId();
  // List mode (a dock tally badge or "all" was tapped): just those panes as
  // one-liners; the dock stays up (tap an icon or a row to open that pane's card).
  const subset = listFilter && states.filter((s) => listFilter === "all" || actOf(s) === listFilter);
  if (subset && subset.length) {
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
      fs.textContent = "⤢";
      fs.title = "Full screen";
      fs.setAttribute("aria-label", "Full screen");
      fs.onclick = () => openScreen(a.pane_id, a.title || a.label);
      deck.append(card(a), bgTerm(a), fs); // flex column: DOM order = visual order
    }
    panesEl.replaceChildren(deck);
  }
  updateBar(panesById[act]);
}

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
function dock(states, act) {
  const el = dockEl;
  el.replaceChildren();
  for (const s of states) {
    const b = document.createElement("button");
    b.className = "dock-icon" + (s.pane_id === act ? " sel" : "");
    b.dataset.pane = s.pane_id;
    // Badge dot overlaps the logo's corner (like the favicon dot); idle panes get
    // none — quiet is the default, only running/waiting earn a signal.
    const a = actOf(s);
    b.innerHTML = iconFor(s.tool) +
      (a === "running" || a === "waiting" ? `<i class="ddot d-${a}" aria-hidden="true"></i>` : "");
    b.title = s.title || s.label || s.pane_id;
    b.setAttribute("aria-label", b.title);
    // Jump to that pane's CARD — including from list mode (a dock tap means "show
    // me this pane", not "re-highlight it inside the list").
    b.onclick = () => { listFilter = null; setActive(s.pane_id); };
    el.appendChild(b);
  }
  // Density + navigation: per-activity tallies, right-aligned — each one FILTERS the
  // list view to those panes; "all" lists everything.
  const counts = document.createElement("span");
  counts.className = "dock-counts";
  const n = {};
  states.forEach((s) => (n[actOf(s)] = (n[actOf(s)] || 0) + 1));
  const filt = (label, key) => {
    const b = document.createElement("button");
    b.className = "badge b-" + key;
    b.textContent = label;
    b.onclick = () => { captureIconRects(); listFilter = key; render(Object.values(panesById)); };
    counts.appendChild(b);
  };
  ["waiting", "running", "idle", "unknown"].filter((a) => n[a]).forEach((a) => filt(`${n[a]} ${a}`, a));
  filt("all", "all");
  el.appendChild(counts);
}

// "Animate the icons down": capture the dock icons' positions when a filter is
// tapped, then fly clones to each row's icon once the list renders (FLIP).
let flipFrom = null; // pane_id -> DOMRect
function captureIconRects() {
  flipFrom = {};
  dockEl.querySelectorAll(".dock-icon").forEach((b) => {
    if (b.dataset.pane) flipFrom[b.dataset.pane] = b.getBoundingClientRect();
  });
}
function flipIn(root) {
  if (!flipFrom) return;
  const from = flipFrom;
  flipFrom = null;
  root.querySelectorAll(".prow").forEach((r) => {
    const src = from[r.dataset.pane];
    const icon = r.querySelector(".icon");
    if (!src || !icon) return;
    const dst = icon.getBoundingClientRect();
    const fly = icon.cloneNode(true);
    Object.assign(fly.style, {
      position: "fixed", left: src.left + "px", top: src.top + "px", margin: "0",
      zIndex: 30, pointerEvents: "none",
    });
    // The row stays INVISIBLE until its icon arrives — only then is the rest of the
    // summary (outline, title, badge) drawn. The safety timer reveals it even if the
    // flight animation gets interrupted, so the list can never end up blank.
    r.style.opacity = "0";
    const reveal = () => {
      fly.remove();
      if (r.style.opacity === "0") {
        r.style.opacity = "";
        r.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 100 });
      }
    };
    document.body.appendChild(fly);
    // Web Animations API, not CSS-class transitions: keyframes take effect the moment
    // they're created, so the start states actually paint (the class-toggle version
    // kept losing the race and text popped in before the flight).
    fly.animate(
      [{ transform: "translate(0,0)" },
       { transform: `translate(${dst.left - src.left}px,${dst.top - src.top}px)` }],
      { duration: 250, easing: "ease-out", fill: "forwards" }
    ).onfinish = reveal;
    setTimeout(reveal, 400);
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
  const badge = a === "idle" ? "idle " + fmtIdle(s.idle_seconds) : a;
  el.innerHTML =
    `<span class="icon">${iconFor(s.tool)}</span>` +
    `<div class="prow-meta"><div class="prow-name">${esc(s.title || s.label || s.pane_id)}</div>` +
    (s.headline ? `<div class="prow-sub">${esc(s.headline)}</div>` : "") +
    `</div><span class="badge b-${a}">${badge}</span>`;
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
  el.className = "card" + (s.activity === "waiting" ? " waiting" : "")
    + (s.pane_id === activeId() ? " active" : "");
  swipeNav(el, s.pane_id);
  // Tapping a card makes it the target of the single bottom input bar.
  el.onclick = (e) => {
    if (e.target.closest("button, input, a, summary, details")) return; // don't steal option/timeline taps
    setActive(s.pane_id);
  };

  const row = document.createElement("div");
  row.className = "row";
  const a = actOf(s);
  const badge =
    a === "idle"
      ? "idle " + fmtIdle(s.idle_seconds)
      : a === "running"
        ? '<span class="pulse"></span>running'
        : a;
  // Header: icon, name (with the working verb·elapsed·↓tokens INLINE to the right to
  // save vertical space), headline below, activity badge. Fields come straight from
  // the parser JSON, so the UI renders whatever the model provides.
  const w = s.working || {};
  const working =
    s.activity === "running" && (w.verb || w.elapsed || w.tokens)
      ? `<span class="worksub">${[w.verb, w.elapsed, w.tokens && "↓" + w.tokens]
          .filter(Boolean).map(esc).join(" ")}</span>`
      : "";
  row.innerHTML = `
    <span class="icon">${iconFor(s.tool)}</span>
    <div class="meta">
      <div class="name">${esc(s.title || s.label || "")} ${working}</div>
      <div class="status">${esc(s.headline || "—")}</div>
    </div>
    <span class="badge b-${a}">${badge}</span>`;
  el.appendChild(row);

  if (s.rewind) el.appendChild(rewindView(s));
  // Tables render BEFORE the question so they act as context above the options.
  if (Array.isArray(s.tables)) s.tables.forEach((t) => el.appendChild(tableView(t)));
  if (s.question) el.appendChild(question(s));
  if (Array.isArray(s.tasks) && s.tasks.length) el.appendChild(tasksView(s.tasks));
  if (Array.isArray(s.links) && s.links.length) el.appendChild(linksView(s.links));
  const log = eventLog[s.pane_id] || [];
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
const peekCache = {}; // pane_id -> {snap, text}
const bgZoom = {}; // pane_id -> {scale, tx, ty}
function bgTerm(s) {
  // Wrapper = the visible window (starts right below the card, ends near the bar);
  // the trimmed capture is top-anchored inside it, scrolled to its tail when longer.
  // So a 1-line shell prompt sits right under the card instead of drowning in the
  // blank lines tmux pads the capture with.
  const wrap = document.createElement("div");
  // "shell" here means "no status chrome at the bottom of the capture" — agents get
  // their chrome tucked behind the bar, shells keep their prompt visible above it.
  // (Don't key this on LOGOS: shell has a logo too now.)
  wrap.className = "bg-wrap" + (["claude", "codex", "gemini"].includes(s.tool) ? "" : " shell");
  const box = document.createElement("pre");
  box.className = "bg-term";
  wrap.appendChild(box);
  const toEnd = () => { wrap.scrollTop = wrap.scrollHeight; };
  const c = peekCache[s.pane_id];
  if (c) box.textContent = c.text; // last capture (kept while a newer one loads)
  const snap = s.snapshot_id;
  if (snap && (!c || c.snap !== snap))
    fetch(`/api/panes/${encodeURIComponent(s.pane_id)}/snapshots/${snap}`)
      .then((r) => (r.ok ? r.text() : ""))
      .then((t) => {
        const txt = t.replace(/\s+$/, "");
        // Responses can resolve out of order — never let an older snapshot (ids are
        // ms timestamps) overwrite a newer one already applied.
        const cur = peekCache[s.pane_id];
        if (!txt || (cur && Number(cur.snap) >= Number(snap))) return;
        peekCache[s.pane_id] = { snap, text: txt };
        box.textContent = txt;
        toEnd();
      })
      .catch(() => {});
  pinchZoom(wrap, box, (bgZoom[s.pane_id] ||= { scale: 1, tx: 0, ty: 0 }), true);
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
  entries.forEach((e) => { e.target.scrollTop = e.target.scrollHeight; }));
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
  return `<div class="ev"><span class="ev-text">${esc(e.text || "")}</span>${note}</div>`;
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
};
function activeState() {
  const id = activeId();
  return panesById[id] || { pane_id: id, label: "" };
}
function updateBar(s) {
  bar.input.placeholder = s ? `Type into ${s.label || "pane"}…` : "No pane";
  bar.meta.innerHTML = s ? metaChips(s) : "";
}
if (bar.input) {
  bar.send.onclick = () => {
    if (bar.input.value) { answer(activeState(), bar.input.value); bar.input.value = ""; }
  };
  bar.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && bar.input.value) {
      answer(activeState(), bar.input.value); bar.input.value = "";
    }
  });
  bar.attach.onclick = () => bar.file.click();
  bar.file.onchange = () => bar.file.files[0] && uploadImage(activeState(), bar.file.files[0], bar.input);
  // Desktop image-paste into the field still works; the dedicated 📋 button is gone
  // (redundant with 📎, and clipboard-read needs HTTPS anyway).
  bar.input.onpaste = (e) => {
    const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
    if (item) { e.preventDefault(); uploadImage(activeState(), item.getAsFile(), bar.input); }
  };
  // Special keys → the active pane (tmux key-names, sent literally).
  document.querySelectorAll("#bar .keys button").forEach((b) => {
    b.onclick = () => sendRaw(activeState(), b.dataset.k);
  });
}

// Read an image off the clipboard and upload it. navigator.clipboard.read() is the
// only path that works on mobile (input.onpaste doesn't), but it needs a SECURE
// CONTEXT — HTTPS or localhost. Over plain http:// on the LAN it's undefined, which
// is almost certainly why paste "does nothing". We surface that instead of failing
// silently, and point the user at the 📎 attach button (which always works).
async function pasteImage(s, input) {
  if (!navigator.clipboard || !navigator.clipboard.read) {
    alert(
      "Clipboard paste needs HTTPS (or localhost). You're on plain http over the LAN, " +
      "so the browser blocks it.\n\nUse the 📎 button instead — pick the screenshot " +
      "from your Photo Library."
    );
    return;
  }
  try {
    const items = await navigator.clipboard.read();
    for (const item of items) {
      const type = item.types.find((t) => t.startsWith("image/"));
      if (type) { uploadImage(s, await item.getType(type), input); return; }
    }
    alert("No image on the clipboard. Copy a screenshot first, or use 📎.");
  } catch (e) {
    alert("Couldn't read clipboard: " + e.message + "\n\nTry the 📎 button instead.");
  }
}

// Upload an image and paste it into the pane (server puts it on the clipboard and
// sends Ctrl-V). If the user already typed text in the web box, send that text to the
// pane FIRST (no Enter), so the text and image land together in the agent's prompt in
// order — otherwise the typed text would be stranded in the web box and lost on the
// next re-render.
async function uploadImage(s, file, input) {
  if (!file) return;
  busy = true;
  try {
    if (input && input.value) {
      await fetch(`/api/panes/${encodeURIComponent(s.pane_id)}/send`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ keys: input.value, enter: false, literal: true }),
      });
      input.value = "";
    }
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(`/api/panes/${encodeURIComponent(s.pane_id)}/image`, { method: "POST", body: fd });
    if (!r.ok) alert("Image upload failed: " + r.status);
  } finally {
    setTimeout(() => { busy = false; poll(); }, 400);
  }
}

function question(s) {
  const q = document.createElement("div");
  q.className = "q";
  const prompt = document.createElement("div");
  prompt.className = "prompt";
  prompt.textContent = s.question.prompt;
  q.appendChild(prompt);

  // Option buttons (drop any "type something"/"Other" pseudo-option — the bottom bar
  // covers free-text). Tapping an option also makes this pane active, then answers it.
  const realOpts = (s.question.options || []).filter((o) => !_FREETEXT_OPT.test(o.trim()));
  if (realOpts.length) {
    const opts = document.createElement("div");
    opts.className = "opts";
    realOpts.forEach((opt, i) => {
      const b = document.createElement("button");
      b.className = "opt";
      b.textContent = opt;
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
  await send(s, { keys, enter: true, literal: true });
}

// Send a tmux key-name (Escape/Up/C-c) — not literal text, no appended Enter.
async function sendRaw(s, keyName) {
  await send(s, { keys: keyName, enter: false, literal: false });
}

async function send(s, body) {
  busy = true;
  try {
    await fetch(`/api/panes/${encodeURIComponent(s.pane_id)}/send`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } finally {
    setTimeout(() => { busy = false; poll(); }, 400);
  }
}

// Full-screen, scrollable (both axes) view of the pane's latest raw capture, fetched
// ON DEMAND (opened via the ⤢ full-screen button over the deck).
async function openScreen(paneId, label) {
  let text = "(loading…)";
  try {
    const list = await (await fetch(`/api/panes/${encodeURIComponent(paneId)}/snapshots`)).json();
    const last = list[list.length - 1];
    if (last)
      text = await (await fetch(`/api/panes/${encodeURIComponent(paneId)}/snapshots/${last.id}`)).text();
    else text = "(no capture yet)";
  } catch { text = "(failed to load)"; }
  const ov = document.createElement("div");
  ov.className = "screen-overlay";
  ov.innerHTML =
    `<div class="screen-head"><span>${esc(label || paneId)}</span><button class="screen-close">✕</button></div>` +
    `<div class="screen-body"><pre class="screen-pre"></pre></div>`;
  ov.querySelector(".screen-pre").innerHTML = linkifyCapture(text);
  const close = () => ov.remove();
  ov.querySelector(".screen-close").onclick = close;
  document.body.appendChild(ov);
  pinchZoom(ov.querySelector(".screen-body"), ov.querySelector(".screen-pre"));
}

// Linkify URLs in a raw capture, INCLUDING ones the TUI hard-wrapped across lines: a
// URL run that reaches the end of a full-width line continues on the next line (that's
// what wrapping means), so those runs are joined into one href while the anchor keeps
// the original line breaks — every visual fragment is clickable and opens the full URL.
// (tmux's own soft-wraps are already unwrapped by capture-pane -J; this handles the
// agent TUIs that print their own hard breaks at pane width.)
function linkifyCapture(raw) {
  raw = raw.replace(/\r/g, "");
  const lines = raw.split("\n");
  // Joining is plausible only when the capture shows evidence of real wrapping: a
  // plausible terminal width (>=40) AND at least TWO full-width lines (a lone longest
  // line that happens to end in a URL, or a uniform tiny pane, is not wrapping). This
  // keeps narrow real panes (e.g. 58-col splits) joining, unlike an absolute floor.
  const maxLen = Math.max(0, ...lines.map((l) => l.length));
  const fullLines = lines.filter((l) => l.length >= maxLen - 1).length;
  const canJoin = maxLen >= 40 && fullLines >= 2 && fullLines < lines.length;
  const joinWidth = maxLen - 1;
  const lineEndLen = new Map(); // offset of each \n in raw -> length of the line it ends
  let off = 0;
  for (const l of lines) { lineEndLen.set(off + l.length, l.length); off += l.length + 1; }

  let out = "", pos = 0, m;
  // URL chars: the original terminator exclusions (whitespace, quotes, brackets — also
  // the belt-and-braces for attribute safety) PLUS everything non-ASCII, so TUI
  // box-drawing borders (│ etc.) flush against a URL never glue onto the href.
  const re = /https?:\/\/[^\s<>"')\]\u0000-\u001F\u007F-\uFFFF]+(?:\n[ \t]*[^\s<>"')\]\u0000-\u001F\u007F-\uFFFF]+)*/gi;
  while ((m = re.exec(raw))) {
    out += esc(raw.slice(pos, m.index));
    // Accept newline-continuations only while the line being left was full-width
    // (a wrapped line); cut the match at the first newline that isn't.
    let cut = m[0].length, search = 0;
    for (;;) {
      const nl = m[0].indexOf("\n", search);
      if (nl === -1) break;
      const endedLine = lineEndLen.get(m.index + nl);
      if (canJoin && endedLine !== undefined && endedLine >= joinWidth) { search = nl + 1; continue; }
      cut = nl; break;
    }
    const shown = m[0].slice(0, cut);
    const href = shown.replace(/\n[ \t]*/g, "");
    out += `<a href="${escAttr(href)}" target="_blank" rel="noopener noreferrer">${esc(shown)}</a>`;
    pos = m.index + cut;
    re.lastIndex = pos;
  }
  return out + esc(raw.slice(pos));
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
    let dx = 0, dy = 0;
    if (r.width <= c.width) dx = c.left - r.left;
    else if (r.left > c.left) dx = c.left - r.left;
    else if (r.right < c.right) dx = c.right - r.right;
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
poll();
setInterval(poll, 2000);

// Auto-reload the page when the web assets change (dev convenience). All state lives
// server-side in the watcher, so a full reload loses nothing. Checks every 5s and
// reloads if the version hash differs from the one seen at load.
let _ver = null;
setInterval(async () => {
  try {
    const { version } = await (await fetch("/api/version")).json();
    if (_ver === null) _ver = version;
    else if (version !== _ver) location.reload();
  } catch {}
}, 5000);

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
