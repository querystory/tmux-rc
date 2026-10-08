import { headerPicker, dismissable } from "/m/header-picker.js";
import { renderAtlas, renderFleet, refreshAtlasHistory } from '/m/atlas.js';
import { renderCaptureLines, linkifyText } from "/terminal.js";
import { setupLiveMode } from "/m/live.js";
import { Composer, bindAttach, enterSubmits } from "/m/composer.js";
import { answerBody, pickCursorRow } from "/cursor-pick.js";
import { sendPresence, setupPush, stateUrl } from "/push.js";
import { paneLinks } from "/pr-links.js";
import { needsYou, activityLabel, activityClass, isRunning, markWorking, isRecent, matchesFilter, matchesSearch, lastActivity, stillOnPane, paneName, paneActivity, paneHeadline, paneMeta, records, itemDone, awaitingLaunch, LAUNCH_GRACE_MS, age } from "/m/pane-model.js";
import { parseHash, formatHash, historyMode } from "/m/url-state.js";
import { overscroll, overscrollState, RESIST_PX, IDLE_MS } from "/m/overscroll.js";
import { setupSidebar } from "/m/sidebar.js";

const refreshViewPicker = headerPicker(document.getElementById("review-layout"));

// Ordinary API calls: long enough for a slow tmux host, short enough that a dead link
// surfaces as an error before the user retries by hand.
const REQUEST_TIMEOUT_MS = 8000;
// Long polls (/api/state, /live) must outlast the server's 25s hold, or every idle poll
// would abort just before the server answers.
const LONG_POLL_TIMEOUT_MS = 35000;
// How long "Answer sent" stays (and the option buttons stay disabled) before we trust
// the pane's own state again — covers the round trip to the agent and back.
const ANSWER_PENDING_MS = 10000;
// Being this close to the bottom of the terminal counts as following it, so a frame
// keeps the view pinned to the tail; further up, the user is reading and we leave it.
const FOLLOW_SLACK_PX = 48;
const VERSION_POLL_MS = 5000;

// Inline Lucide paths, matching the existing UI; no external assets behind IAP.
const LUCIDE = {
  terminal: '<path d="m4 17 6-5-6-5M12 19h8"/>',
  layers: '<path d="m12 3 10 6-10 6L2 9l10-6ZM2 15l10 6 10-6M2 12l10 6 10-6"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4M12 17h.01"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  chevronUp: '<path d="m18 15-6-6-6 6"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  target: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
  pencil: '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497zM15 5l4 4"/>',
  back: '<path d="m12 19-7-7 7-7M5 12h14"/>',
  up: '<path d="m5 12 7-7 7 7M12 19V5"/>',
  down: '<path d="m5 12 7 7 7-7M12 5v14"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  check: '<path d="m20 6-11 11-5-5"/>',
  circle: '<circle cx="12" cy="12" r="9"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  x: '<path d="m18 6-12 12M6 6l12 12"/>',
  ellipsis: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
  trash: '<path d="M10 11v6M14 11v6M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  bell: '<path d="M10.3 21h3.4M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/>',
  mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/>',
  message: '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
  clipboard: '<rect width="8" height="4" x="8" y="2" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>',
  paperclip: '<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  keyboard: '<rect width="20" height="12" x="2" y="6" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h.01M18 14h.01M9 14h6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  book: '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/>',
  pointer: '<path d="M4.037 4.688a.495.495 0 0 1 .651-.651l16 6.5a.5.5 0 0 1-.063.947l-6.124 1.58a2 2 0 0 0-1.438 1.435l-1.579 6.126a.5.5 0 0 1-.947.063z"/>',
  cursor: '<path d="M17 22h-1a4 4 0 0 1-4-4V6a4 4 0 0 1 4-4h1M7 22h1a4 4 0 0 0 4-4v-1M7 2h1a4 4 0 0 1 4 4v1"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  panel: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
  dashboard: '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
  rows: '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/><path d="M14 4h7M14 9h7M14 15h7M14 20h7"/>',
  unfold: '<path d="m7 15 5 5 5-5M7 9l5-5 5 5"/>',
  fold: '<path d="m7 20 5-5 5 5M7 4l5 5 5-5"/>',
  arrowUpDown: '<path d="m21 16-4 4-4-4M17 20V4M3 8l4-4 4 4M7 4v16"/>',
  bot: '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2M20 14h2M15 13v2M9 13v2"/>',
};
const licon = (name, size = 20) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${LUCIDE[name]}</svg>`;
const $ = (id) => document.getElementById(id);
const text = (node, value = "") => { if (node.textContent !== String(value)) node.textContent = value; };
const html = (node, value) => { if (node._html !== value) { node.innerHTML = value; node._html = value; } };
const show = (id, visible) => { $(id).hidden = !visible; };
const icon = (id, name) => html($(id), licon(name));
const paneUrl = (id, path) => `/api/panes/${encodeURIComponent(id)}/${path}`;
const LOGOS = { claude: "/claude.png", codex: "/openai.svg", gemini: "/gemini.svg", opencode: "/opencode.svg", omp: "/omp.svg", shell: "/bash.png" };
const NO_TMUX = "tmux is not running on this host (it does not survive a reboot).";
const EMPTY_MESSAGE = { all: "No tmux panes are open.", attention: "Nothing needs your attention.", running: "No panes are running.", recent: "No recently active panes." };
// The desktop workspace shows context alongside the live terminal; phones retain tabs.
const WIDE = matchMedia("(min-width: 1100px)");
// The wide Layout picker's choice, global across panes and reloads: a split, or the one tab
// the focus layout shows. "focus" is what the Overview choice was saved as before the tab was.
let reviewLayout = "auto";
try { const saved = localStorage.getItem("tmuxrc-review-layout"); if (["auto", "side", "stack", "summary", "terminal"].includes(saved)) reviewLayout = saved; else if (saved === "focus") reviewLayout = "summary"; } catch {}
const focusChoice = () => ["summary", "terminal"].includes(reviewLayout);
// The tab a pane opens on when its URL names none: the chosen one, so it survives pane switches.
const defaultView = () => WIDE.matches && reviewLayout === "terminal" ? "terminal" : "summary";
function effectiveLayout() {
  if (!WIDE.matches || focusChoice()) return "focus";
  if (reviewLayout !== "auto") return reviewLayout;
  if (view === "terminal") return "focus"; // Honor an explicit terminal deep link.
  const { width, height } = document.getElementById("detail").getBoundingClientRect();
  if (width >= 900 && height >= 480) return "side";
  if (width >= 480 && height >= 720) return "stack";
  return "focus";
}
const reviewing = () => effectiveLayout() !== "focus";
const terminalVisible = () => reviewing() || view === "terminal";
const overviewVisible = () => reviewing() || view === "summary";
const drafts = new Map();
let dashboard = false;
const dashboardVisible = () => !active && (WIDE.matches || dashboard);
let panes = [], active = null, view = "summary", filter = "all", loaded = false, booted = false, tmuxRunning = true;
let focusPushComposer = false;
let sort = "updated";
let sending = false, prefix = "C-b", stateController, detailController, detailId = null;
let streamedLayout = null;
let eventsKey = null, latestCapture = "", fontSize = 13;
// Answers sent and not yet reflected, per pane (pane id -> question signature): the sidebar
// can answer several panes inside one hold, and each keeps its own.
const pendingAnswers = new Map();
// Per-line nodes under #capture, in document order; each caches the markup last written
// to it (_html). Set when a frame was held back for a selection, so selectionchange
// knows there is something to catch up on.
let captureLines = [], captureDirty = false;
let latestFrame = "", paintedFrame = "";
let wheel = overscrollState(), wheelQueued = 0, wheelInFlight = 0, wheelSpring = 0, touchY = null, wheelLine = Promise.resolve();
const liveSession = (() => {
  try { return crypto.randomUUID(); }
  catch { return ""; } // CSPRNG-random or omitted, never guessed.
})();

function draft(id = active) {
  if (!drafts.has(id)) drafts.set(id, new Composer(() => { text($("draft-status"), "Draft"); updateComposer(); }, notice));
  return drafts.get(id);
}

// Keep interactive nodes in place while long-polls repaint: a mobile tap may span a poll.
function reconcile(parent, values, keyOf, build, update) {
  const previous = new Map([...parent.children].map((node) => [node._key, node]));
  values.forEach((value, index) => {
    const key = keyOf(value, index);
    const node = previous.get(key) || build(value);
    previous.delete(key);
    node._key = key;
    update(node, value, index);
    if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] || null);
  });
  previous.forEach((node) => node.remove());
}

async function request(url, options = {}, timeout = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  const external = options.signal;
  if (external?.aborted) controller.abort();
  external?.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(abort, timeout);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    if (!response.ok) {
      // Carry the server's own explanation (FastAPI puts it in `detail`) on the error.
      // Without it a caller can only say something generic, which is how a launcher
      // that isn't on the daemon's PATH used to surface as an unrelated focus error.
      const detail = await response.json().then((body) => body?.detail, () => null);
      const error = new Error(`Request failed (${response.status})`);
      error.status = response.status;
      if (typeof detail === "string" && detail) error.detail = detail;
      throw error;
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
    external?.removeEventListener("abort", abort);
  }
}
const post = (url, body) => request(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
// Browser failures -> /api/client-error -> OTel (#57): a phone has no devtools, so a swallowed
// mic denial or uncaught exception is otherwise invisible. Best-effort, deduped and capped, and
// a failed report is never itself reported (no recursion).
const reported = new Set();
function reportError(kind, detail) {
  const message = String(detail?.message ?? detail ?? ""), key = `${kind}|${message}`;
  if (reported.has(key) || reported.size >= 50) return;
  reported.add(key);
  fetch("/api/client-error", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ kind, name: detail?.name, message: message || undefined, session: liveSession || undefined }) }).catch(() => {});
}
window.addEventListener("error", (e) => reportError("onerror", e.error || e.message));
window.addEventListener("unhandledrejection", (e) => reportError("unhandledrejection", e.reason));
function pause(ms, signal) {
  return new Promise((resolve) => {
    const finish = () => { clearTimeout(timer); signal?.removeEventListener("abort", finish); resolve(); };
    const timer = setTimeout(finish, ms);
    if (signal?.aborted) finish();
    else signal?.addEventListener("abort", finish, { once: true });
  });
}
function notice(message = "") { text($("notice"), message); show("notice", !!message); }

// Every user-driven move goes through here: the URL is written first, then the view is
// routed synchronously (pushState/replaceState fire no hashchange). Back/Forward and edits
// by hand reach route() through hashchange instead. Never call this from a poll.
function navigate(id = null, nextView = "summary", { mode } = {}) {
  const next = formatHash({ pane: id, view: nextView, dashboard: nextView === "dashboard", filter, sort });
  const current = location.hash.slice(1);
  if (next !== current) {
    history[`${mode || historyMode(current, next)}State`](null, "", next ? `#${next}` : location.pathname + location.search);
  }
  route();
}

// Filter and sort change the list, not the screen: with the sidebar beside an open pane
// (wide layout) the pane, its tab and a dashboard view must survive them; on a phone a
// filter tap is a request for the list.
const stayPut = () => navigate(active, active ? view : dashboard && WIDE.matches ? "dashboard" : "summary");

// Leaving a pane the user did not choose to leave: it closed under them, or a deep link
// named one that is gone. Replace, never push: Back would land on the dead deep link and
// bounce straight out again. `id` is the pane the caller believes is on screen; stillOnPane
// rejects the call when the user has already moved on and only the queued hashchange is late
// (see pane-model.js).
function leaveMissingPane(id) {
  if (!stillOnPane(location.hash, id)) return;
  navigate(null, "summary", { mode: "replace" });
  notice("That pane is no longer available.");
}

function route() {
  const wasDashboardVisible = dashboardVisible();
  const state = parseHash(location.hash, defaultView());
  const changed = state.pane !== active;
  active = state.pane;
  focusPushComposer = state.compose;
  ({ dashboard, view, filter, sort } = state);
  if (active) returnPane = null;
  $("sort").ariaLabel = `Sort: ${sort === "updated" ? "Last updated" : "Session order"}`;
  if (changed) {
    if (active) $("reply").replaceWith(draft().editor);
    $("overview").scrollTop = 0;
    text($("draft-status"), "");
    show("keys", false);
    $("keyboard").setAttribute("aria-expanded", "false");
    notice();
  }
  // restartDetail first: it swaps the detail controller, and render()'s loadEvents keys
  // off it. The other way round the events request left on the old controller and was
  // aborted at once, so every navigation fetched events twice.
  restartDetail();
  render();
  if (dashboardVisible() && !wasDashboardVisible) {
    refreshHistory(true);
  }
  if (active && changed) {
    const id = active;
    // A dead deep link selects before state can say so; a 404 is the gone-pane path, and a
    // rejection after the user moved on must not overwrite whatever notice that left.
    post(paneUrl(id, "select")).catch((error) => {
      if (error.status === 404) leaveMissingPane(id);
      else if (stillOnPane(location.hash, id)) notice("Could not focus this pane on the host.");
    });
  }
  if (changed && active) $("pane-title").focus({ preventScroll: true }); // land in the pane, on its name: focusing Back painted its ring on open
}

function makeRow(pane) {
  const button = document.createElement("button");
  button.className = "pane-row";
  button.innerHTML = `<span class="pane-icon"><img alt=""></span><span class="row-body"><span class="row-title"><strong></strong><span class="row-age"></span>${licon("chevron", 14)}</span><span class="row-status"></span><span class="row-meta"><span class="session-chip" hidden></span><span class="row-details"></span><span class="badge"></span></span></span>`;
  button.onclick = () => navigate(pane.pane_id);
  return button;
}
function updateRow(button, pane) {
  if (pane.pane_id === active) button.setAttribute("aria-current", "true");
  else button.removeAttribute("aria-current");
  button.classList.toggle("needs-you", needsYou(pane));
  button.classList.toggle("fresh", pane.activity === "idle" && isRecent(pane));
  const logo = button.querySelector(".pane-icon img");
  logo.alt = pane.tool || "tmux";
  markWorking(logo, pane, LOGOS);
  text(button.querySelector("strong"), paneName(pane));
  text(button.querySelector(".row-age"), age(pane));
  const badge = button.querySelector(".badge");
  badge.className = `badge ${activityClass(pane)}`;
  text(badge, activityLabel(pane));
  text(button.querySelector(".row-status"), paneActivity(pane) || "No recent activity");
  const sessionChip = button.querySelector(".session-chip");
  sessionChip.hidden = sort !== "updated" || !pane.session; // a session label already says it
  text(sessionChip, pane.session || "");
  sessionChip.title = pane.session ? `Session: ${pane.session}` : "";
  text(button.querySelector(".row-details"), [pane.tool, pane.model, pane.window_index !== "" && pane.window_index != null ? `Window ${pane.window_index}` : ""].filter(Boolean).join(" / "));
}
const renderSidebar = setupSidebar({ licon, reconcile, text, html, renderItems, logos: LOGOS, navigate, notice,
  active: () => active, sending: () => sending, answers: answerOptions, answered: isAnswered, answer, compose,
  setFilter: (value) => { filter = value; stayPut(); }, repaint: () => renderList() });
function emptyMessage(query) {
  if (!loaded) return "Loading sessions...";
  if (!booted) return "Reading terminal sessions...";
  if (query) return "No matching panes.";
  if (!tmuxRunning) return NO_TMUX;
  return EMPTY_MESSAGE[filter] || EMPTY_MESSAGE.all;
}
function renderList() {
  show("clear-search", !!$("search").value);
  const query = $("search").value.trim().toLowerCase();
  const subset = panes.filter((p) => matchesFilter(p, filter) && matchesSearch(p, query));
  // One composer per pane, in either layout: an open pane takes over its sidebar Reply draft.
  const inline = renderSidebar.drafts.get(active);
  if (inline) { draft().append(inline); inline.editor.remove(); renderSidebar.drafts.delete(active); }
  renderSidebar.answers.prune(subset);
  if (WIDE.matches) renderSidebar(subset, query, filter);
  else renderPhoneList(subset);
  show("empty", !subset.length);
  text($("empty"), emptyMessage(query));
  const waiting = panes.filter(needsYou).length;
  text($("all-count"), panes.length);
  text($("attention-count"), waiting);
  text($("running-count"), panes.filter(isRunning).length);
  text($("recent-count"), panes.filter((pane) => isRecent(pane)).length);
  $("list-nav").querySelectorAll("button").forEach((button) => button.setAttribute("aria-pressed", !dashboard && filter === button.dataset.filter));
  $("dashboard-tab").setAttribute("aria-pressed", String(dashboard));
  $("dash-nav").setAttribute("aria-pressed", String(dashboardVisible()));
  // No panes means no session to open a window in: offer to start one instead — the
  // only way back after a reboot, when no tmux server is running at all.
  // Not before boot: an empty `panes` then would open the dialog in New session mode.
  $("new-window").disabled = !booted;
  const startable = booted && !panes.length;
  show("list-start", startable && !WIDE.matches); // wide: the dashboard's button says it
  show("landing-start", startable);
}
// Needs-you rows are cards answerable in place (the sidebar's answers and Reply), left in their
// sorted place: pinning them on top shoved the list around whenever a pane started asking.
function renderPhoneList(subset) {
  const rows = sort === "updated" ? subset.sort((a, b) => lastActivity(b) - lastActivity(a))
    : [...new Set(subset.map((p) => p.session))].flatMap((session) => [{ heading: session || "Session", key: `session:${session}` }, ...subset.filter((p) => p.session === session)]);
  reconcile($("pane-list"), rows, (p) => p.heading ? p.key : needsYou(p) ? `ask:${p.pane_id}` : p.pane_id, (p) => {
    if (p.heading) { const node = document.createElement("h2"); node.className = "session-label"; return node; }
    if (!needsYou(p)) return makeRow(p);
    const card = document.createElement("div");
    card.className = "pane-card";
    card.append(makeRow(p));
    renderSidebar.answers.add(card);
    return card;
  }, (node, p) => {
    if (p.heading) return text(node, p.heading);
    node._p = p;
    updateRow(node.querySelector(".pane-row") || node, p);
    if (node.matches(".pane-card")) renderSidebar.answers.update(node, p);
  });
}

// The wide-screen main column before a pane is picked. Deliberately the SAME numbers the
// filter tabs already show — a second count that disagreed with the tabs would be worse
// than no count — plus panes blocked on you. Rows navigate exactly like sidebar rows.
function landingRows(id, subset) {
  show(id, !!subset.length);
  reconcile($(id + "-list"), subset, (p) => p.pane_id, () => {
    const b = document.createElement("button");
    b.className = "landing-row";
    b.innerHTML = '<span class="t"></span><span class="m"></span>';
    return b;
  }, (node, p) => {
    node.onclick = () => navigate(p.pane_id);
    text(node.querySelector(".t"), paneName(p));
    text(node.querySelector(".m"), `${activityLabel(p)} · ${p.session} / ${p.window_name || p.pane_id}`);
  });
}

function renderLanding() {
  const waiting = panes.filter(needsYou);
  show("landing-back", panes.some((p) => p.pane_id === returnPane));
  // With panes, the atlas speaks for itself (its filter row is labelled); the heading is
  // only for the loading and empty states, and hides itself when blank (style.css).
  text($("landing-title"), !booted ? "Reading sessions…" : panes.length ? "" : "No panes yet");
  text($("landing-sub"), !booted ? "Saved history is available while the current inventory loads." : panes.length
    ? "" : tmuxRunning ? "No tmux panes are open. Start a session and it will appear here."
      : `${NO_TMUX} Start a session to bring it back.`);
  renderAtlas($("session-atlas"), panes, navigate, LOGOS, term => {
    $("search").value = term;
    filter = "all";
    renderList();
    navigate();
    $("sessions").scrollTop = $("side-list").scrollTop = 0;
  }, licon);
  landingRows("landing-attention", waiting);
}

// Drag the seam between the sidebar and the main column. Width is a CSS variable the
// grid clamps, so a stored value from a wider window can never strand the layout, and
// persistence is per browser (localStorage) because it is a per-screen preference, not
// something the daemon should know. Arrow keys move it too: the handle is a focusable
// separator, and a pointer-only affordance would be unreachable from the keyboard.
const SIDEBAR_KEY = "tmuxrc-sidebar", SIDEBAR_DEFAULT = 300;
// Mirrors the CSS clamp() in style.css, which stays the real guard: it holds with JS off
// and against a hand-edited localStorage value. These bounds exist so the separator can
// report a truthful value to assistive tech. MAX can fall BELOW MIN on a narrow window
// (46vw of 390px is 179px), and clamp() resolves that by letting the minimum win — so
// the order here is min-last, matching CSS, not Math.min(Math.max(...)).
const SIDEBAR_MIN = 260, SIDEBAR_MAX = () => window.innerWidth * 0.46;
const clampSidebar = (px) => Math.round(Math.max(Math.min(px, SIDEBAR_MAX()), SIDEBAR_MIN));
// The width the user chose, unclamped. Kept apart from the rendered value because the
// clamp is viewport-dependent: narrowing the window must not erase the desktop width, so
// what we store and replay is always the intent, and the clamp is applied on the way out.
let sidebarWidth = SIDEBAR_DEFAULT;
// `persist` is false for the boot restore and for resize: only a drag or an arrow key is
// the user choosing a width, so a phone visit cannot overwrite the desktop's.
function setSidebar(px, persist = true) {
  const width = clampSidebar(px);
  // A drag or an arrow key records the width the user actually SAW, not the raw pointer
  // position: over-dragging past the ceiling must not bank a width that a later resize
  // would suddenly honour. A replay (persist false) keeps the intent it was handed, which
  // is the whole point of replaying it.
  sidebarWidth = persist ? width : px;
  document.documentElement.style.setProperty("--sidebar", width + "px");
  // A focusable role="separator" is a widget, so it owes screen readers a value.
  const handle = $("divider");
  handle.setAttribute("aria-valuenow", width);
  handle.setAttribute("aria-valuemin", SIDEBAR_MIN);
  handle.setAttribute("aria-valuemax", Math.max(Math.round(SIDEBAR_MAX()), SIDEBAR_MIN));
  if (persist) { try { localStorage.setItem(SIDEBAR_KEY, String(width)); } catch {} }
  return width;
}
let storedSidebar = 0;
try { storedSidebar = Number(localStorage.getItem(SIDEBAR_KEY)); } catch {}
setSidebar(storedSidebar > 0 ? storedSidebar : SIDEBAR_DEFAULT, false);
// The clamp moves with the viewport, so replay the intent whenever it changes: a tab that
// loaded narrow and was widened gets the saved desktop width back rather than the value
// the narrow clamp had squeezed it to, and aria-valuenow follows the seam it describes.
addEventListener("resize", () => setSidebar(sidebarWidth, false));

// Pointer plumbing every seam shares: primary button only (a right-click is a context-menu
// gesture, and preventDefault() would swallow it), capture for the drag, and every ending
// clears the dragging state. pointerup/pointercancel are the ordinary endings (capture
// guarantees one even if the pointer leaves the window); lostpointercapture is the backstop
// for the rest: crossing the wide breakpoint mid-drag hides the seam, which drops capture
// without firing either. `bodyClass` matters for the sidebar: body.resizing kills
// pointer-events on the main column, so it sticking on would deaden the whole pane.
function seam(handle, move, end = () => {}, bodyClass = "") {
  handle.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("dragging");
    if (bodyClass) document.body.classList.add(bodyClass);
  });
  handle.addEventListener("pointermove", (e) => { if (handle.hasPointerCapture(e.pointerId)) move(e); });
  const stop = (e) => {
    if (!handle.classList.contains("dragging")) return;
    if (handle.hasPointerCapture(e.pointerId)) handle.releasePointerCapture(e.pointerId);
    handle.classList.remove("dragging");
    if (bodyClass) document.body.classList.remove(bodyClass);
    end(e);
  };
  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) handle.addEventListener(type, stop);
}

const divider = $("divider");
// Measured from the app's left edge, not the viewport, so it stays correct if the layout
// ever gains an outer margin.
seam(divider, (e) => setSidebar(e.clientX - $("app").getBoundingClientRect().left), undefined, "resizing");
divider.addEventListener("keydown", (e) => {
  const step = { ArrowLeft: -16, ArrowRight: 16 }[e.key];
  if (!step) return;
  e.preventDefault();
  // From the rendered width, not the stored one: the clamp may already be overriding it,
  // and an arrow press should move the seam the user can actually see.
  setSidebar($("sessions").getBoundingClientRect().width + step);
});

// One seam for both arrangements. Store dimensions separately so rearranging never
// turns a preferred column width into an implausibly tall overview.
const reviewSizes = { side: 360, stack: 280 };
try {
  for (const mode of Object.keys(reviewSizes)) {
    const saved = Number(localStorage.getItem(`tmuxrc-review-${mode}`));
    if (Number.isFinite(saved) && saved > 0) reviewSizes[mode] = saved;
  }
} catch {}
const reviewDivider = $("review-divider");
function sizeReview(persist = false, requested = reviewSizes[effectiveLayout()]) {
  if (!reviewing()) return;
  const mode = effectiveLayout(), side = mode === "side", rect = $("detail").getBoundingClientRect();
  const min = side ? 240 : 120;
  const max = Math.max(min, Math.floor(side ? rect.width * .45 : rect.height * .5));
  const size = Math.round(Math.max(min, Math.min(max, requested)));
  $("detail").style.setProperty("--review-size", `${size}px`);
  reviewDivider.setAttribute("aria-orientation", side ? "vertical" : "horizontal");
  for (const [key, value] of Object.entries({ min, max, now: size })) reviewDivider.setAttribute(`aria-value${key}`, value);
  if (persist) {
    reviewSizes[mode] = size;
    try { localStorage.setItem(`tmuxrc-review-${mode}`, String(size)); } catch {}
    sizeFleet(); // a taller overview takes its room from the terminal, which the split's cap protects
  }
}
// The composer and heading too: a multiline draft, the key row or a wrapped title grows them without resizing #detail,
// and the terminal it squeezes is what the split's cap protects.
const detailResize = new ResizeObserver(() => {
  if (active && streamedLayout !== effectiveLayout()) {
    restartDetail(); render();
  } else { sizeReview(); if (WIDE.matches) sizeFleet(); }
});
detailResize.observe($("detail"));
detailResize.observe($("composer"));
detailResize.observe($("heading"));
$("mobile-view-toggle").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-view]");
  if (!button) return;
  $("review-layout").value = button.dataset.view;
  $("review-layout").dispatchEvent(new Event("change", { bubbles: true }));
});
$("review-layout").onchange = (e) => {
  const choice = e.target.value;
  if (WIDE.matches) {
    reviewLayout = choice;
    try { localStorage.setItem("tmuxrc-review-layout", reviewLayout); } catch {}
  }
  if (choice === "auto") view = "summary";
  if (["summary", "terminal"].includes(choice)) view = choice;
  navigate(active, view);
};
seam(reviewDivider, (e) => {
  const rect = $("detail").getBoundingClientRect();
  sizeReview(true, effectiveLayout() === "side" ? rect.right - e.clientX : e.clientY - $("overview").getBoundingClientRect().top);
});
reviewDivider.onkeydown = (e) => {
  const steps = effectiveLayout() === "side" ? { ArrowLeft: 16, ArrowRight: -16 } : { ArrowUp: -16, ArrowDown: 16 };
  const step = steps[e.key];
  if (!step) return;
  e.preventDefault(); sizeReview(true, Number(reviewDivider.getAttribute("aria-valuenow")) + step);
};
reviewDivider.ondblclick = () => sizeReview(true, effectiveLayout() === "side" ? 360 : 280);

// The fleet chart docks under the pane on a wide screen, behind a seam: drag it to any
// height and release snaps to strip, medium or tall; a double-click or the chevron folds
// and unfolds. `fleetHeight` is the chosen height, persisted per screen; the shown one is
// capped so the pane's flexible row (the terminal, or the overview alone) keeps
// FLEET_ROOM, which a short window or a tall stacked overview would otherwise take.
const FLEET_KEY = "tmuxrc-fleet-height", STRIP = 40, MEDIUM = 300, FLEET_ROOM = 120;
let fleetHeight = STRIP, fleetShown = STRIP, returnPane = null, returnView = "summary";
try { fleetHeight = Number(localStorage.getItem(FLEET_KEY)) || STRIP; } catch {}
const fleetHandle = $("fleet-handle");
function sizeFleet(px = fleetHeight, mode = "") {
  // Hidden, the grid reports unresolved tracks (auto, minmax) that would parse to NaN.
  if (!WIDE.matches || !active) return;
  // From the grid's own tracks, not the flexible row's current height: after a big window
  // shrink that row may already be clamped to zero, and would hide the overflow.
  // The stacked overview's row can itself be squeezed below its chosen size, so count that.
  const tracks = getComputedStyle($("detail")).gridTemplateRows.split(" ").map(parseFloat);
  const stack = $("detail").dataset.layout === "stack", flex = stack ? 3 : 2; // the minmax(0, 1fr) row
  if (stack) tracks[2] = Math.max(tracks[2], parseFloat($("detail").style.getPropertyValue("--review-size")) || 0);
  const fixed = tracks.reduce((total, h, i) => i === flex || i === tracks.length - 1 ? total : total + h, 0);
  const cap = Math.max(STRIP, $("detail").clientHeight - fixed - FLEET_ROOM);
  const snaps = [STRIP, Math.min(MEDIUM, cap), cap];
  if (mode === "snap") px = snaps.reduce((best, snap) => Math.abs(snap - px) < Math.abs(best - px) ? snap : best);
  fleetShown = Math.round(Math.max(STRIP, Math.min(snaps[2], px)));
  if (mode) {
    fleetHeight = fleetShown;
    try { localStorage.setItem(FLEET_KEY, String(fleetHeight)); } catch {}
  }
  $("detail").style.setProperty("--fleet-h", `${fleetShown}px`);
  for (const [key, value] of Object.entries({ min: STRIP, max: snaps[2], now: fleetShown })) fleetHandle.setAttribute(`aria-value${key}`, value);
  renderFleetSplit();
}
const foldFleet = () => sizeFleet(fleetShown > STRIP ? STRIP : MEDIUM, "snap");
seam(fleetHandle, (e) => sizeFleet($("detail").getBoundingClientRect().bottom - e.clientY, "drag"), () => sizeFleet(fleetShown, "snap"));
fleetHandle.ondblclick = foldFleet;
fleetHandle.onkeydown = (e) => {
  const step = { ArrowUp: 16, ArrowDown: -16 }[e.key];
  if (!step) return;
  e.preventDefault(); sizeFleet(fleetShown + step, "drag");
};
// The strip's Dashboard swaps the pane for the dashboard page; its Back returns to the pane.
// Opening it again from the dashboard keeps the pane Back returns to.
const openDashboard = () => { if (active) [returnPane, returnView] = [active, view]; navigate(null, "dashboard"); };
$("landing-back").onclick = () => navigate(returnPane, returnView); // the tab the user left, too
html($("landing-back"), licon("back", 18));
function renderFleetSplit() {
  if (!WIDE.matches || !active) return;
  renderFleet($("fleet"), panes, { open: fleetShown > STRIP, icon: licon, toggle: foldFleet, dashboard: openDashboard });
}
const refreshHistory = (force) => refreshAtlasHistory(request, () => { if (dashboardVisible()) renderLanding(); renderFleetSplit(); }, force);

function render() {
  const pane = panes.find((p) => p.pane_id === active);
  const inPane = !!active;
  // Once state has shown us the launched pane the grace record is spent — wherever the
  // user happens to be looking. Keyed on the pane list, not on `active`: a launch the
  // user navigated away from before the first poll would otherwise keep its exemption,
  // and coming back to that pane after it had exited would find it still held on screen
  // by its own birth. Presence is imperfect evidence when tmux recycles an id — see the
  // note at the eviction below; it is the only evidence the client has.
  if (launched && panes.some((p) => p.pane_id === launched.id)) launched = null;
  // On a wide screen the list never leaves, so it is not "list OR pane" any more:
  // the sidebar stays up (filters live in it, so the tab bar is phone-only), and Back
  // has nothing to go back TO — the sidebar it would return you to is already there.
  // The brand keeps its slot for the same reason. Narrow is unchanged.
  const wide = WIDE.matches;
  show("sessions", (!inPane && !dashboard) || wide); show("list-nav", !inPane && !wide);
  const list = !inPane && !dashboard; // a phone's list screen: the only one with its title and sort
  show("brand", wide || (!inPane && dashboard)); show("list-title", list); show("sort", list);
  show("back", inPane && !wide); show("close-pane", wide); show("heading", inPane); show("detail", inPane);
  // The main column is never blank on a wide screen: with no pane chosen it answers the
  // question the sidebar cannot, which is what the whole fleet is doing right now.
  show("landing", dashboardVisible());
  show("divider", wide); show("fleet", wide); show("fleet-handle", wide);
  renderList();
  if (!inPane) { if (dashboardVisible()) renderLanding(); return; }
  // The pane you were looking at is gone (you sent Ctrl-D, or it closed on the host).
  // Leaving you on it is a dead end: the header reads "Pane unavailable", the terminal
  // still shows the last frame, and every key is disabled — nothing to do but hit back.
  // Go back to the list instead, and only once the daemon is authoritative: `booted`
  // false means the inventory is still loading (startup, or a restart), where an absent
  // pane means "not yet", not "gone". Draft text is preserved by pruneDrafts.
  // ...unless the app itself created this pane moments ago and state has yet to catch up,
  // which is "not yet" too — see awaitingLaunch for why that is a deadline. Once the pane
  // HAS been seen the record is spent: a window that opens and then closes inside the
  // grace is an ordinary death, and must not be held on screen by its own birth.
  // Presence is the only evidence the clearing above has, and it is imperfect: tmux
  // recycles pane ids, so a launch that reuses one can match the previous occupant's
  // entry in a state poll that predates it, ending the grace early and showing the dead
  // card for a tick. Telling the two apart needs a birth token in /api/state that the
  // endpoint can echo back — a schema change, not a client fix, and not worth it for a
  // window this narrow: it costs a stale card until the next poll, and the next poll is
  // what it was waiting for anyway.
  // Has the daemon's word on this pane settled? Every "it's gone" wording below turns on
  // this rather than on `booted` alone, so the grace reads as "still loading" throughout
  // instead of announcing the pane unavailable on a screen we are deliberately holding.
  const settled = booted && !awaitingLaunch(launched, active);
  if (settled && loaded && !pane) { leaveMissingPane(active); return; }
  text($("pane-title"), (pane && paneName(pane)) || (settled ? "Pane unavailable" : "Loading pane"));
  text($("pane-location"), pane ? `${pane.session} / ${pane.window_name || pane.pane_id}` : "Waiting for session state");
  for (const id of ["pane-title", "pane-location"]) $(id).title = $(id).textContent; // both ellipsize: hover shows the full text
  $("detail").dataset.layout = effectiveLayout();
  const layouts = [["summary", "Overview"], ["terminal", "Terminal"]];
  if (wide) layouts.unshift(["auto", "Auto"], ["side", "Side by side"], ["stack", "Overview above"]);
  const picker = $("review-layout");
  if (picker.options.length !== layouts.length) picker.replaceChildren(...layouts.map(([value, label]) => new Option(label, value)));
  picker.value = wide && !focusChoice() ? reviewLayout : view;
  refreshViewPicker();
  $("mobile-view-toggle").querySelectorAll("button").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  });
  show("review-divider", reviewing());
  show("overview", overviewVisible()); show("terminal", terminalVisible());
  sizeReview(false);
  if (wide) sizeFleet();
  text($("activity"), pane ? activityLabel(pane) : settled ? "Unavailable" : "Loading");
  $("activity").className = `badge ${pane ? activityClass(pane) : "unknown"}`;
  text($("tool"), pane?.tool || "");
  const missing = loaded && !pane ? (settled ? "This pane is no longer available." : "Reading terminal sessions...") : "Waiting for activity...";
  const headline = paneHeadline(pane) || missing;
  html($("status-line"), linkifyText(headline));
  const summary = pane?.session_summary && pane.session_summary !== headline ? pane.session_summary : "";
  html($("session-summary"), linkifyText(summary)); show("session-summary", !!summary);
  const elapsed = pane?.working?.elapsed ?? pane?.elapsed;
  const tokens = pane?.working?.tokens ?? pane?.tokens;
  text($("metadata"), paneMeta(pane));
  const chips = [pane?.model, pane?.context_pct != null ? `${pane.context_pct}% context` : "", pane?.cost,
    elapsed, tokens ? `${tokens} tokens` : "",
    ...(Array.isArray(pane?.status_entries) ? pane.status_entries.slice(0, 4) : []),
    ({ plan: "Plan mode", "accept-edits": "Accept edits", bypass: "Bypass permissions" })[pane?.mode],
    pane?.agents ? `${pane.agents} agents` : ""].filter(Boolean);
  reconcile($("session-chips"), chips, (_, i) => i, () => document.createElement("span"), (node, value) => text(node, value));
  show("question", !!pane?.question && needsYou(pane));
  const question = pane?.question;
  text($("prompt"), question?.ask || question?.prompt || "");
  // The widget's own rows, once and folded away: the restatement above is what to read.
  // Folded again for each new pane or command: an expanded one must not carry over.
  const command = $("question-command"), commandKey = `${active}\n${question?.context || ""}`;
  if (command._key !== commandKey) { command._key = commandKey; command.open = false; }
  command.hidden = !question?.context;
  text(command.lastChild, question?.context || "");
  const answered = !!pane && isAnswered(pane);
  show("answer-status", answered);
  text($("answer-status"), "Answer sent. Waiting for the pane...");
  reconcile($("options"), answerOptions(question), (o) => `${active}:${question.prompt}:${o.index}:${o.option}`, () => {
    const button = document.createElement("button");
    button.onclick = () => answer(active, button._option.option, button._option.index);
    return button;
  }, (button, option) => { button._option = option; text(button, option.option); button.disabled = sending || answered; });
  renderTasks(pane);
  renderRichContent(pane);
  updateComposer();
  if (focusPushComposer && pane?.question && needsYou(pane)) {
    focusPushComposer = false;
    requestAnimationFrame(() => $("reply").focus({ preventScroll: true }));
  }
  if (pane && overviewVisible()) loadEvents(pane);
}

function renderRichContent(pane) {
  const links = paneLinks(pane);
  show("link-section", !!links.length);
  reconcile($("links"), links, (link) => link.href, () => {
    const anchor = document.createElement("a");
    anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
    anchor.innerHTML = '<span></span><small></small>' + licon("chevron", 16);
    return anchor;
  }, (anchor, link) => {
    const host = new URL(link.href).host;
    anchor.href = link.href;
    text(anchor.firstChild, Array.from(String(link.text || host).replace(/[\u202A-\u202E\u2066-\u2069]/g, "")).slice(0, 160).join(""));
    text(anchor.querySelector("small"), link.detail || host);
  });
  const tables = (Array.isArray(pane?.tables) ? pane.tables : []).filter((table) => table && Array.isArray(table.rows));
  show("table-section", !!tables.length);
  // Reconcile inside stable scroll containers so a poll never resets a table's position.
  reconcile($("tables"), tables, (table, i) => `${i}:${table.title || ""}`, () => {
    const box = document.createElement("div"); box.className = "pane-table";
    box.innerHTML = '<h2></h2><div class="table-scroll" tabindex="0" role="region"><table><thead><tr></tr></thead><tbody></tbody></table></div>';
    return box;
  }, (box, table) => {
    text(box.firstChild, table.title || ""); box.firstChild.hidden = !table.title;
    box.querySelector(".table-scroll").setAttribute("aria-label", table.title || "Pane table");
    const headers = Array.isArray(table.headers) ? table.headers : [];
    box.querySelector("thead").hidden = !headers.length;
    reconcile(box.querySelector("thead tr"), headers, (_, i) => i, () => {
      const cell = document.createElement("th"); cell.scope = "col"; return cell;
    }, (cell, value) => html(cell, linkifyText(value)));
    reconcile(box.querySelector("tbody"), table.rows.filter(Array.isArray), (_, i) => i, () => document.createElement("tr"), (row, cells) => {
      reconcile(row, cells, (_, i) => i, () => document.createElement("td"), (cell, value) => html(cell, linkifyText(value)));
    });
  });
}

function renderTasks(pane) {
  const tasks = records(pane?.tasks), agents = records(pane?.subagents), copyables = records(pane?.copyables);
  show("task-section", !!tasks.length); show("agent-section", !!agents.length); show("copy-section", !!copyables.length);
  text($("task-count"), `${tasks.filter((t) => t.done).length}/${tasks.length}`);
  renderItems($("tasks"), tasks); renderItems($("agents"), agents, true);
  reconcile($("copyables"), copyables, (value, i) => `${i}:${value.label}`, () => {
    const node = document.createElement("div"); node.className = "copyable";
    node.innerHTML = '<div class="copy-heading"><strong></strong><button class="icon-button" aria-label="Copy text" title="Copy text">' + licon("clipboard") + '</button></div><pre></pre>';
    node.querySelector("button").onclick = async () => {
      try { await navigator.clipboard.writeText(node._value); notice("Copied to clipboard."); }
      catch { notice("Could not copy. Select the text to copy it."); }
    };
    return node;
  }, (node, value) => { node._value = value.text; text(node.querySelector("strong"), value.label); text(node.querySelector("pre"), value.text); });
}

// One line per task or sub-agent: the pane overview's lists, and the sidebar's agent rows.
// An open circle already says running (and its role=img label says it aloud), so only the
// other states are spelled out.
function renderItems(el, values, agents = false) {
  reconcile(el, values, (value, i) => `${i}:${value.text || value.label}`, () => {
    const node = document.createElement("div"); node.innerHTML = "<span></span><span></span><small></small>"; return node;
  }, (node, value) => {
    const done = itemDone(value);
    node.className = `${agents ? "agent" : "task"}${done ? " done" : ""}${agents && value.state === "compacting" ? " compacting" : ""}`;
    html(node.firstChild, licon(done ? "check" : "circle", 16));
    node.firstChild.setAttribute("role", "img");
    node.firstChild.ariaLabel = done ? "done" : agents ? value.state || "running" : "to do";
    text(node.children[1], value.text || value.label);
    node.title = value.text || value.label || "";
    text(node.lastChild, agents ? [value.state === "running" ? "" : value.state, value.elapsed, value.tokens ? `${value.tokens} tokens` : ""].filter(Boolean).join(" · ") : "");
  });
}

async function loadEvents(pane) {
  const key = `${pane.pane_id}:${pane.events_seq ?? pane.updated_at}`;
  // Same hidden-tab rule as restartDetail, which used to be the only path that ran first.
  if (eventsKey === key || document.hidden || !detailController || detailController.signal.aborted) return;
  eventsKey = key;
  const signal = detailController.signal;
  try {
    const events = await request(paneUrl(pane.pane_id, "events"), { signal });
    if (signal.aborted || active !== pane.pane_id || eventsKey !== key) return;
    reconcile($("events"), events.slice(-30).reverse(), (e, i) => `${e.ts}:${i}`, () => {
      const node = document.createElement("div"); node.className = "event"; node.innerHTML = "<time></time><p></p>"; return node;
    }, (node, event) => {
      text(node.firstChild, event.ts ? new Date(event.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "Recent");
      html(node.lastChild, linkifyText(event.text || ""));
    });
    show("events-empty", !events.length); text($("events-empty"), "No recent activity.");
  } catch {
    if (signal.aborted) return;
    eventsKey = null;
    show("events-empty", true); text($("events-empty"), "Activity unavailable. Reconnecting...");
  }
}

function restartDetail() {
  detailController?.abort();
  detailController = new AbortController();
  // A previously hidden detail may only get its real dimensions after render().
  // The resize observer restarts streams if that changes the resolved layout.
  streamedLayout = effectiveLayout();
  eventsKey = null;
  // Leaving the terminal, by pane, view or hidden page, sends a scrolled app home first, so the
  // watcher never keeps parsing old history nobody is looking at.
  if (detailId !== active || !terminalVisible() || document.hidden) wheelHome(detailId);
  if (detailId !== active) {
    $("events").replaceChildren(); text($("events-empty"), "Loading activity..."); show("events-empty", true);
    latestCapture = ""; clearCapture(); detailId = active;
  }
  if (!active || document.hidden) return;
  const pane = panes.find((p) => p.pane_id === active);
  if (terminalVisible()) streamTerminal(active, detailController.signal);
  if (pane && overviewVisible()) loadEvents(pane);
}

function clearCapture() { $("capture").replaceChildren(); captureLines = []; captureDirty = false; latestFrame = paintedFrame = ""; }
// One <span> per screen line, each ending in its own "\n" (except the last), so inside the
// <pre>'s `white-space: pre` the layout and copied text are exactly what one innerHTML of
// the whole frame gave — no extra CSS, and no block children to lose the newlines. The
// newline is part of the cached markup, so a line that becomes/stops being last is
// rewritten like any other change.
const lineMarkup = (lines) => lines.map((line, i) => i < lines.length - 1 ? `${line}\n` : line);
// Would this frame disturb the selection? Only if it changes a line the selection touches
// (or the line count, which reflows everything from there down). A selection outside
// #capture, or over lines the frame leaves alone, never holds the paint; one we cannot
// localize (no range, no lines painted yet) does, rather than risk eating it.
function selectionDirty(selection, lines) {
  const range = selection.rangeCount ? selection.getRangeAt(0) : null;
  if (!range) return true;
  if (!range.intersectsNode($("capture"))) return false;
  if (!captureLines.length || lines.length !== captureLines.length) return true;
  return captureLines.some((node, i) => node._html !== lines[i] && range.intersectsNode(node));
}
// Line-diff painter: only lines whose markup changed are written, so
// a busy pane repaints a few rows per frame and untouched rows keep their selection.
function paintLines(lines) {
  const pre = $("capture");
  lines.forEach((markup, i) => {
    let node = captureLines[i];
    if (!node) { node = document.createElement("span"); captureLines[i] = node; pre.appendChild(node); }
    if (node._html !== markup) { node._html = markup; node.innerHTML = markup; }
  });
  captureLines.splice(lines.length).forEach((node) => node.remove());
}
function paintCapture() {
  const lines = lineMarkup(renderCaptureLines(latestCapture, { color: true }));
  const selection = getSelection();
  if (selection && !selection.isCollapsed && selectionDirty(selection, lines)) { captureDirty = true; return; }
  captureDirty = false;
  const scroll = $("terminal-scroll");
  const follow = scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - FOLLOW_SLACK_PX;
  paintLines(lines);
  paintedFrame = latestFrame;
  if (follow) scroll.scrollTop = scroll.scrollHeight;
}
async function streamTerminal(id, signal) {
  let frame = "";
  text($("terminal-status"), "Connecting");
  while (!signal.aborted) {
    try {
      const query = new URLSearchParams({ frame });
      if (liveSession) query.set("session", liveSession);
      const data = await request(`${paneUrl(id, "live")}?${query}`, { signal }, LONG_POLL_TIMEOUT_MS);
      if (signal.aborted) return;
      frame = data.frame || "";
      if (typeof data.text === "string") { latestCapture = data.text; latestFrame = frame; paintCapture(); }
      text($("terminal-status"), "Live terminal");
      await pause(100, signal);
    } catch (error) {
      if (signal.aborted) return;
      if (error.status === 404) {
        // The daemon says this pane is gone, and it is the FIRST thing to know: the live
        // stream 404s the moment the pane dies, while /api/state may still be holding its
        // long poll. Leaving you on a dead pane — stale frame, disabled keys — is a dead
        // end, and render()'s own check cannot fire until the pane list catches up. Only
        // act when this is still the pane on screen; a stale controller must not yank you
        // out of a pane you have since switched to, which is leaveMissingPane's own guard.
        text($("terminal-status"), "Pane closed or not found");
        leaveMissingPane(id);
        return;
      }
      text($("terminal-status"), "Reconnecting...");
      frame = "";
      await pause(1500, signal);
    }
  }
}

function startState() {
  stateController?.abort();
  stateController = new AbortController();
  if (!document.hidden) {
    refreshHistory();
    pollState(stateController.signal);
  }
}
async function pollState(signal) {
  let version = null;
  while (!signal.aborted) {
    try {
      const data = await request(stateUrl(version || null), { signal }, LONG_POLL_TIMEOUT_MS);
      if (signal.aborted) return;
      version = Number.isFinite(data.version) && data.version > 0 ? data.version : null;
      panes = data.panes || []; loaded = true; booted = data.booted !== false; tmuxRunning = data.tmux_running !== false; prefix = data.prefix || "C-b";
      $("ctrl-b").hidden = data.prefix === "C-b"; // absent prefix: can't know it's C-b, so show it
      refreshHistory();
      pruneDrafts();
      text($("connection"), data.stale ? "Stalled" : "Live");
      $("connection").classList.toggle("online", !data.stale);
      const u = data.usage; // LLM spend this run: parser + voice, the debug readout the old UI kept under its status dot
      $("connection").title = (data.stale ? "Watcher stalled; pane summaries may be out of date" : "Connected")
        + (u ? ` | LLM $${u.cost.toFixed(3)}, ${Math.round((u.in_tokens + u.out_tokens + (u.live?.in_tokens || 0) + (u.live?.out_tokens || 0)) / 1000)}k tokens` : "");
      render();
      await pause(version ? 100 : 1500, signal);
    } catch {
      if (signal.aborted) return;
      version = null;
      text($("connection"), "Offline"); $("connection").classList.remove("online");
      await pause(1500, signal);
    }
  }
}

// Drafts for panes that have closed: drop the empty ones (revoking chip object URLs the
// way Composer.edited does) but keep any with content, so text is not lost if the pane
// reappears. The active pane's draft is the editor on screen, so it always stays.
function pruneDrafts() {
  for (const [id, value] of drafts) {
    if (id === active || panes.some((p) => p.pane_id === id)) continue;
    if (value.segments().length || value.pendingEnter) continue;
    value.files.forEach((_, chip) => URL.revokeObjectURL(chip.src));
    drafts.delete(id);
  }
}
function updateComposer() {
  if (!active) return;
  const available = panes.some((p) => p.pane_id === active);
  const value = draft();
  // Editability depends only on the pane existing: a non-editable div loses focus and
  // dismisses the phone keyboard on every send, and `sending` already guards re-entry.
  $("reply").contentEditable = String(available);
  $("reply").setAttribute("aria-disabled", String(!available));
  $("send").disabled = sending || !available || (!value.segments().length && !value.pendingEnter);
  $("attach").disabled = sending || !available;
  $("keys").querySelectorAll("button").forEach((button) => { button.disabled = sending || !available; });
}
// Returns whether the keys were DELIVERED. Most callers ignore it; the cursor walk
// (web/cursor-pick.js) cannot — a move it wrongly believes happened leaves every later
// step one row out and commits the wrong row.
async function sendKeys(body, answer = false, id = active) {
  if (sending || !panes.some((p) => p.pane_id === id)) return false;
  const signature = JSON.stringify(panes.find((p) => p.pane_id === id)?.question);
  let delivered = false;
  sending = true; notice(); render();
  try {
    await post(paneUrl(id, "send"), body);
    delivered = true;
    if (answer) {
      pendingAnswers.set(id, signature);
      setTimeout(() => { if (pendingAnswers.get(id) === signature) { pendingAnswers.delete(id); render(); } }, ANSWER_PENDING_MS);
    }
    if (active === id) text($("draft-status"), "Sent");
    startState();
  } catch { notice("Delivery could not be confirmed. Check the terminal before retrying."); }
  finally { sending = false; render(); }
  return delivered;
}

// A closing question's tappable answers, for the pane's question card and the sidebar's
// Needs you cards alike. Free-text escapes ("Type something") are left to the composer.
function answerOptions(question) {
  return (Array.isArray(question?.options) ? question.options : []).map((option, index) => ({ option, index })).filter(({ option }) => typeof option === "string" && option.trim() && !/^(type\b|other\b|something else|let me|custom|free.?text|write )/i.test(option.trim()));
}
function isAnswered(pane) { return pendingAnswers.get(pane.pane_id) === JSON.stringify(pane.question); }
function answer(id, option, index) {
  const current = panes.find((p) => p.pane_id === id);
  if (!current?.question || !needsYou(current)) return;
  // A cursor list answers to neither of the other two styles: the row's text and a
  // digit both land in the picker's search box. It needs a verified walk, and the walk
  // stops the moment its pane is off screen, so a sidebar tap opens the pane first.
  if (current.question.answer_style === "cursor") { if (active !== id) navigate(id); pickCursorRow(cursorIO(id), option, index); }
  else sendKeys(answerBody(current.question, option, index), true, id);
}

// This surface's half of the shared cursor walk. No send here sets `pendingAnswers`:
// gating the option buttons on the first Down would disable the very row the walk is
// working toward. What keeps a second tap from starting a rival walk is not this surface
// at all — `sending` is released between every step — but the module's own one-at-a-time
// lock, which is where that belongs since both surfaces have the same gap.
function cursorIO(id) {
  // Two separate things, both needed. sendKeys takes the captured id, so no move can ever
  // be posted to a pane the walk was not started for — it defaults to `active` for every
  // other caller, but a default resolved at call time is exactly what a multi-second walk
  // must not rely on. And question() reports nothing once `active` has moved off that
  // pane, which STOPS the walk: a picker the user has navigated away from should not go on
  // being driven, let alone committed, out of sight.
  const pane = () => (active === id ? panes.find((p) => p.pane_id === id) : null);
  return {
    question: () => pane()?.question || null,
    parsedAt: () => pane()?.parsed_at || 0,
    sendKey: (k) => sendKeys({ keys: k, enter: false, literal: false }, false, id),
    sendText: (t) => sendKeys({ keys: t, enter: false, literal: true }, false, id),
    note: notice,
  };
}
// Send a draft to pane `id`: the pane's own composer and a sidebar card's Reply both come
// through here, so they share one endpoint and one confirmation. Resolves to delivered.
async function compose(id, value) {
  const segments = value.segments();
  sending = true; notice(); render();
  try {
    const form = new FormData();
    for (const segment of segments) {
      if (segment.file) form.append("image", segment.file);
      else form.append("text", segment.text);
    }
    await request(paneUrl(id, "compose"), { method: "POST", body: form }, 45000);
    value.replace([]);
    value.pendingEnter = false;
    if (active === id) text($("draft-status"), "Sent");
    startState();
    return true;
  } catch { notice("Delivery could not be confirmed. Draft kept; check the terminal before retrying."); return false; }
  finally { sending = false; render(); }
}
$("reply-form").onsubmit = (event) => {
  event.preventDefault();
  if (!sending && !$("send").disabled) compose(active, draft());
};
// Enter SENDS, Shift+Enter inserts a newline — the standard chat-composer contract.
// It shipped requiring Cmd/Ctrl+Enter, a shortcut
// a phone keyboard cannot type at all, so the most obvious way to send did nothing and
// silently added a blank line instead. Cmd/Ctrl+Enter still sends, for a hardware
// keyboard and for anyone whose fingers already learned it. isComposing guards IME
// input: mid-composition Enter commits the candidate word and must not send.
// The handler is delegated from the FORM, not bound to the editor, because render()
// swaps in a per-pane editor element — a listener on #reply would die on the first pane
// switch. Delegation means keydown from the form's other controls lands here too, so it
// only acts on the editor: without that guard, a keyboard user who tabs to the Keys or
// attach button and presses Enter gets their draft SENT (preventDefault eats the button
// activation) instead of the key row or the file picker.
enterSubmits($("reply-form"), (target) => $("reply").contains(target));
bindAttach($("attach"), $("image-file"), () => active && !sending ? draft() : null);

for (const [id, name] of Object.entries({ collapse: "panel", "dash-nav": "dashboard", back: "back", theme: "sun", docs: "book", "close-pane": "x", "pane-menu-button": "ellipsis", "more-button": "ellipsis", sort: "arrowUpDown", "new-window": "plus", "search-icon": "search", "clear-search": "x", send: "up", attach: "paperclip", keyboard: "keyboard", "close-launch": "x", "zoom-in": "plus", "zoom-out": "minus", tail: "down" })) icon(id, name);
for (const [id, label, glyph] of [["all", "All", "layers"], ["running", "Running", "terminal"], ["recent", "Recent", "clock"], ["attention", "Needs you", "alert"]]) {
  html($(`${id}-tab`), `<span class="nav-icon">${licon(glyph)}<span id="${id}-count" class="count">0</span></span><span>${label}</span>`);
}
// The keys worth a thumb on a phone.
//
// Ctrl-B is a LITERAL C-b, distinct from Prefix (which sends whatever tmux reports as its
// prefix — C-a on some hosts). Nested tmux and Claude Code's "ctrl+b to run in background"
// need the real byte. It starts hidden and the state poll
// reveals it unless the prefix IS C-b, where it would just duplicate Prefix.
//
// Ctrl-D and Ctrl-O were missing here at first: this list was written fresh rather than
// ported, so the two keys you need when a pane has dropped to a bare shell — EOF to
// close it, and Claude Code's newline — were unreachable from a phone.
//
// S-Left is the mobile-only one that matters most: codex parks follow-up questions behind
// "shift + ← to answer", which a hardware keyboard simply types. Here it is the
// difference between a question being answerable and not.
//
// The row is overflow-x:auto with flex:none buttons, so it scrolls rather than shrinking
// them below a thumb-sized target (see #keys in style.css).
//
// PgUp/PgDn scroll agent TUIs and pagers a screen at a time; they use tmux's canonical
// names (PPage/NPage, what list-keys prints) and sit beside the arrows they extend.
//
// Ctrl-X then Ctrl-S is Claude Code's "send now" chord for flushing queued messages.
//
// Fourth slot is the SPOKEN name, defaulting to the visible label. Only a button labelled
// with a glyph or an abbreviation needs one: a screen reader handed "⇧←" announces two arrow characters,
// or nothing at all — useless for the very key you opened the row to press. The icon
// buttons (Up/Down) already carry words, so they need nothing extra.
for (const [label, key, name, aria = label] of [["Esc", "Escape"], ["Tab", "Tab"], ["Up", "Up", "up"], ["Down", "Down", "down"], ["PgUp", "PPage", null, "Page Up"], ["PgDn", "NPage", null, "Page Down"], ["\u21e7\u2190", "S-Left", null, "Shift+Left"], ["Enter", "Enter"], ["Ctrl-C", "C-c"], ["Ctrl-D", "C-d"], ["Ctrl-O", "C-o"], ["Ctrl-X", "C-x"], ["Ctrl-S", "C-s"], ["Ctrl-B", "C-b"], ["Prefix", "prefix"]]) {
  const button = document.createElement("button"); button.title = aria; button.setAttribute("aria-label", aria);
  if (name) html(button, licon(name, 18)); else text(button, label);
  if (key === "C-b") { button.id = "ctrl-b"; button.hidden = true; }
  button.onclick = () => sendKeys({ keys: key === "prefix" ? prefix : key, enter: false, literal: false });
  $("keys").append(button);
}
// Fade the right edge only while the key row actually has more to scroll to. A mask
// gradient does the drawing (see #keys); this just measures. It must react to scroll,
// to resize/rotation, and to the row being shown or its buttons changing, so a
// ResizeObserver on the row covers the last two without a layout-thrashing poll.
const KEYS_FADE = 24;
function fadeKeys() {
  const row = $("keys");
  const room = row.scrollWidth - row.clientWidth - Math.ceil(row.scrollLeft);
  row.style.setProperty("--keys-fade", `${room > 1 ? KEYS_FADE : 0}px`);
}
$("keys").addEventListener("scroll", fadeKeys, { passive: true });
new ResizeObserver(fadeKeys).observe($("keys"));
$("keyboard").onclick = () => { const open = $("keys").hidden; show("keys", open); $("keyboard").setAttribute("aria-expanded", open); if (open) fadeKeys(); };
html($("dashboard-tab"), `<span class="nav-icon">${licon("layers")}</span><span>Dashboard</span>`);
$("dashboard-tab").onclick = $("dash-nav").onclick = () => openDashboard();
// Close only leaves the pane, exactly like Back (which stands in for it on a narrow screen).
$("back").onclick = $("close-pane").onclick = () => navigate();
$("search").oninput = renderList;
$("clear-search").onclick = () => { $("search").value = ""; renderList(); $("search").focus(); };
$("sort").onclick = () => { sort = sort === "updated" ? "session" : "updated"; stayPut(); };
$("list-nav").querySelectorAll("button[data-filter]").forEach((button) => { button.onclick = () => { filter = button.dataset.filter; stayPut(); }; });
function applyTheme(light) {
  document.documentElement.classList.toggle("light", light);
  document.querySelector('meta[name="theme-color"]').content = light ? "#f5f7f6" : "#101312";
  document.querySelector('meta[name="apple-mobile-web-app-status-bar-style"]').content = light ? "default" : "black";
  icon("theme", light ? "moon" : "sun");
  $("theme").title = $("theme").ariaLabel = light ? "Use dark theme" : "Use light theme";
}
applyTheme(document.documentElement.classList.contains("light"));
$("theme").onclick = () => { const light = !document.documentElement.classList.contains("light"); applyTheme(light); try { localStorage.setItem("tmuxrc-theme", light ? "light" : "dark"); } catch {} };
function zoom(delta) { fontSize = Math.max(9, Math.min(22, fontSize + delta)); $("capture").style.fontSize = `${fontSize}px`; text($("font-size"), fontSize); $("zoom-out").disabled = fontSize === 9; $("zoom-in").disabled = fontSize === 22; }
$("zoom-in").onclick = () => zoom(1); $("zoom-out").onclick = () => zoom(-1);
$("tail").onclick = () => { $("terminal-scroll").scrollTop = $("terminal-scroll").scrollHeight; wheelHome(active); };
// Overscroll past the top of the live view scrolls the pane's own app (overscroll.js). Only
// tools whose fullscreen mode takes the wheel: Codex, omp and shells write their history
// into tmux, which the view already shows, and the daemon refuses anything else that has
// not asked for mouse reports (an inline Claude Code), after which this pane stops asking.
// Touch takes the same path, since a fullscreen agent's phone view is one screen and nothing
// else reaches older output.
const WHEEL_TOOLS = new Set(["claude", "opencode", "gemini"]);
function paintWheelCue() {
  const pull = wheel.net ? 0 : wheel.pull / RESIST_PX;
  $("capture").style.transform = pull ? `translateY(${Math.round(pull * 24)}px)` : "";
  text($("scroll-cue"), wheel.net ? "In the app's history" : "Keep scrolling for the app's history");
  show("scroll-cue", !!(wheel.net || pull));
}
// Every wheel request for every pane goes through one queue, in order, so a return home
// can never overtake the notches that scrolled the app up.
// A failed request must not stall the queue behind it. Retries stay in their place in the
// queue, so a later gesture can never overtake them.
function wheelPost(id, lines, retries = 0) {
  const attempt = (left) => post(paneUrl(id, "wheel"), { lines }).catch((error) => {
    if (!left) throw error;
    return attempt(left - 1);
  });
  const request = wheelLine.then(() => attempt(retries));
  wheelLine = request.catch(() => {});
  return request;
}
async function flushWheel() {
  if (wheelInFlight || !wheelQueued || !active) return;
  // `gesture` is this pane visit's state: wheelHome replaces it, so a late answer to an
  // older visit can never touch the current one.
  const id = active, gesture = wheel, lines = Math.max(-30, Math.min(30, wheelQueued));
  wheelQueued -= lines; wheelInFlight = lines;
  try {
    const { sent } = await wheelPost(id, lines);
    if (!sent && gesture === wheel) { wheel = { ...overscrollState(), off: true }; wheelQueued = 0; paintWheelCue(); }
  } catch {
    // A failed request may still have been delivered. Err toward the app being further up,
    // since a return home that overshoots the bottom does nothing: an upward batch counts
    // as sent, a downward one as not, and the next gesture tries it again.
    if (gesture === wheel && lines < 0) { wheel.net -= lines; paintWheelCue(); }
  } finally { wheelInFlight = 0; flushWheel(); }
}
// Bring the app back to its bottom when you leave it scrolled up, then forget this pane's
// gesture. It sends every notch that went up (`up`, never reduced by scrolling down) plus
// two spares, since the downs may not have undone the ups (overscroll.js) and extra notches
// below the bottom do nothing, so a failed batch is simply sent again (twice at most).
// Sent as 30-notch requests, the daemon's limit.
function wheelHome(id) {
  for (let left = id && wheel.up ? wheel.up + 2 : 0; left > 0; left -= 30)
    wheelPost(id, -Math.min(30, left), 2).catch(() => {});
  wheel = overscrollState(); wheelQueued = 0; paintWheelCue();
}
function overscrollPane(dy) {
  const box = $("terminal-scroll"), pane = panes.find((p) => p.pane_id === active);
  if (wheel.off || !dy || !WHEEL_TOOLS.has(pane?.tool)) return;
  if (dy < 0 ? box.scrollTop > 0 : box.scrollTop + box.clientHeight < box.scrollHeight - 1) return;
  wheelQueued += overscroll(wheel, dy, performance.now());
  paintWheelCue(); flushWheel();
  clearTimeout(wheelSpring);
  wheelSpring = setTimeout(() => { if (!wheel.net) wheel.pull = 0; paintWheelCue(); }, IDLE_MS);
}
// Pinch-zoom arrives as a ctrl+wheel. deltaMode counts in px, ~16px lines (Firefox) or pages.
$("terminal-scroll").addEventListener("wheel", (e) => { if (!e.ctrlKey) overscrollPane(e.deltaY * [1, 16, e.currentTarget.clientHeight][e.deltaMode]); }, { passive: true });
$("terminal-scroll").addEventListener("touchstart", (e) => { touchY = e.touches.length === 1 ? e.touches[0].clientY : null; }, { passive: true });
// A second finger drops the baseline, so the move after a pinch starts a new one.
$("terminal-scroll").addEventListener("touchmove", (e) => {
  if (e.touches.length !== 1) { touchY = null; return; }
  const y = e.touches[0].clientY;
  if (touchY !== null) overscrollPane(touchY - y);
  touchY = y;
}, { passive: true });
// Click mode: a tap on the terminal is a mouse click in the pane (the daemon drops it
// unless the pane's app asked for mouse reports). Select mode is the plain text view, for
// copying. Two explicit modes rather than guessing intent from drag-vs-tap, because a tap
// that meant "place the selection" would otherwise click whatever is under it.
function setClickMode(on) {
  $("capture").classList.toggle("clicks", on);
  $("click-mode").setAttribute("aria-pressed", String(on));
  html($("click-mode"), `${licon(on ? "pointer" : "cursor", 16)}${on ? "Click" : "Select"}`);
  $("click-mode").ariaLabel = $("click-mode").dataset.tip = on
    ? "Click mode: taps click inside the app, like menus and agent rows. Tap to switch to Select for copying text."
    : "Select mode: drag to select and copy text. Tap to switch to Click to use the app's menus and rows.";
}
let storedClickMode = null;
try { storedClickMode = localStorage.getItem("tmuxrc-click-mode"); } catch {}
setClickMode(storedClickMode !== "off");
$("click-mode").onclick = () => { const on = !$("capture").classList.contains("clicks"); setClickMode(on); try { localStorage.setItem("tmuxrc-click-mode", on ? "on" : "off"); } catch {} };
// The cell comes from monospace geometry, not the tapped node, so blank space right of
// the text still hits its row. Rows count up from the frame's last line — the edge it
// shares with the screen (see tmux.click). A tap on a link still follows the link.
$("capture").onclick = (event) => {
  const pre = $("capture");
  if (!active || !paintedFrame || captureDirty || !captureLines.length || !pre.classList.contains("clicks") || event.target.closest("a")) return;
  const probe = pre.appendChild(document.createElement("span"));
  probe.textContent = "0".repeat(100);
  const cell = probe.getBoundingClientRect().width / 100;
  probe.remove();
  const style = getComputedStyle(pre), box = pre.getBoundingClientRect();
  const top = box.top + parseFloat(style.paddingTop);
  const row = Math.floor((event.clientY - top) / ((box.bottom - parseFloat(style.paddingBottom) - top) / captureLines.length));
  const col = Math.floor((event.clientX - box.left - parseFloat(style.paddingLeft)) / cell) + 1;
  if (row < 0 || row >= captureLines.length || col < 1) return;
  post(paneUrl(active, "click"), { from_bottom: captureLines.length - 1 - row, col, frame: paintedFrame }).catch(() => {});
};

// One dialog for both: the session <select> ends in "New session…" (value ""), which swaps
// in directory + name fields and posts /api/sessions instead of /api/windows.
const launchMode = () => {
  const fresh = !$("launch-session").value;
  show("launch-new", fresh); text($("launch-title"), fresh ? "New session" : "New window");
};
$("launch-session").onchange = launchMode;
// The name defaults to the directory's basename, shown as the placeholder so typing a
// name of your own simply overrides it.
const sessionName = (dir) => (dir.replace(/\/+$/, "").split("/").pop() || "").replace(/^~$/, "home").replace(/[^\w-]+/g, "-").replace(/^-+|-+$/g, "") || "main";
$("launch-dir").oninput = () => { $("launch-name").placeholder = sessionName($("launch-dir").value.trim()); };
document.querySelectorAll(".start-session").forEach((b) => { b.onclick = () => openLaunch(true); });
$("new-window").onclick = () => openLaunch(false);
const LAST_DIR = "tmuxrc-last-dir";
async function openLaunch(fresh) {
  $("launch-dialog").showModal(); text($("launch-error"), "Loading agents..."); $("launch-choices").replaceChildren();
  const sessions = [...new Set(panes.map((p) => p.session).filter(Boolean))];
  $("launch-session").replaceChildren(...sessions.map((s) => new Option(s, s)), new Option("New session…", ""));
  if (fresh || !sessions.length) $("launch-session").value = "";
  $("launch-name").value = ""; // the placeholder (the directory's name) is the default
  try { $("launch-dir").value = localStorage.getItem(LAST_DIR) || "~"; } catch { $("launch-dir").value = "~"; }
  $("launch-dir").oninput(); launchMode();
  // Suggestions only (open panes' and past agent sessions' directories); any path works.
  request("/api/sessions/dirs").then((d) => $("launch-dirs").replaceChildren(...d.dirs.map((dir) => new Option(dir))), () => {});
  try {
    const data = await request("/api/launchers");
    text($("launch-error"), "");
    $("launch-choices").replaceChildren(...data.launchers.map((launcher) => {
      const button = document.createElement("button");
      const logo = document.createElement("img");
      logo.src = Object.prototype.hasOwnProperty.call(LOGOS, launcher.icon) ? LOGOS[launcher.icon] : launcher.icon || "/tmux-logomark.svg"; logo.alt = "";
      const label = document.createElement("span");
      const name = document.createElement("strong"); name.textContent = launcher.label; label.append(name);
      // A launcher whose command the daemon can't find stays VISIBLE — the user
      // configured it, so hiding it would only be a second mystery — but is disabled and
      // states the reason, instead of opening a window that dies in milliseconds.
      // The marker outlives the disabled flag, which launchWindow's `finally` clears on
      // every button: without it one failed launch would re-arm the entries the daemon
      // has just told us cannot run.
      if (launcher.unavailable) { button.dataset.unavailable = launcher.unavailable; const why = document.createElement("small"); why.textContent = launcher.unavailable; label.append(why); }
      button.append(logo, label);
      if (!launcher.unavailable) button.insertAdjacentHTML("beforeend", licon("plus"));
      button.disabled = !!launcher.unavailable;
      button.onclick = () => launchWindow(launcher.label, button);
      return button;
    }));
  } catch { text($("launch-error"), "Could not load launchers. Close and try again."); }
}
$("close-launch").onclick = () => $("launch-dialog").close();
let launching = false, launched = null;
async function launchWindow(launcher, button) {
  const session = $("launch-session").value, name = $("launch-name").value.trim() || $("launch-name").placeholder;
  if (launching) return;
  if (!session && !/^[\w-]{1,64}$/.test(name)) return text($("launch-error"), "Session names are letters, digits, - and _ only.");
  launching = true; text($("launch-error"), session ? "Creating window..." : "Starting session...");
  $("launch-choices").querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    const data = await (session ? post("/api/windows", { session, launcher })
      : post("/api/sessions", { name, cwd: $("launch-dir").value.trim() || "~", launcher }));
    if (!session) try { localStorage.setItem(LAST_DIR, $("launch-dir").value.trim()); } catch {}
    // Record the id BEFORE navigating to it: startState only *starts* a fetch, so the
    // hashchange this triggers reaches render() while `panes` is still the previous
    // poll's, without the pane that was created a moment ago. See awaitingLaunch.
    launched = { id: data.pane_id, at: Date.now() };
    // The exemption expires on a clock, but only a render can act on it, and renders are
    // driven by /api/state — which may be parked on a 25s long poll. One scheduled render
    // at the deadline is what makes LAUNCH_GRACE_MS mean anything at all. No cancellation: an
    // extra render is idempotent, and both the pane-appeared and user-moved-on cases are
    // already handled (by the pane being found, and by leaveMissingPane's stillOnPane).
    setTimeout(render, LAUNCH_GRACE_MS);
    $("launch-dialog").close(); startState(); navigate(data.pane_id);
  } catch (error) {
    text($("launch-error"), error.detail || "Creation could not be confirmed. Check sessions before retrying.");
    // The list was a snapshot from the GET; if the daemon has since decided it can't run
    // this one, believe it now rather than leaving a button that only ever re-shows the
    // same refusal. The marker is what `finally` restores from, so setting it is enough.
    // ONLY on 400, the preflight's own status: every FastAPI error carries a `detail`, so
    // a stale session (404) or any other transient refusal would otherwise disable a
    // perfectly good launcher for the rest of the dialog over something that isn't
    // about the command at all.
    if (button && error.status === 400 && error.detail) button.dataset.unavailable = error.detail;
  }
  finally { launching = false; $("launch-choices").querySelectorAll("button").forEach((button) => { button.disabled = "unavailable" in button.dataset; }); }
}

function fitViewport() {
  // iOS resizes the visual viewport, not the layout viewport, when its keyboard opens.
  const viewport = window.visualViewport;
  if (!viewport || viewport.scale !== 1) return;
  const standalone = navigator.standalone || matchMedia("(display-mode: standalone)").matches;
  const focused = document.activeElement;
  const textInput = focused?.tagName === "INPUT"
    && ["text", "search", "email", "url", "tel", "number", "password"].includes(focused.type);
  const editing = focused?.isContentEditable
    || ((textInput || focused?.tagName === "TEXTAREA") && !focused.readOnly && !focused.disabled);
  // Installed mode lets iOS reserve the status bar outside the app. Fill that
  // available viewport while browsing; editors still follow the keyboard.
  document.documentElement.classList.toggle("standalone-fill", !!standalone && !editing);
  // Translucent installs expose a top safe area excluded from visualViewport;
  // opaque-status-bar installs report zero. Preserve both without sniffing the installer.
  const topInset = standalone && !editing ? parseFloat(getComputedStyle($("app")).paddingTop) || 0 : 0;
  document.documentElement.style.setProperty("--app-height", `${viewport.height + topInset}px`);
  document.documentElement.style.setProperty("--app-top", `${viewport.offsetTop}px`);
}
window.visualViewport?.addEventListener("resize", fitViewport);
window.visualViewport?.addEventListener("scroll", fitViewport);
window.addEventListener("resize", fitViewport);
document.addEventListener("focusin", fitViewport);
document.addEventListener("focusout", () => requestAnimationFrame(fitViewport));
// Crossing the breakpoint changes which elements are hidden, and only render() knows
// that. Without this, widening the window leaves the list hidden until the next poll
// repaints — and narrowing it leaves a sidebar with no room, which is the worse half.
// Older iOS Safari has only the deprecated addListener, and calling the modern name
// unguarded would throw here and abort the whole module — breaking the phone UI to add
// a wide-screen affordance those browsers can never show.
// The wide sidebar takes the header's controls into its slots, in this order; a phone
// gets them back where index.html put them (a comment marks each home), so every control
// is one element with one set of listeners, whichever layout it is in.
const CHROME = { "sb-head": ["brand", "collapse"], "sb-nav": ["new-window", "dash-nav", "chat", "live-mode", "push"], "sb-foot": ["connection", "theme", "docs"] };
const homes = {};
function placeChrome() {
  for (const [slot, ids] of Object.entries(CHROME)) for (const id of ids) {
    if (!homes[id]) $(id).before(homes[id] = document.createComment(id));
    if (WIDE.matches) $(slot).append($(id)); else homes[id].after($(id));
  }
}
placeChrome();
// route() again, not just render(): a pane URL without a view opens on a different tab once wide.
const resizeWorkspace = () => { placeChrome(); route(); };
if (WIDE.addEventListener) WIDE.addEventListener("change", resizeWorkspace);
else if (WIDE.addListener) WIDE.addListener(resizeWorkspace);
// Kill the pane's whole tmux window. Buried in the overflow menu, not on the X: an X reads
// as "close this view", and pressing it should never end a process. The poll drops the pane
// and leaveMissingPane does the rest; 404 means it is already gone, the outcome asked for.
dismissable($("pane-menu"));
dismissable($("more-menu"));
html($("kill-pane"), `${licon("trash", 18)}<span>Kill window</span>`);
$("kill-pane").onclick = async () => {
  $("pane-menu").open = false; $("pane-menu-button").focus(); // the item just hid: keep keyboard focus on a visible control
  if (!active || !confirm("Kill this tmux window? Whatever is running in it will end.")) return;
  try { await post(paneUrl(active, "close")); } catch (error) { if (error.status !== 404) notice("Could not kill this window."); }
};
window.addEventListener("hashchange", route);
// Only catch up a frame that was held for a selection; composer keystrokes also fire this.
document.addEventListener("selectionchange", () => { if (terminalVisible() && captureDirty) paintCapture(); });
document.addEventListener("visibilitychange", () => { sendPresence(); startState(); restartDetail(); });
window.addEventListener("online", () => { startState(); restartDetail(); });
window.addEventListener("pageshow", () => { startState(); restartDetail(); fitViewport(); });
window.addEventListener("pagehide", () => { stateController?.abort(); detailController?.abort(); });
fitViewport(); route(); startState();
setupPush($("push"), notice, licon("bell"));
const live = setupLiveMode({ request, session: liveSession, licon, wide: WIDE, report: reportError, onVersion: observeVersion });
let assetVersion = null;
function hasDrafts() {
  return [...drafts.values(), ...renderSidebar.drafts.values()].some((value) => value.pendingEnter || value.files.size || value.editor.textContent.length);
}
function observeVersion(version) {
  if (typeof version !== "string" || !version) return;
  if (assetVersion === null) assetVersion = version;
  const changed = version !== assetVersion;
  show("update-notice", changed);
  $("reload-update").disabled = sending || launching;
  // A deploy must not eat another pane's draft, an in-flight action, or a voice session.
  if (changed && !document.hidden && !sending && !launching && !hasDrafts() && !live.isActive() && !document.querySelector("dialog[open]")) location.reload();
}
$("reload-update").onclick = () => {
  if (sending || launching) return;
  if ((hasDrafts() || live.isActive()) && !confirm("Reload now? Unsent drafts will be discarded and Live Mode will end.")) return;
  location.reload();
};
setInterval(() => { if (!document.hidden) live.refresh(); }, VERSION_POLL_MS);
