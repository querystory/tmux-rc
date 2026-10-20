import { headerPicker, dismissable } from "/m/header-picker.js";
import { renderAtlas, renderFleet, refreshAtlasHistory, renderHeatView, detailCard, pageHead } from '/m/atlas.js';
import { renderCaptureLines, linkifyText, besideHeld, keptRows } from "/terminal.js";
import { setupLiveMode } from "/m/live.js";
import { Composer, bindAttach, enterSubmits } from "/m/composer.js";
import { answerBody, pickCursorRow, STALE, UNTAKEN } from "/cursor-pick.js";
import { sendPresence, setupPush, stateUrl } from "/push.js";
import { paneLinks, askLink } from "/pr-links.js";
import { needsYou, activityLabel, activityClass, markWorking, isRecent, matchesFilter, matchesSearch, newestFirst, stillOnPane, paneName, paneActivity, paneHeadline, panePending, markLine, paneMeta, questionItems, records, itemDone, awaitingLaunch, LAUNCH_GRACE_MS, age, since, esc, markEnding, noteSessions, oneSession, sessionOf } from "/m/pane-model.js";
import { parseHash, formatHash, historyMode, PAGES, scratchUrl } from "/m/url-state.js";
import { overscroll, overscrollState, RESIST_PX, IDLE_MS } from "/m/overscroll.js";
import { dragToDismiss } from "/m/drag-dismiss.js";
import { tmuxKey, inputQueue } from "/m/keys.js";
import { setupSidebar, SHOW, SORTS } from "/m/sidebar.js";
import { renderUsage, renderStrip, byUrgency, shownUsage, moreUsage, staleUsage } from "/m/usage.js";
import { renderUsageView, usageDays } from "/m/usage-view.js";
import { setupAccounts } from "/m/accounts.js";
import { renderLoops, renderLoopsCard } from "/m/loops.js";
import { setupRestore } from "/m/restore.js";

const refreshViewPicker = headerPicker(document.getElementById("review-layout"));

// Ordinary API calls: long enough for a slow tmux host, short enough that a dead link
// surfaces as an error before the user retries by hand.
const REQUEST_TIMEOUT_MS = 8000;
// Long polls (/api/state, /live) must outlast the server's 25s hold, or every idle poll
// would abort just before the server answers.
const LONG_POLL_TIMEOUT_MS = 35000;
// No client timeout (setTimeout's largest delay), for a request whose server run an abort
// would not stop: the answer must be its own outcome, never a failure while it runs on.
const NO_TIMEOUT = 2 ** 31 - 1;
// How long a delivered answer holds its card (spinner, options disabled) when the pane's
// question never changes — an ignored key — before we trust the pane's own state again.
const ANSWER_PENDING_MS = 15000;
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
  history: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
  x: '<path d="m18 6-12 12M6 6l12 12"/>',
  ellipsis: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
  menu: '<path d="M4 5h16M4 12h16M4 19h16"/>',
  power: '<path d="M12 2v10M18.4 6.6a9 9 0 1 1-12.77.04"/>',
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
  panelRightClose: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M15 3v18M8 9l3 3-3 3"/>',
  panelRightOpen: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M15 3v18M10 15l-3-3 3-3"/>',
  dashboard: '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
  rows: '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/><path d="M14 4h7M14 9h7M14 15h7M14 20h7"/>',
  unfold: '<path d="m7 15 5 5 5-5M7 9l5-5 5 5"/>',
  fold: '<path d="m7 20 5-5 5 5M7 4l5 5 5-5"/>',
  gauge: '<path d="m12 14 4-4M3.34 19a10 10 0 1 1 17.32 0"/>',
  images: '<path d="M18 22H4a2 2 0 0 1-2-2V6"/><path d="m22 13-1.296-1.296a2.41 2.41 0 0 0-3.408 0L11 18"/><circle cx="12" cy="8" r="2"/><rect width="16" height="16" x="6" y="2" rx="2"/>',
  arrowUpDown: '<path d="m21 16-4 4-4-4M17 20V4M3 8l4-4 4 4M7 4v16"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  filter: '<path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/>',
  eye: '<path d="M2.06 12.35a1 1 0 0 1 0-.7 10.75 10.75 0 0 1 19.88 0 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-19.88 0"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M10.73 5.08a10.74 10.74 0 0 1 11.2 6.57 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-1.44 2.49M14.08 14.16a3 3 0 0 1-4.24-4.24M17.48 17.5a10.75 10.75 0 0 1-15.42-5.15 1 1 0 0 1 0-.7 10.75 10.75 0 0 1 4.45-5.14M2 2l20 20"/>',
  settings: '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
  bot: '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2M20 14h2M15 13v2M9 13v2"/>',
};
const licon = (name, size = 20) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${LUCIDE[name]}</svg>`;
const $ = (id) => document.getElementById(id);
const text = (node, value = "") => { if (node.textContent !== String(value)) node.textContent = value; };
const html = (node, value) => { if (node._html !== value) { node.innerHTML = value; node._html = value; } };
const show = (id, visible) => { $(id).hidden = !visible; };
const icon = (id, name) => html($(id), licon(name));
const paneUrl = (id, path) => `/api/panes/${encodeURIComponent(id)}/${path}`;
const LOGOS = { claude: "/claude.png", codex: "/openai.svg", gemini: "/gemini.svg", antigravity: "/antigravity.svg", opencode: "/opencode.svg", omp: "/omp.svg", shell: "/bash.png" };
const NO_TMUX = "tmux is not running on this host (it does not survive a reboot).";
// What an empty list says was filtered: "No Personal panes are working."
const STATE_VERB = { attention: "need you", working: "are working", finished: "just finished", idle: "are idle" };
// The desktop workspace shows context alongside the live terminal; phones retain tabs.
// The Layout menu's wireframes, in a 38×24 box: a terminal panel is dark, with a prompt and
// output lines; an overview panel is tinted, with a title bar and lines of text.
const strokes = (x, y, room, widths) => widths.filter((_, i) => 3 * i + 2 < room).map((w, i) => `M${x} ${y + 3 * i}h${w}`).join("");
const box = (x, y, w, h) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="1.5"/>`;
const term = (x, y, w, h) => `<g class="term">${box(x, y, w, h)}<path class="prompt" d="M${x + 2.5} ${y + 2.5}l2 1.5-2 1.5M${x + 5.5} ${y + 5.5}h2.5"/><path d="${strokes(x + 2.5, y + 9, h - 9, [Math.min(w - 5, 12), Math.min(w - 5, 7)])}"/></g>`;
const over = (x, y, w, h) => `<g class="over">${box(x, y, w, h)}${box(x, y, w, 3)}<path d="${strokes(x + 2.5, y + 6, h - 6, [w - 5, w - 9, w - 7])}"/></g>`;
const svgThumb = (body) => `<svg viewBox="0 0 38 24">${body}</svg>`;
const LAYOUT_THUMBS = { auto: "✦", side: svgThumb(term(0, 0, 23, 24) + over(25, 0, 13, 24)),
  stack: svgThumb(over(0, 0, 38, 9) + term(0, 11, 38, 13)), summary: svgThumb(over(0, 0, 38, 24)), terminal: svgThumb(term(0, 0, 38, 24)) };
const WIDE = matchMedia("(min-width: 1100px)");
// The wide Layout picker's choice, global across panes and reloads: a split, or the one tab
// the focus layout shows. "focus" is what the Overview choice was saved as before the tab was.
let reviewLayout = "auto";
try { const saved = localStorage.getItem("tmuxrc-review-layout"); if (["auto", "side", "stack", "summary", "terminal"].includes(saved)) reviewLayout = saved; else if (saved === "focus") reviewLayout = "summary"; } catch {}
// Folding the side-by-side overview to a strip is a view of that layout, not a layout of its
// own: the picker and Auto still resolve to "side", so nothing restarts the streams (the
// terminal is captured at tmux's size, not ours) and the overview stays current underneath.
let overviewFolded = false;
try { overviewFolded = localStorage.getItem("tmuxrc-overview-folded") === "on"; } catch {}
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
// page: a dashboard card's own view (url-state.js PAGES), which replaces the page; leaving
// it lands on its card (CARDS).
let dashboard = false, page = null;
const CARDS = { heat: "heat-card", usage: "usage-panel", loops: "loops-card" };
const dashboardVisible = () => !active && (WIDE.matches || dashboard);
let panes = [], statePanes = [], paneIds = null, accountsAt = 0, active = null, view = "summary", filter = [], loaded = false, booted = false, tmuxRunning = true;
let focusPushComposer = false;
// The sort is a per-browser preference, as the sidebar's grouping is: how you like to read the
// list, not what a link should show. A phone's Settings and the wide filter menu share it.
let sort = "updated";
try { if (localStorage.getItem("tmuxrc-sort") === "session") sort = "session"; } catch {}
// Whether panes that need you show outside the filters (renderList): per browser, on by default.
let pinNeeds = true, outside = new Set();
// The agent filter (the URL's agent=) and the dashboard's tmux session: one scope that the
// list, its filter menu and the dashboard's chips and charts (atlas.js) all read and set.
const scope = $("session-atlas")._scope = { tools: [], session: "", pick: (tools) => { scope.tools = tools; stayPut(); }, pickSession: (session) => { scope.session = session; stayPut(); } };
const ofAgent = (p) => !scope.tools.length || scope.tools.includes(p.tool || "other");
try { pinNeeds = localStorage.getItem("tmuxrc-pin-needs") !== "off"; } catch {}
const setPinNeeds = (v) => { pinNeeds = v === "on"; try { localStorage.setItem("tmuxrc-pin-needs", v); } catch {} renderList(); paintSettings(); };
const setSort = (v) => { sort = v; try { localStorage.setItem("tmuxrc-sort", v); } catch {} renderList(); paintSettings(); };
let sending = false, prefix = "C-b", stateController, detailController, detailId = null, fitCols = 0, fitAlt; // see fitTo; fitAlt: is the streamed app full screen
let streamedLayout = null;
let eventsKey = null, latestCapture = "", fontSize = 13;
// Answers tapped and not yet reflected, per pane (pane id -> { key, option, index, note }): the
// sidebar can answer several panes inside one hold, and each keeps its own.
const pendingAnswers = new Map();
// Per-line nodes under #capture, in document order; each caches the markup last written
// to it (_html). Set when a frame was held back for a selection, so selectionchange
// knows there is something to catch up on.
let captureLines = [], captureDirty = false;
let latestFrame = "", paintedFrame = "", shown = { kept: [], count: 0 }; // the painted frame's rows (frameLines)
// Earlier history (docs/design/live-view.md): tmux rows above the live frame, fetched as you
// near the top and kept, since a history row never changes. Rows are counted from the
// oldest one tmux keeps, so new output (which only adds rows below) never renumbers a row
// already here. #earlier holds rows [top, end); while it is empty both follow the frame's
// own first row. `latestGeometry` is the newest frame's first row and `screen` (how its
// rows are numbered: the width and which screen is up; a change renumbers every row).
const EARLIER_LINES = 500;
let earlier = { top: 0, end: 0, screen: "" }, latestGeometry = [0, ""], shownTop = 0; // shownTop: the painted frame's first row
let wheel = overscrollState(), wheelQueued = 0, wheelInFlight = 0, wheelSpring = 0, touchY = null, wheelLine = Promise.resolve();
// CSPRNG-random or omitted, never guessed. getRandomValues, unlike randomUUID, needs no
// secure context, and a reconnecting chat finds its parked cards by this id.
const liveSession = (() => {
  try { return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join(""); }
  catch { return ""; }
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
      else if (typeof detail?.message === "string") Object.assign(error, detail, { detail: detail.message }); // a refusal with data (the move's older copy)
      throw error;
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
    external?.removeEventListener("abort", abort);
  }
}
const post = (url, body, timeout) => request(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, timeout);
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
// A notice stays until replaced or cleared; with `ms` it also clears itself, for a short
// result. `action`, a [label, fn] pair, adds one button (an Undo) that clears it and runs fn.
let noticeTimer;
// Restore is a view of its own, so the dashboard behind it stays whole. An offer that turns up
// on the bare app (no pane, no view chosen) opens it; a spent one leaves it.
const restore = setupRestore({ mount: $("restore"), request, slow: NO_TIMEOUT, logos: LOGOS, licon, notice: (...a) => notice(...a), running: () => tmuxRunning,
  open: () => openPage("restore"),
  opened: () => { if (!active && !page && !dashboard) navigate(null, "restore", { mode: "replace" }); },
  closed: () => { if (page === "restore") closePage(); } });
function notice(message = "", ms = 0, action = null) {
  clearTimeout(noticeTimer); text($("notice"), message); show("notice", !!message);
  if (action) $("notice").append(" ", Object.assign(document.createElement("button"), { type: "button", textContent: action[0], onclick: () => { notice(); action[1](); } }));
  if (message && ms) noticeTimer = setTimeout(() => notice(), ms);
}

const accounts = setupAccounts({ request, post, licon, notice, repaint: () => { paintUsage(); render(); paintSettings(); },
  // An account picked: this very screen, in a URL that names it (or none, for All).
  navigate: (acct) => navigate(active, active ? view : dashboard ? page || "dashboard" : "summary", { acct }),
  sessions: () => [...new Set(panes.map((p) => p.session).filter(Boolean))],
  paneAlive: (id) => panes.some((p) => p.pane_id === id), openPane: (id) => { $("settings").close(); openLaunched(id); }, // a phone's Settings sheet would cover the pane
  // A phone's list says its search and states on the filter button and at the list's foot: those chips are wide only.
  query: () => WIDE.matches ? $("search").value.trim() : "", clearQuery: () => $("clear-search").click(),
  // The wide list's state filters and, in both layouts, the agents, each a chip that clears it
  // (focus to the filter button), as [kind, key, label].
  chips: () => [...WIDE.matches ? filter.map((f) => ["State", f, SHOW[f]]) : [], ...scope.tools.map((t) => ["Agent", `agent:${t}`, t])],
  clearChip: (k) => { filter = filter.filter((f) => f !== k); scope.tools = scope.tools.filter((t) => `agent:${t}` !== k); stayPut(); renderSidebar.picker.firstChild.focus(); }, clearAll: clearFilters });

const writeHash = (next, how = "replace", state = history.state) => history[`${how}State`](state, "", next ? `#${next}` : location.pathname + location.search);
// Every user-driven move goes through here: the URL is written first, then the view is
// routed synchronously (pushState/replaceState fire no hashchange). Back/Forward and edits
// by hand reach route() through hashchange instead. Never call this from a poll.
function navigate(id = null, nextView = "summary", { mode, acct = accounts.key() } = {}) {
  const nextPage = PAGES.includes(nextView) ? nextView : null;
  const next = formatHash({ pane: id, view: nextView, dashboard: nextView === "dashboard" || !!nextPage, page: nextPage, filter, acct, agent: scope.tools, q: $("search").value });
  const current = location.hash.slice(1);
  if (next !== current) {
    // A replaced entry keeps its state (a card's view stays "from its card"); a new screen starts bare.
    const how = mode || historyMode(current, next);
    writeHash(next, how, how === "replace" ? history.state : null);
  }
  route();
}

// Filters change the list, not the screen: with the sidebar beside an open pane
// (wide layout) the pane, its tab and a dashboard view must survive them; on a phone a
// filter tap is a request for the list.
// Opened from its card, a view's entry says so in history.state, which survives a reload and
// Back/Forward; its Back then steps back rather than stacking a second dashboard entry.
// Not the view already open: that adds no entry, so its Back would leave the app.
const openPage = (name) => { if (page !== name) { navigate(null, name); history.replaceState({ fromCard: true }, ""); } };
const closePage = () => history.state?.fromCard ? history.back() : navigate(null, "dashboard", { mode: "replace" });
const stayPut = (opts) => navigate(active, active ? view : dashboard ? page || "dashboard" : "summary", opts);
// Clear all: every filter (the states, the accounts and the search), not the grouping or the
// sort, which are not filters; focus goes to the search box, else (a phone's dashboard or
// pane, where it is hidden) the tab bar's pressed tab or Back; in the rail, to the filter
// button, since focusing search would expand it.
function clearFilters() {
  filter = []; scope.tools = []; scope.session = ""; $("search").value = ""; renderSidebar.picker.open = false; stayPut({ acct: [] });
  ($("app").classList.contains("rail") ? [renderSidebar.picker.firstChild] : [$("search"), document.querySelector('#list-nav [aria-pressed="true"]'), $("back")])
    .find((el) => el?.getClientRects().length)?.focus();
}

// Leaving a pane the user did not choose to leave: it closed under them, or a deep link
// named one that is gone. Replace, never push: Back would land on the dead deep link and
// bounce straight out again. `id` is the pane the caller believes is on screen; stillOnPane
// rejects the call when the user has already moved on and only the queued hashchange is late
// (see pane-model.js). A pane this page is still closing is endPane's to leave, once its
// request answers: the live stream 404s on it as soon as the window dies.
function leaveMissingPane(id) {
  if (!stillOnPane(location.hash, id) || (ending.has(id) && !ending.get(id).done)) return;
  navigate(null, "summary", { mode: "replace" });
  notice("That pane is no longer available.");
}

function route() {
  const wasDashboardVisible = dashboardVisible(), wasPage = page;
  const state = parseHash(location.hash, defaultView());
  const changed = state.pane !== active;
  dropGesture();
  active = state.pane;
  focusPushComposer = state.compose;
  ({ dashboard, view, filter, page } = state);
  scope.tools = state.agent;
  if (state.q !== $("search").value.trim()) $("search").value = state.q; // a reload, a link or Back: typing writes the URL, not this
  if (accounts.follow(state.acct)) paintUsage(); // the strip and meters are filtered too
  if (page !== wasPage) { $("dash-page").replaceChildren(); $("dash-page")._signature = null; } // each view builds afresh
  if (viewer.open && page !== "scratch") viewer.close(); // Back, a push or Safari's swipe left Scratch: its viewer goes too
  if (changed) {
    if (active) $("reply").replaceWith(draft().editor);
    $("secret").value = ""; // a password is for the pane it was typed at
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
  // Focus follows: into the view's Back, then back to the card's Details.
  if (CARDS[wasPage] && !page && dashboardVisible()) { $(CARDS[wasPage]).scrollIntoView({ block: "center" }); $(CARDS[wasPage]).querySelector('[data-key^="details:"]')?.focus({ preventScroll: true }); }
  // Not into Restore: its first button is the Restore itself, which an Enter would press.
  if (page && !wasPage) { $("landing").scrollTop = 0; if (page !== "restore") $("dash-page").querySelector(".landing-head h2")?.focus({ preventScroll: true }); }
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
  button.innerHTML = `<span class="pane-icon"><img alt=""></span><span class="row-body"><span class="row-title"><strong></strong><span class="row-age"></span>${licon("chevron", 14)}</span><span class="row-status"></span><span class="row-meta"><span class="session-chip" hidden></span><span class="row-acct"></span><span class="row-details"></span><span class="badge"></span></span></span>`;
  button.onclick = () => navigate(pane.pane_id);
  return button;
}
function updateRow(button, pane) {
  if (pane.pane_id === active) button.setAttribute("aria-current", "true");
  else button.removeAttribute("aria-current");
  button.classList.toggle("needs-you", asks(pane));
  button.classList.toggle("fresh", pane.activity === "idle" && isRecent(pane));
  const logo = button.querySelector(".pane-icon img");
  logo.alt = pane.tool || "tmux";
  markWorking(logo, pane, LOGOS);
  text(button.querySelector("strong"), paneName(pane));
  text(button.querySelector(".row-age"), age(pane));
  const badge = button.querySelector(".badge");
  badge.className = `badge ${badgeOf(pane).cls}`;
  text(badge, badgeOf(pane).label);
  markLine(button.querySelector(".row-status"), paneActivity(pane), pane);
  const sessionChip = button.querySelector(".session-chip");
  const session = sessionOf(pane);
  sessionChip.hidden = sort !== "updated" || !session; // a session label already says it
  text(sessionChip, session);
  sessionChip.title = session ? `Session: ${session}` : "";
  html(button.querySelector(".row-acct"), accounts.badge(pane.pane_id));
  text(button.querySelector(".row-details"), [pane.tool, pane.model, pane.window_index !== "" && pane.window_index != null ? `Window ${pane.window_index}` : ""].filter(Boolean).join(" / "));
}
const renderSidebar = setupSidebar({ licon, reconcile, text, html, renderItems, logos: LOGOS, navigate, notice, badge: accounts.badge,
  active: () => active, wide: () => WIDE.matches, sending: () => sending, answers: answerOptions, answered: isAnswered, picked, asks, badgeOf, answer, compose, offers: offersOf, copy: copyText,
  setFilter: (value) => { filter = value; stayPut(); }, setAgents: scope.pick,
  setSession: scope.pickSession, clearAll: clearFilters, outside: (p) => outside.has(p.pane_id), sort: () => sort, setSort, repaint: () => renderList() });
function emptyMessage(query) {
  if (!loaded) return "Loading sessions...";
  if (!booted) return "Reading terminal sessions...";
  const accts = accounts.on(), q = $("search").value.trim(), verb = filter.map((f) => STATE_VERB[f]).join(" or ");
  if (!filter.length && !accts.length && !q) return tmuxRunning ? "No tmux panes are open." : NO_TMUX;
  const what = `${accts.length ? `${accts.join(" or ")} ` : ""}panes`;
  return q ? `No ${what}${verb ? ` that ${verb}` : ""} match "${q}".` : `No ${what}${verb ? ` ${verb}` : ""}.`;
}
function renderList() {
  show("clear-search", !!$("search").value);
  accounts.paintFilter(); // the pill names the search
  const query = $("search").value.trim().toLowerCase();
  const mine = panes.filter((p) => accounts.matches(p) && ofAgent(p)); // the account and agent filters, else every pane
  // Panes that need you show whatever the account or search filter says (marked as outside
  // it), unless the state filter leaves Needs you out or the setting is off.
  const pin = pinNeeds && (!filter.length || filter.includes("attention"));
  outside = new Set(pin ? panes.filter((p) => needsYou(p) && !(accounts.matches(p) && ofAgent(p) && matchesSearch(p, query))).map((p) => p.pane_id) : []);
  const subset = panes.filter((p) => outside.has(p.pane_id) || accounts.matches(p) && ofAgent(p) && matchesFilter(p, filter) && matchesSearch(p, query));
  // The menu's Agent and Session groups: the kinds of pane and the tmux sessions there are.
  const counts = {}; // not Object.groupBy: iOS 16.4–17.3 lack it
  for (const p of panes.filter(accounts.matches)) counts[p.tool || "other"] = (counts[p.tool || "other"] || 0) + 1;
  const tools = [...new Set([...Object.keys(counts), ...scope.tools])].map((t) => [t, counts[t] || 0]); // one picked stays, to be unpicked
  // Sessions as the dashboard offers them (its history's too, and the one picked: atlas.js), else the live ones.
  const more = { agents: tools, agent: scope.tools, sessions: scope.sessions || [...new Set(panes.map((p) => p.session).filter(Boolean))].sort(), session: scope.session };
  // One composer per pane, in either layout: an open pane takes over its sidebar Reply draft.
  const inline = renderSidebar.drafts.get(active);
  if (inline) { draft().append(inline); inline.editor.remove(); renderSidebar.drafts.delete(active); }
  renderSidebar.answers.prune(subset);
  if (WIDE.matches) { renderSidebar(subset, query, filter, more); placeRail(); } else { $("app").classList.remove("rail"); placePill(); renderSidebar.marks(filter, more); renderPhoneList(subset); } // a folded sidebar's class would hide the chips
  show("empty", !subset.length);
  text($("empty"), emptyMessage(query));
  // Empty, while panes the filter hides need you: say so, a click from them.
  const needing = subset.length ? [] : panes.filter(needsYou), hidden = needing.length, accts = accounts.on();
  // A phone's list narrowed by the filter menu says so at its foot too, a tap from all of them.
  const scopedOn = !hidden && !WIDE.matches && accts.length + filter.length + scope.tools.length > 0, scoped = subset.filter((p) => !outside.has(p.pane_id)).length; // not the pinned
  show("empty-needs", hidden > 0 || scopedOn);
  $("empty-needs").dataset.all = scopedOn ? "filters" : "needs";
  // "On other accounts" only when that is what hides every one of them.
  text($("empty-needs"), scopedOn ? `${scoped} ${[...scope.tools, ...accts].map((a) => `${a} `).join("or ")}pane${scoped === 1 ? "" : "s"} of ${panes.length} · Show all`
    : `${hidden} pane${hidden === 1 ? "" : "s"} ${needing.some(accounts.matches) || !accts.length ? "outside the filter" : "on other accounts"} need${hidden === 1 ? "s" : ""} you · Show`);
  text($("attention-count"), (pinNeeds ? panes : mine).filter(asks).length || "");
  // Sessions is the list under whatever the filter menu says; Needs you, the list of just those.
  const tab = String(filter) === "attention" ? "attention" : "all";
  $("list-nav").querySelectorAll("[data-filter]").forEach((button) => button.setAttribute("aria-pressed", !dashboard && tab === button.dataset.filter));
  // Restore rides the dashboard's view machinery but is its own screen: its row is the one pressed.
  $("dashboard-tab").setAttribute("aria-pressed", String(dashboard && page !== "restore"));
  $("dash-nav").setAttribute("aria-pressed", String(dashboardVisible() && !["restore", "scratch"].includes(page)));
  $("scratch-nav").setAttribute("aria-pressed", String(page === "scratch"));
  $("restore").setAttribute("aria-pressed", String(page === "restore"));
  // The filter button counts each filter on, and its tooltip names them.
  const on = [filter.map((f) => SHOW[f]).join(", "), scope.tools.join(", "), accounts.on().join(", "), scope.session, $("search").value.trim() && `"${$("search").value.trim()}"`].filter(Boolean);
  const n = filter.length + scope.tools.length + accounts.on().length + !!scope.session + !!query;
  // Named for screen readers too: in the rail the chips that say it are hidden.
  Object.assign(renderSidebar.picker.firstChild, { title: on.join(" · ") || "No filters", ariaLabel: on.length ? `Filter panes: ${on.join(" · ")}` : "Filter panes" }).dataset.n = n || "";
  renderSidebar.clearAll.disabled = !n;
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
  const rows = sort === "updated" ? subset.sort(newestFirst) : oneSession() ? subset
    // Sessions as the watcher lists them: tmux's own order.
    : [...new Set(subset.map((p) => p.session))].flatMap((session) => [{ heading: session || "Session", key: `session:${session}` }, ...subset.filter((p) => p.session === session)]);
  // A card for answers to tap: a question's, or a finished turn's offers.
  const carded = (p) => needsYou(p) || offersOf(p).length > 0;
  reconcile($("pane-list"), rows, (p) => p.heading ? p.key : carded(p) ? `ask:${p.pane_id}` : p.pane_id, (p) => {
    if (p.heading) { const node = document.createElement("h2"); node.className = "session-label"; return node; }
    if (!carded(p)) return makeRow(p);
    const card = document.createElement("div");
    card.className = "pane-card";
    card.append(makeRow(p));
    renderSidebar.answers.add(card);
    return card;
  }, (node, p) => {
    if (p.heading) return text(node, p.heading);
    node._p = p;
    markEnding(node, p);
    node.classList.toggle("outside", outside.has(p.pane_id));
    updateRow(node.querySelector(".pane-row") || node, p);
    if (node.matches(".pane-card")) renderSidebar.answers.update(node, p);
  });
}

// WebKit lays the dashboard out short for a moment when a repaint swaps nodes under its
// container query (#landing-main), and that clamps the scroll: a phone at the bottom of the
// page lands near the top on every poll. A repaint keeps the position it found, and puts the
// filter back in a view's head it may have rebuilt (placePill).
const steady = (paint) => (...args) => {
  const landing = $("landing"), top = landing.scrollTop;
  try { return paint(...args); } finally { placePill(); if (landing.scrollTop !== top) landing.scrollTop = top; }
};
const renderLanding = steady(() => {
  if (page === "restore") { if (!$("dash-page").contains(restore.card)) $("dash-page").replaceChildren(restore.card); return restore.card; }
  if (page === "heat") return renderHeatView($("dash-page"), $("session-atlas")._scope || {}, licon, closePage);
  if (page === "usage") return paintUsage();
  if (page === "scratch") return scratchPage();
  if (page === "loops") return renderLoops(cardView("Open loops", loopsView), accounts.loops(loops), navigate, licon);
  paintUsage(); // follows the tool chips
  // With panes, the atlas speaks for itself (its filter row is labelled); the heading is
  // only for the loading and empty states, and hides itself when blank (style.css).
  text($("landing-title"), !booted ? "Reading sessions…" : panes.length ? "" : "No panes yet");
  text($("landing-sub"), !booted ? "Saved history is available while the current inventory loads." : panes.length
    ? "" : tmuxRunning ? "No tmux panes are open. Start a session and it will appear here."
      : `${NO_TMUX} Start a session to bring it back.`);
  renderAtlas($("session-atlas"), panes.filter(accounts.matches), navigate, LOGOS, term => {
    $("search").value = term;
    filter = [];
    renderList();
    navigate();
    $("sessions").scrollTop = $("sb-scroll").scrollTop = $("side-list").scrollTop = 0;
  }, licon, [$("landing-cards")], $("heat-card"), () => openPage("heat"), $("radar-card"), accounts.sessionBadge);
  renderLoopsCard($("loops-card"), accounts.loops(loops), openLane, licon);
});

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
  } else { sizeReview(); if (WIDE.matches) sizeFleet(); refit(); }
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
$("overview-toggle").onclick = () => {
  overviewFolded = !overviewFolded;
  try { localStorage.setItem("tmuxrc-overview-folded", overviewFolded ? "on" : "off"); } catch {}
  render();
};

// The fleet chart docks under the pane on a wide screen, behind a seam: drag it to any
// height and release snaps to strip, medium or tall; a double-click or the chevron folds
// and unfolds. `fleetHeight` is the chosen height, persisted per screen; the shown one is
// capped so the pane's flexible row (the terminal, or the overview alone) keeps
// FLEET_ROOM, which a short window or a tall stacked overview would otherwise take.
const FLEET_KEY = "tmuxrc-fleet-height", STRIP = 40, MEDIUM = 300, FLEET_ROOM = 120;
let fleetHeight = STRIP, fleetShown = STRIP;
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
// The strip's Dashboard swaps the pane for the dashboard page. The dashboard has no Back to
// the pane: the sidebar and the tab bar reach every pane, and the browser's Back returns to it.
const openDashboard = () => navigate(null, "dashboard");
function renderFleetSplit() {
  if (!WIDE.matches || !active) return;
  renderFleet($("fleet"), panes, { open: fleetShown > STRIP, icon: licon, toggle: foldFleet, dashboard: openDashboard });
}
const refreshHistory = (force) => refreshAtlasHistory(request, () => { if (dashboardVisible()) renderLanding(); renderFleetSplit(); }, force);
// Plan limits move on the daemon's minute poll (Claude's every five), so a minute is plenty.
// Which meters a phone shows is a per-viewer choice (Settings › Plan usage, Auto by default).
// The wide sidebar keeps them all unless the same setting is Off, the one choice Settings
// offers there: someone paying per token has no plan limits to watch.
const USAGE_MODES = { auto: "Auto", on: "On", off: "Off" };
let usage = [], loops = null, cachedAt = 0, usageMode = "auto", usagePast = 0, cachedSeq = 0;
try { const saved = localStorage.getItem("tmuxrc-usage"); if (Object.hasOwn(USAGE_MODES, saved)) usageMode = saved; } catch {}
// A strip's last card: the quiet meters' "+N" and, when a reading was kept through failed
// reads, how old, once rather than on every card. Redrawn only when changed or wiped.
function usageEnd(el, list, more = "") {
  const html = el.hidden ? "" : more + staleUsage(list), was = el.querySelector(":scope > .usage-end");
  if (was?._html === html && el.lastElementChild === was) return;
  was?.remove();
  if (html) el.insertAdjacentHTML("beforeend", `<div class="usage-end">${html}</div>`), el.lastElementChild._html = html;
}
const paintUsage = steady(() => {
  // Every meter, most urgent first; Auto folds the quiet ones into a "+N", the
  // strip's last card (usage.js).
  const all = accounts.usage(usage), several = accounts.any();
  const focused = document.activeElement?.id === "usage-more"; // a redrawn strip takes it along
  const { shown, quiet } = shownUsage(byUrgency(all), WIDE.matches && usageMode !== "off" ? "on" : usageMode);
  renderStrip($("usage"), shown);
  usageEnd($("usage"), shown, moreUsage(quiet)); // a tap on "+N" opens Plan usage, as the strip's does
  if (focused) $("usage-more")?.focus({ preventScroll: true });
  $("usage-handle").hidden = $("usage").hidden;
  // The dashboard's card draws every meter as a trend whatever the mode; its view (#view=usage)
  // draws them over time (usage-view.js). The dashboard's tool chips filter both, and a
  // tool with no plan meters hides the card. Several accounts: a labelled group each when
  // wide, the strip on a phone.
  const tools = scope.tools, board = tools.length ? all.filter((a) => tools.includes(a.provider)) : all;
  show("usage-panel", board.length > 0);
  const el = $("usage-board"), grouped = several && WIDE.matches, mode = grouped ? "groups" : "board", order = (list) => several ? byUrgency(list) : list;
  if (el._mode !== mode) { el._mode = mode; el._html = el._groups = null; el.className = "usage-board"; }
  if (grouped) accounts.groups(el, board, (cells, one) => renderUsage(cells, one, { wide: true }));
  else renderUsage(el, order(board), { wide: true });
  accounts.strip(el, several); // grouped too: a line too narrow for its charts scrolls rather than wraps
  if (page !== "usage") return;
  if (usagePast !== usageDays()) refreshCached(true); // past windows: only this view asks for them
  // Every account's meters, whatever the list's account filter: the view picks its own
  // (usage-view.js). Named and chipped as the list names them; a tool filter with no meters
  // (reached by Forward, say) shows them all rather than a blank view.
  const scoped = usage.filter((a) => tools.includes(a.provider)), list = (scoped.length ? scoped : usage).map((a) => {
    const p = (a.dirs || []).map(accounts.profile).find((p) => p?.signed_in);
    return { ...a, acct: p, chip: p ? accounts.chip(p) : "" };
  }).filter((a) => a.acct || !accounts.list().length); // a removed account drops out, as in accounts.usage()
  renderUsageView(cardView("Plan usage", usageView), list, { refetch: () => refreshCached(true), icon: licon("filter", 16) });
});
const usageView = document.createElement("section");
usageView.className = "atlas-panel usage-view";
usageView.ariaLabel = "Plan usage over time";
detailCard($("usage-panel"), "Plan usage", "", () => openPage("usage"));
const loopsView = document.createElement("section");
loopsView.id = "landing-loops";
loopsView.ariaLabel = "Open loops";
detailCard($("loops-card"), "Open loops", "", () => openPage("loops"));
// A lane's count on the card opens the view at that lane, unfolded.
function openLane(lane) {
  openPage("loops");
  const at = loopsView.querySelector(`[data-lane="${lane}"]`);
  if (at) { at.open = true; at.scrollIntoView(); at.firstChild.focus({ preventScroll: true }); }
}
// The scratch gallery, in the content area so the app stays put (the page is sandboxed, so a frame).
const scratchFrame = Object.assign(document.createElement("iframe"), { src: "/scratch/", title: "Scratch", className: "scratch-frame" });
// Its Grid/List sits on its filter's line. The sandboxed page cannot keep the choice, or read
// the app's theme, so the app keeps the one and tells the page both (scratch.html listens).
let scratchView = "grid";
try { scratchView = localStorage.getItem("tmuxrc-scratch-view") || "grid"; } catch {}
const tellScratch = () => scratchFrame.contentWindow?.postMessage({ view: scratchView, light: document.documentElement.classList.contains("light") }, "*"); // its origin is opaque
scratchFrame.addEventListener("load", tellScratch);
// Only the gallery is heard, never a page the viewer shows: it opens a pane, keeps its view, or
// opens one of its pages, a path under /scratch/ here and nothing else (url-state.js).
addEventListener("message", (e) => {
  if (e.source !== scratchFrame.contentWindow) return;
  const { pane, view, open, title } = e.data || {}, href = scratchUrl(open, location.href);
  if (/^%\d+$/.test(pane)) navigate(pane);
  if (["grid", "list"].includes(view)) { scratchView = view; try { localStorage.setItem("tmuxrc-scratch-view", view); } catch {} }
  if (href) viewScratch(href, title);
});
function scratchPage() { return cardView("Scratch", scratchFrame); }
// A gallery page, full screen over the app in a frame of its own (sandboxed by the daemon's CSP
// like every /scratch/ page), under a bar: Back, its title, and a real link out to a tab.
// A fresh frame each time, so its loads add nothing to the app's history; gone once closed.
const viewer = $("scratch-viewer");
function viewScratch(href, title) {
  text($("viewer-title"), String(title || ""));
  $("viewer-out").href = href;
  viewer.querySelector("iframe")?.remove();
  viewer.append(Object.assign(document.createElement("iframe"), { src: href, title: String(title || "Scratch page") }));
  viewer.showModal(); viewer.focus();
}
$("close-viewer").onclick = () => viewer.close();
viewer.addEventListener("close", () => viewer.querySelector("iframe")?.remove());
// A card's view: its head over `node`, built once a visit (route() empties #dash-page).
function cardView(title, node) {
  if (!$("dash-page").contains(node)) $("dash-page").replaceChildren(pageHead(title, licon, closePage), node);
  return node;
}
// Plan usage and open loops are both cached by the server; a minute is fresh enough.
async function refreshCached(force) {
  if (!force && Date.now() - cachedAt < 60000) return;
  cachedAt = Date.now(); usagePast = page === "usage" && usageDays();
  const seq = ++cachedSeq;
  // The accounts with the meters, not after: drawn before them, several accounts' meters
  // stand as one tall grid for a moment before folding into their strip.
  const [u, l] = await Promise.allSettled([request(usagePast ? `/api/usage?past=${usagePast}` : "/api/usage"), request("/api/open-loops"), accounts.refresh()]);
  if (seq !== cachedSeq) return; // a forced fetch overlapped this one: only the latest answers land
  if (u.status === "fulfilled") usage = u.value.accounts || [];
  if (l.status === "fulfilled") loops = l.value;
  paintUsage(); render();
}

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
  // A phone's Chat and mic ride its tab bar either side of Sessions; a pane hides the bar, so
  // its heading takes them, by the overflow menu. Moved only when they are elsewhere: a move
  // on every repaint would drop their focus.
  const callTo = inPane ? $("pane-menu") : $("all-tab");
  if (!wide && $("chat").nextElementSibling !== callTo) inPane ? callTo.before($("live-mode"), $("chat")) : callTo.replaceWith($("chat"), callTo, $("live-mode"));
  const list = !inPane && !dashboard; // a phone's list screen: the only one with search and sort
  show("brand", wide || !list); show("find", wide || list);
  show("back", inPane && !wide); show("close-pane", wide); show("heading", inPane); show("detail", inPane);
  // The main column is never blank on a wide screen: with no pane chosen it answers the
  // question the sidebar cannot, which is what the whole fleet is doing right now.
  show("landing", dashboardVisible()); show("landing-main", !page); show("dash-page", !!page);
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
  // Expunge is offered only where it can work: a Claude Code or Codex pane (the server refuses the rest anyway).
  for (const id of ["expunge-rule", "expunge-pane"]) show(id, ["claude", "codex"].includes(pane?.tool));
  show("move-pane", !!pane && accounts.moveTargets(pane.pane_id, pane.tool).length > 0);
  markEnding($("detail"), pane);
  text($("pane-location"), pane ? [sessionOf(pane), pane.window_name || pane.pane_id].filter(Boolean).join(" / ") : "Waiting for session state");
  // Which account it runs on, when that needs saying: first on a phone, which ellipsizes the end.
  const account = pane && accounts.badge(pane.pane_id);
  if (account) $("pane-location").insertAdjacentHTML(wide ? "beforeend" : "afterbegin", wide ? ` · ${account}` : `${account} `);
  for (const id of ["pane-title", "pane-location"]) $(id).title = $(id).textContent; // both ellipsize: hover shows the full text
  $("detail").dataset.layout = effectiveLayout();
  // Each choice carries a line on what it shows and, wide, a wireframe of it (LAYOUT_THUMBS).
  const layouts = [["summary", "Overview", "The overview alone"], ["terminal", "Terminal", "The terminal alone"]];
  if (wide) layouts.unshift(["auto", "Auto", "Side by side or stacked, to fit"], ["side", "Side by side", "Terminal, overview to its right"], ["stack", "Overview above", "Overview over the terminal"]);
  const picker = $("review-layout");
  if (picker.options.length !== layouts.length) picker.replaceChildren(...layouts.map(([value, label, hint]) => {
    const option = Object.assign(new Option(label, value), { title: hint });
    option.dataset.thumb = LAYOUT_THUMBS[value];
    return option;
  }));
  picker.value = wide && !focusChoice() ? reviewLayout : view;
  refreshViewPicker();
  $("mobile-view-toggle").querySelectorAll("button").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  });
  show("review-divider", reviewing());
  const toggle = $("overview-toggle");
  $("detail").toggleAttribute("data-folded", overviewFolded);
  show("overview-toggle", effectiveLayout() === "side");
  toggle.setAttribute("aria-expanded", String(!overviewFolded));
  toggle.title = toggle.ariaLabel = overviewFolded ? "Show overview" : "Hide overview";
  html(toggle, licon(overviewFolded ? "panelRightOpen" : "panelRightClose", 18));
  show("overview", overviewVisible()); show("terminal", terminalVisible());
  sizeReview(false);
  if (wide) sizeFleet();
  text($("activity"), pane ? badgeOf(pane).label : settled ? "Unavailable" : "Loading");
  $("activity").className = `badge ${pane ? badgeOf(pane).cls : "unknown"}`;
  text($("tool"), pane?.tool || "");
  const missing = loaded && !pane ? (settled ? "This pane is no longer available." : "Reading terminal sessions...") : panePending(pane) ? "Summarizing…" : "Waiting for activity...";
  const headline = paneHeadline(pane) || missing;
  $("status-line").classList.toggle("pending", !paneHeadline(pane) && panePending(pane));
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
  // The card holds a question's answers and a finished turn's offers alike.
  const asking = !!pane?.question && needsYou(pane), offers = offersOf(pane);
  show("question", asking || offers.length > 0);
  const question = asking ? pane.question : null;
  text($("prompt"), question?.ask || question?.prompt || "");
  text($("question-items"), questionItems(question));
  // The widget's own rows, once and folded away: the restatement above is what to read.
  // Folded again for each new pane or command: an expanded one must not carry over.
  const command = $("question-command"), commandKey = `${active}\n${question?.context || ""}`;
  if (command._key !== commandKey) { command._key = commandKey; command.open = false; }
  command.hidden = !question?.context;
  text(command.lastChild, question?.context || "");
  const answered = pane && isAnswered(pane);
  $("question").classList.toggle("answered", !!answered);
  show("answer-status", !!answered);
  text($("answer-status"), answered?.note || "");
  reconcile($("options"), answerOptions(question), (o) => `${active}:${question.prompt}:${o.index}:${o.option}`, () => {
    const button = document.createElement("button");
    button.onclick = () => answer(active, button._option.option, button._option.index);
    return button;
  }, (button, option) => {
    button._option = option; text(button, option.option); button.disabled = sending || !!answered;
    button.classList.toggle("picked", picked(pane, option));
  });
  // An offer: its exact text once, folded to a row (a tap unfolds it), then Send it and Copy.
  reconcile($("offers"), offers, (o) => `${active}:${o}`, () => {
    const node = document.createElement("div"); node.className = "offer";
    node.innerHTML = `<button type="button" class="offer-text" aria-expanded="false"><code></code>${licon("chevronDown", 16)}</button>
      <div class="offer-choices"><button type="button">${licon("up", 18)}Send it</button><button type="button">${licon("clipboard", 18)}Copy</button></div>`;
    const [fold, send, copy] = node.querySelectorAll("button");
    fold.onclick = () => fold.setAttribute("aria-expanded", fold.getAttribute("aria-expanded") === "false");
    send.onclick = () => answer(active, node._offer, -1, true);
    copy.onclick = () => copyText(node._offer);
    return node;
  }, (node, value) => {
    node._offer = value; text(node.querySelector("code"), value);
    const send = node.querySelector(".offer-choices button"); send.disabled = sending || !!answered; send.classList.toggle("picked", picked(pane, { option: value, index: -1 }));
  });
  for (const fold of $("offers").querySelectorAll('[aria-expanded="false"]')) fold.classList.toggle("fits", fold.firstChild.scrollWidth <= fold.firstChild.clientWidth);
  renderTasks(pane);
  renderRichContent(pane);
  updateComposer();
  if (focusPushComposer && pane?.question && needsYou(pane)) {
    focusPushComposer = false;
    requestAnimationFrame(() => $(pane.secret ? "secret" : "reply").focus({ preventScroll: true }));
  }
  if (pane && overviewVisible()) loadEvents(pane);
}

// One tappable link row: the Links list's, and the one leading a question's card.
function renderLinks(el, links) {
  reconcile(el, links, (link) => link.href, () => {
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
}

function renderRichContent(pane) {
  // The link the question asks you to open leads its card instead of waiting in Links.
  const ask = pane?.question && needsYou(pane) ? askLink(pane) : null;
  const links = paneLinks(pane).filter((link) => link.href !== ask?.href);
  renderLinks($("ask-link"), ask ? [ask] : []);
  show("link-section", !!links.length);
  renderLinks($("links"), links);
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
  // A copyable of an offer's text would repeat the question card's.
  const tasks = records(pane?.tasks), agents = records(pane?.subagents), copyables = records(pane?.copyables).filter((c) => !offersOf(pane).includes(String(c.text).trim()));
  show("task-section", !!tasks.length); show("agent-section", !!agents.length); show("copy-section", !!copyables.length);
  text($("task-count"), `${tasks.filter((t) => t.done).length}/${tasks.length}`);
  renderItems($("tasks"), tasks); renderItems($("agents"), agents, true);
  reconcile($("copyables"), copyables, (value, i) => `${i}:${value.label}`, () => {
    const node = document.createElement("div"); node.className = "copyable";
    node.innerHTML = '<div class="copy-heading"><strong></strong><button class="icon-button" aria-label="Copy text" title="Copy text">' + licon("clipboard") + '</button></div><pre></pre>';
    node.querySelector("button").onclick = () => copyText(node._value);
    return node;
  }, (node, value) => { node._value = value.text; text(node.querySelector("strong"), value.label); text(node.querySelector("pre"), value.text); });
}
async function copyText(value) {
  try { await navigator.clipboard.writeText(value); notice("Copied to clipboard."); }
  catch { notice("Could not copy. Select the text to copy it."); }
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
    text(node.lastChild, agents ? [value.state === "running" ? "" : value.state, value.progress && `workflow ${value.progress}`, value.elapsed, value.tokens ? `${value.tokens} tokens` : ""].filter(Boolean).join(" · ") : "");
  });
}

async function loadEvents(pane) {
  const key = `${pane.pane_id}:${pane.events_seq ?? pane.updated_at}:${panePending(pane)}`;
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
    show("events-empty", !events.length); markLine($("events-empty"), "", pane, "No recent activity.");
  } catch {
    if (signal.aborted) return;
    eventsKey = null;
    show("events-empty", true); markLine($("events-empty"), "Activity unavailable. Reconnecting...", pane);
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
  fitCols = 0; fitAlt = undefined; // a terminal stream reports afresh: attachment, or the pane's app, may have changed
  if (detailId !== active) {
    $("events").replaceChildren(); text($("events-empty"), "Loading activity..."); show("events-empty", true);
    latestCapture = ""; clearCapture(); detailId = active;
  }
  if (!active || document.hidden) return;
  const pane = panes.find((p) => p.pane_id === active);
  if (terminalVisible()) streamTerminal(active, detailController.signal);
  if (pane && overviewVisible()) loadEvents(pane);
}

// Fit width (docs/design/fit-width.md): the columns this view shows at its font, which the
// daemon sizes a lone pane's window to so its app lays out at the screen's edge: reported
// as the terminal opens, and again on zoom, resize or the toggle. The window keeps the
// width after the view leaves (see the design note for why there is no restore).
function termCols() {
  const probe = $("capture").appendChild(document.createElement("span"));
  probe.textContent = "0".repeat(100);
  const char = probe.offsetWidth / 100, pad = parseFloat(getComputedStyle($("capture")).paddingLeft) * 2;
  probe.remove();
  return char ? Math.max(0, Math.floor(($("terminal-scroll").clientWidth - pad) / char)) : 0;
}
// One at a time, in the order asked: a tick and untick must reach the daemon in that order.
// fitCols is what the daemon last took; a failed report leaves it 0 for the stream to retry.
let fitLine = Promise.resolve(), fitBusy = 0; // fitBusy: reports queued or in flight
// What to fit: the pane's own pick (made in its menu, so it applies even with a terminal
// attached), else this browser's default. Picks are kept per pane incarnation.
const FIT_DEFAULTS = { auto: "Auto", width: "Width only", off: "Off" };
const FIT_PICKS = { "": "Default", width: "Width", tall: "Tall", off: "Off" };
const stored = (k, fallback) => { try { return localStorage.getItem(k) ?? fallback; } catch { return fallback; } };
const store = (k, v) => { try { localStorage.setItem(k, v); } catch {} };
let fitDefault = stored("tmuxrc-fit-default", "auto"), tallRows = Number(stored("tmuxrc-tall-rows", 500)) || 500;
let fitPicks = {}; try { fitPicks = JSON.parse(stored("tmuxrc-fit-picks", "{}")) || {}; } catch {}
const paneKey = (id) => `${id}:${panes.find((p) => p.pane_id === id)?.birth ?? ""}`;
const fitMode = (id) => fitPicks[paneKey(id)] || fitDefault;
function fitTo(id, cols) {
  // Not before the pane is in state: its own pick is filed under its incarnation, which
  // only state knows. fitCols stays 0, so the stream's next frame tries again.
  if (!id || !cols || !panes.some((p) => p.pane_id === id)) return; // a hidden view measures nothing
  const pick = fitPicks[paneKey(id)], mode = fitMode(id);
  if (mode === "off" && !pick) { fitCols = cols; return; } // nothing to send: the window is left as it is
  const body = { cols, mode, rows: tallRows, force: !!pick }, signal = detailController?.signal;
  const current = () => !signal?.aborted && id === active; // a reply to a stream since replaced is stale
  fitBusy++;
  fitLine = fitLine.then(() => post(paneUrl(id, "fit"), body)).finally(() => fitBusy--).then((r) => {
    if (!current() || !terminalVisible()) return fitDue();
    fitCols = cols;
    refit(); // resized while this was in flight: the last size wins
  }, () => { if (current()) fitCols = 0; else fitDue(); }); // our own failure: the stream's next frame retries
}
// The view on screen has no accepted report and none is queued: send one (the stream's first
// once rendered, a retry, or the one a stale reply held up).
const fitDue = () => { if (!fitCols && !fitBusy && terminalVisible()) fitTo(active, termCols()); };
// Mid-gesture sizes coalesce: while a report is in flight its reply measures again.
const refit = () => { const cols = !fitBusy && terminalVisible() && termCols(); if (cols && cols !== fitCols) fitTo(active, cols); };
const refitNow = () => { if (terminalVisible()) fitTo(active, termCols()); }; // this terminal, measured now
function pickFit(v) {
  if (v) fitPicks[paneKey(active)] = v; else delete fitPicks[paneKey(active)];
  store("tmuxrc-fit-picks", JSON.stringify(fitPicks)); paintFit(); refitNow();
}
const paintFit = () => choices("fit-pane", FIT_PICKS, fitPicks[paneKey(active)] || "", pickFit);
$("pane-menu").addEventListener("toggle", () => {
  // The fit is the terminal's, and a choice is filed under the pane's incarnation, which
  // only state knows: offered on the terminal once the pane is in state.
  show("fit-row", terminalVisible() && panes.some((p) => p.pane_id === active));
  if ($("pane-menu").open) paintFit();
});
const setFitDefault = (v) => { fitDefault = v; store("tmuxrc-fit-default", v); paintSettings(); refitNow(); };
$("tall-rows").onchange = (e) => {
  tallRows = Math.max(100, Math.min(2000, Math.round(Number(e.target.value)) || 500));
  e.target.value = tallRows; store("tmuxrc-tall-rows", tallRows); refitNow();
};

function clearCapture() { $("capture").replaceChildren(); captureLines = []; captureDirty = false; latestFrame = paintedFrame = ""; latestGeometry = [0, ""]; shownTop = 0; resetEarlier(); }
const following = () => { const s = $("terminal-scroll"); return s.scrollTop + s.clientHeight >= s.scrollHeight - FOLLOW_SLACK_PX; };
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
// A full-screen app's blank runs squeezed (keptRows): `kept` says which of the capture's
// `count` rows each line is, which a click counts in. Only there: its frame is the whole
// screen (capture_live), so no row of it ever lands in #earlier, which shows rows as they are.
function frameLines() {
  const all = renderCaptureLines(latestCapture, { color: true }), kept = fitAlt ? keptRows(all) : [...all.keys()];
  return Object.assign(lineMarkup(kept.map((i) => all[i])), { kept, count: all.length });
}
const selectionHolds = (lines) => { const s = getSelection(); return !!s && !s.isCollapsed && selectionDirty(s, lines); };
function paintCapture(force = false) {
  // A reflow (new width) or a switch of screen renumbers every row, and trimmed or cleared
  // history moves the frame's first row back: the rows held here no longer line up, so drop them
  // (and, by replacing `earlier`, any chunk still on its way), even if a selection holds
  // this frame back. While one does, nothing loads (loadEarlier), so no rows are placed
  // against a frame that is not the one on screen.
  const [top, screen] = latestGeometry;
  if (screen !== earlier.screen || top < earlier.seen) resetEarlier(top, screen);
  earlier.seen = top;
  const lines = frameLines();
  if (selectionHolds(lines)) { captureDirty = true; return; }
  captureDirty = false;
  const scroll = $("terminal-scroll"), follow = following();
  // Empty, the held range follows the frame while you follow the tail and no request is
  // using it as its edge. Reading further up, it stays, so the rows output scrolls out from
  // under you come back as a gap, and this frame waits for them: loadEarlier lands them
  // and paints it in one step, so what you are reading never jumps in between.
  if (earlier.top === earlier.end && !earlier.busy && follow) earlier.top = earlier.end = top;
  if (!follow && !force && earlier.end < top && !backingOff()) { loadEarlier(); return; }
  paintLines(lines);
  paintedFrame = latestFrame; shownTop = top; shown = lines;
  if (follow) scroll.scrollTop = scroll.scrollHeight;
  loadEarlier();
}
function resetEarlier(top = 0, screen = "") {
  $("earlier").replaceChildren();
  earlier = { top, end: top, seen: top, screen };
  paintEarlierCue();
}
function paintEarlierCue() {
  const cue = earlier.loading ? "Loading earlier…" : earlier.top === 0 && earlier.end > 0 ? "Start of history" : "";
  text($("earlier-cue"), cue); show("earlier-cue", !!cue);
}
// Rows added above what you are reading must not move it: measure the frame's first line,
// make the change, and scroll by however far that line moved. Measured rather than
// computed, so it is right whether or not the browser anchors scrolling itself. `ifAbove`:
// only when you are reading the frame, not rows above it: that line is above the view, or
// there are no rows above it at all.
// Back at the tail, the view stays there instead.
function holdView(change, ifAbove = false) {
  const box = $("terminal-scroll"), mark = $("capture").firstChild || $("capture"), tail = following();
  const y = mark.getBoundingClientRect().top;
  const hold = !ifAbove || !$("earlier").firstChild || y < box.getBoundingClientRect().top; // nothing above: the frame it is
  change();
  if (tail) box.scrollTop = box.scrollHeight;
  else if (hold) box.scrollTop += mark.getBoundingClientRect().top - y;
}
const backingOff = () => performance.now() - (earlier.failed ?? -Infinity) < 3000; // after a failed request
const selected = (node) => { const s = getSelection(); return !!s?.rangeCount && !s.isCollapsed && s.getRangeAt(0).intersectsNode(node); };
// Fill whatever is missing, one request at a time: first a gap (output moved the frame's
// first row past #earlier's end while you read above the tail; left for later while you
// follow it, so a streaming pane costs nothing, and while a selection holds the frame
// back), then older rows while you are within a screen of the top. Each chunk is rendered
// by the live view's own renderer. A failure pauses loading for a few seconds, then retries.
//
// A gap has two parts, split at the painted frame's first row (`shownTop`), and requests
// never straddle it. Rows above it are on screen nowhere (left while you followed, or after
// a failed fetch), so they land at once and, if you are reading the frame, are held above
// it. Rows below it are on screen in the painted frame and leave it only with the frame
// that waits for them: they collect in `held` and land in the same step as that frame, in
// the place the old frame showed them, so whatever you were reading stays put. If a
// selection now holds that frame back, they are dropped and fetched again later.
//
// A line wrapped across a chunk's edge, by tmux (tmux.capture_history says so) or by the
// app's own newline, arrives in two pieces, so each chunk is rendered against the raw rows
// already held at that end (`head` and `tail`, and whether the last row wraps on) and the
// held lines it changes are replaced (besideHeld): a long URL stays one link, in its own
// colors. A held line inside a selection is left alone rather than replaced under it.
async function loadEarlier() {
  const h = earlier, id = active, box = $("terminal-scroll"), pre = $("earlier");
  if (h.busy || !id || !h.screen || captureDirty || backingOff()) return;
  h.busy = true;
  const whole = (raw) => raw.split("\n").length >= pre.children.length; // one end's rows are all there are, so both ends'
  const draw = (node, markup) => { node.innerHTML = `${markup}\n`; node.markup = markup; return node; };
  let held = [], redraws = [], heldEnd = h.end, heldTail, heldWraps, since = 0, older = false; // the staged rows' own, while any are
  const land = (force) => {
    if (paintedFrame !== latestFrame && selectionHolds(frameLines())) { captureDirty = true; return; }
    if (redraws.some(([node]) => selected(node))) return; // selected since: fetched again, left as shown meanwhile
    const tail = following();
    for (const [node, markup] of redraws) draw(node, markup);
    pre.append(...held); Object.assign(h, { end: heldEnd, tail: heldTail, wraps: heldWraps }); if (whole(heldTail)) h.head = heldTail;
    if (tail) box.scrollTop = box.scrollHeight;
    if (paintedFrame !== latestFrame) paintCapture(force); // the frame that waited
  };
  try {
    for (;;) {
      if (captureDirty) return;
      // Only on the way up: off the tail, or by a gesture up at the top of a frame too short
      // to scroll (a few long wrapped lines), one chunk per gesture. Older rows also need you
      // within a screen of the top.
      // Gaps go first, but under steady output there is always one, so after each gap
      // request an older one gets its turn when you are near the top, except while gap rows
      // wait to land (at most 5s): a prepend could replace the piece they are to replace,
      // or a frame painted meanwhile could strand them.
      const up = h.asked || !following(), want = latestGeometry[0];
      older = (!older || heldEnd >= want) && heldEnd === h.end && h.top > 0 && box.scrollTop < box.clientHeight && up;
      const gap = heldEnd < want && up && !older;
      if (!gap && !older) return;
      const unseen = heldEnd < shownTop; // 1000: the daemon's cap
      const before = gap ? Math.min(want, heldEnd + 1000, unseen ? shownTop : Infinity) : h.top;
      if (!gap) holdView(() => { h.loading = true; paintEarlierCue(); });
      const query = new URLSearchParams({ before, lines: gap ? before - heldEnd : EARLIER_LINES }), shown = shownTop;
      const { text: rows, start, wraps, screen } = await request(`${paneUrl(id, "history")}?${query}`);
      if (h !== earlier) return;
      // Numbered differently from the rows held here (history trimmed since, say): none of
      // them can be trusted. Drop them and load nothing until the next live frame says how
      // rows are numbered now (an unknown screen holds loading off, and repaints of the
      // frame already here keep it unknown); the frame waiting, if any, paints as it is.
      if (screen !== h.screen) { latestGeometry = [latestGeometry[0], ""]; paintCapture(true); return; }
      // A frame painted meanwhile moved the split this chunk was asked by: start the gap over.
      if (gap && shownTop !== shown) { held = []; redraws = []; heldEnd = h.end; continue; }
      const edge = gap ? held.at(-1) || pre.lastChild : pre.firstChild;
      const join = edge && !selected(edge);
      const [endRaw, endWraps] = heldEnd !== h.end ? [heldTail, heldWraps] : [h.tail, h.wraps]; // rows staged (a wrap's may add no node)
      const { lines, raw, again } = besideHeld(rows, join ? gap ? endRaw : h.head : null, gap, gap ? endWraps : wraps);
      // The held rows the new ones change are redrawn in place, from the edge in, unless
      // selected; staged rows run on into the shown rows above them.
      const near = (n) => !gap ? n.nextSibling : !held.includes(n) ? n.previousSibling : n !== held[0] ? held[held.indexOf(n) - 1] : pre.lastChild;
      const changes = [];
      for (let n = join && edge, i = 0; n && i < again.length; n = near(n), i++) if (!selected(n)) changes.push([n, again[i]]);
      const redraw = () => { for (const [node, markup] of changes) draw(node, markup); };
      const nodes = lines.map((markup) => draw(document.createElement("span"), markup));
      if (gap) { heldTail = raw; heldWraps = wraps; } else h.head = raw;
      if (!gap) {
        holdView(() => { redraw(); pre.prepend(...nodes); if (whole(h.head)) h.tail = h.head; if (!edge) h.wraps = wraps; h.top = start; h.loading = h.asked = false; paintEarlierCue(); });
      } else if (unseen) {
        holdView(() => { redraw(); pre.append(...nodes); h.end = heldEnd = before; Object.assign(h, { tail: raw, wraps }); if (whole(raw)) h.head = raw; }, true);
      } else {
        redraws.push(...changes); // with the rows that land
        held.push(...nodes); heldEnd = before;
        // Caught up, they land with the newest frame. Output that keeps ahead (over a
        // request's worth, or for 5s) can't hold them back: they land and that frame paints
        // anyway, the rows it passed becoming a gap above it.
        const behind = latestGeometry[0] - heldEnd, now = performance.now();
        since ||= now;
        if (behind <= 0 || behind > 1000 || now - since > 5000) { land(behind > 0); held = []; redraws = []; heldEnd = h.end; since = 0; }
      }
    }
  } catch { h.failed = performance.now(); setTimeout(loadEarlier, 3000); }
  finally {
    const cue = h.loading; // a failed load's cue; untouched otherwise, so a scroll stays put
    h.busy = h.loading = h.asked = false;
    if (h === earlier && cue) holdView(paintEarlierCue);
    // A frame still waiting (its gap failed, or you went back to the tail) paints as is; the
    // rows it skips are above it then, so a later fetch holds them.
    if (h === earlier && paintedFrame !== latestFrame && !captureDirty) paintCapture(true);
  }
}
async function streamTerminal(id, signal) {
  let frame = "";
  text($("terminal-status"), "Connecting");
  while (!signal.aborted) {
    try {
      // An unknown screen (loadEarlier met another numbering) asks for the whole frame again,
      // since only a full answer says how rows are numbered now.
      const query = new URLSearchParams({ frame: latestGeometry[1] ? frame : "" });
      if (liveSession) query.set("session", liveSession);
      const data = await request(`${paneUrl(id, "live")}?${query}`, { signal }, LONG_POLL_TIMEOUT_MS);
      if (signal.aborted) return;
      frame = data.frame || "";
      // A full-screen app came or went: Auto picks again between tall and width only.
      // The first word too: a fit sent before it may have judged a shell the app has since
      // left. Queued, not left to fitDue: a reply already in flight would mark it done.
      // Before the frame: a full-screen app's is drawn squeezed (frameLines).
      if ("alt" in data && data.alt !== fitAlt) { if (fitMode(id) === "auto") refitNow(); fitAlt = data.alt; }
      if (typeof data.text === "string") { latestCapture = data.text; latestFrame = frame; latestGeometry = [data.top, data.screen]; paintCapture(); }
      fitDue();
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
    refreshHistory(); refreshCached();
    pollState(stateController.signal);
  }
}
// Panes of windows this page is closing, by pane id: { birth, pane, label } while the request
// runs, then { birth, done } for each pane the server says it killed. A killed pane stays
// hidden until its %N comes back with another birth (tmux gave the id to a new pane), not
// just until a poll omits it: a watcher tick that read tmux before the kill can publish it
// again any time later, as slow as its classifications run.
const ending = new Map();
function showPanes() {
  panes = statePanes.flatMap((p) => {
    const e = ending.get(p.pane_id);
    if (e?.done && e.birth !== p.birth) ending.delete(p.pane_id);
    return !e || e.birth !== p.birth ? [p] : e.done ? [] : [{ ...p, ending: e.label }];
  });
  // Until the request answers, keep a pane the server already unpublished (drop_window wakes
  // the poll mid-request): only endPane decides it is gone, so the page leaves it cleanly.
  const listed = new Set(statePanes.map((p) => p.pane_id));
  for (const [id, e] of ending) if (!e.done && !listed.has(id)) panes.push({ ...e.pane, ending: e.label });
  noteSessions(panes);
}
const leavePane = (id) => { if (stillOnPane(location.hash, id)) navigate(null, "summary", { mode: "replace" }); };
// Kill or expunge a pane's window. The window as last seen is greyed while the request runs;
// what then hides for good is what the server says it killed, since tmux listed those in the
// kill's own command (a split moved in or out meanwhile is counted right). Errors are rethrown
// for the caller. Only a refusal (4xx, but not 404, which means it's already gone) restores
// the window, because nothing was killed. A 5xx or a lost connection may come after the kill
// (expunge's deletion runs after it), so the target at least stays hidden rather than letting
// stale polls revive a dead window; a reload shows it if it survived.
async function endPane(id, action, label, body, timeout) {
  const pane = statePanes.find((p) => p.pane_id === id);
  const splits = statePanes.filter((p) => pane && p.session === pane.session && p.window_index === pane.window_index);
  const mark = (e, of) => { for (const p of of) if (e) ending.set(p.pane_id, { birth: p.birth, pane: p, ...e }); else ending.delete(p.pane_id); showPanes(); render(); };
  // Forget the greyed splits quietly, leave whichever killed pane is on screen (unless state
  // already shows a newer pane under its id), and only then render: no "no longer available".
  const ended = (gone) => {
    for (const p of splits) ending.delete(p.pane_id);
    for (const p of gone) if ((statePanes.find((q) => q.pane_id === p.pane_id)?.birth ?? p.birth) === p.birth) leavePane(p.pane_id);
    mark({ done: true }, gone);
  };
  mark({ label }, splits);
  try {
    const done = await post(paneUrl(id, action), body, timeout);
    ended((done.killed || []).map(([paneId, birth]) => ({ pane_id: paneId, birth })));
    return done;
  } catch (error) {
    if (error.status >= 400 && error.status < 500 && error.status !== 404) mark(null, splits);
    else ended(pane ? [pane] : []);
    throw error;
  }
}
async function pollState(signal) {
  let version = null;
  while (!signal.aborted) {
    try {
      const data = await request(stateUrl(version || null), { signal }, LONG_POLL_TIMEOUT_MS);
      if (signal.aborted) return;
      version = Number.isFinite(data.version) && data.version > 0 ? data.version : null;
      // A pane opened, closed or started an agent: which account each runs on may have changed
      // (accounts.js). An agent restarted on another account changes nothing here, so at least
      // every 20s as well.
      const ids = (data.panes || []).map((p) => `${p.pane_id}:${p.birth}:${p.tool}`).join(); // birth: a recycled id is a new pane
      if (ids !== paneIds || Date.now() - accountsAt > 20000) { paneIds = ids; accountsAt = Date.now(); accounts.refresh(); }
      const wasRunning = loaded ? tmuxRunning : null; // null: the first answer always asks
      statePanes = data.panes || []; showPanes(); loaded = true; booted = data.booted !== false; tmuxRunning = data.tmux_running !== false; prefix = data.prefix || "C-b";
      if (wasRunning !== tmuxRunning || restore.due()) restore.refresh(); // a server came or went: the offer changes
      $("ctrl-b").hidden = data.prefix === "C-b"; // absent prefix: can't know it's C-b, so show it
      refreshHistory(); refreshCached();
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
  // Text already in a pane's draft when its password prompt appeared was likely typed for
  // it: in any composer, active or not, it must not wait to be sent in the clear later.
  for (const p of panes) {
    if (!p.secret) continue;
    for (const value of [drafts.get(p.pane_id), renderSidebar.drafts.get(p.pane_id)]) {
      if (value?.segments().length) value.replace([]);
    }
  }
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
  // A pane at a password prompt (its tty stopped echoing) swaps the draft for a password
  // field. Its value lives only in that input: never a draft, never stored, cleared
  // when sent or when the prompt goes away.
  const secret = !!panes.find((p) => p.pane_id === active)?.secret;
  if (!secret) $("secret").value = ""; // and its draft is cleared by pruneDrafts
  $("reply").hidden = secret;
  $("secret").hidden = !secret;
  $("attach").hidden = secret;
  // The editor is never locked: a newly opened pane takes typing before its state lands.
  // Only delivery waits: Send, and Enter (the submit checks Send), do nothing until the pane
  // exists, keeping the text; a password prompt arriving with it empties the draft instead.
  $("send").disabled = sending || !available || (secret ? !$("secret").value : !value.segments().length && !value.pendingEnter);
  $("attach").disabled = sending;
  $("keys").querySelectorAll("button").forEach((button) => { button.disabled = sending || !available; });
}
// Returns whether the keys were DELIVERED. Most callers ignore it; the cursor walk
// (web/cursor-pick.js) cannot — a move it wrongly believes happened leaves every later
// step one row out and commits the wrong row.
async function sendKeys(body, id = active) {
  if (sending || !panes.some((p) => p.pane_id === id)) return false;
  let delivered = false;
  sending = true; notice(); render();
  try {
    const sent = await paneInput.run(id, () => post(paneUrl(id, "send"), body));
    // A menu answer the widget ignored went out, but the card goes live again (answer()).
    if (sent?.took === false) notice(UNTAKEN);
    else delivered = sent && "picker" in sent ? sent : true; // a walk's step: the screen it made
    if (delivered && active === id) text($("draft-status"), "Sent");
    startState();
  } catch (error) {
    // 409 on a cursor walk's step: nothing was sent, and the walk rereads and goes again.
    if ("picker" in body && error.status === 409) delivered = STALE;
    // 409 on a menu answer: the pane holds a different question now, and nothing was sent.
    else if (body.question && error.status === 409) { notice("This question changed — check the pane"); startState(); }
    else notice("Delivery could not be confirmed. Check the terminal before retrying.");
  } finally { sending = false; render(); }
  return delivered;
}

// A question's tappable answers, for the pane's question card and the sidebar's Needs you
// cards alike. The row the parser marks as the agent's own free-text field is left to the
// composer (push.renderable_options reads the same mark).
function answerOptions(question) {
  return (Array.isArray(question?.options) ? question.options : []).map((option, index) => ({ option, index })).filter(({ option, index }) => typeof option === "string" && option.trim() && index !== question.free_text);
}
// What makes a question the same ask: its prompt, context, items and subject, and its options unless
// it is a cursor list, whose walk redraws them (a long list scrolls) under one prompt.
const questionKey = (q) => JSON.stringify([q?.prompt, q?.context, q?.items, q?.subject, q?.answer_style === "cursor" ? null : q?.options]);
// The tapped answer while its question is still the one on the card, else null. Once the
// pane shows another question, or none, the answer has landed and is forgotten; a sent
// offer, once the pane's state has moved on (a question-less turn keeps the same key).
function isAnswered(pane) {
  const pending = pendingAnswers.get(pane.pane_id);
  if (pending && (pending.key !== questionKey(pane.question) || "since" in pending && pending.since !== pane.state_since)) pendingAnswers.delete(pane.pane_id);
  return pendingAnswers.get(pane.pane_id) || null;
}
// Whether a pane reads as Needs you: not while a tapped answer holds it. Its card keeps its
// place (group, filter, list slot) for the hold, so the spinner stays under the finger.
// Whether option `o` is the tapped one: its row by text, since a walk renumbers the list,
// and by index too where two rows share the text.
function picked(pane, o) {
  const pending = isAnswered(pane), named = answerOptions(pane.question).filter((x) => x.option === o.option);
  return pending?.option === o.option && (pending.index === o.index || named.length === 1);
}
function asks(pane) { return needsYou(pane) && !isAnswered(pane); }
// A pane's status badge: "Sending", then "Sent", while a tapped answer holds it, never "Needs you".
function badgeOf(pane) {
  const pending = isAnswered(pane);
  return pending ? { label: pending.sent ? "Sent" : "Sending", cls: "running" } : { label: activityLabel(pane), cls: activityClass(pane) };
}
// Holds pane `id`'s card under `pending` at once, "Sending…" and not Needs you. The returned
// settle(sent) ends the send: delivered, "Sent, waiting for the agent" until the pane's next
// state or ANSWER_PENDING_MS; not sent, the options are live again (the notice says why).
function hold(id, pending) {
  pending.note = "Sending…"; pendingAnswers.set(id, pending); render();
  return (sent) => {
    if (pendingAnswers.get(id) !== pending || sent && pending.sent) return; // moved on, or said already
    if (!sent) { pendingAnswers.delete(id); render(); return; }
    pending.sent = true; pending.note = "Sent, waiting for the agent"; render();
    setTimeout(() => { if (pendingAnswers.get(id) === pending) { pendingAnswers.delete(id); render(); } }, ANSWER_PENDING_MS);
  };
}
// A tap holds its card at once: the tapped option spins, the rest go grey, and the card
// stops reading as Needs you, through the send and on until the pane's next state. An
// offer (index -1) is sent as typed text plus Enter, as a text answer is.
async function answer(id, option, index, offer = false) {
  const current = panes.find((p) => p.pane_id === id);
  if (!(offer ? offersOf(current).includes(option) : current?.question && needsYou(current)) || isAnswered(current) || sending) return;
  const settle = hold(id, { key: questionKey(current.question), option, index, ...offer && { since: current.state_since } });
  // A cursor list answers to neither of the other two styles: the row's text and a
  // digit both land in the picker's search box. It needs a verified walk, and the walk
  // stops the moment its pane is off screen, so a sidebar tap opens the pane first.
  const q = offer ? {} : current.question, cursor = q.answer_style === "cursor", body = !cursor && answerBody(q, option, index);
  // Open the pane first, as the cursor walk does, so the keyboard reaches the right one.
  if ((cursor || !body) && active !== id) navigate(id);
  // Delivered: a walk says so as its select lands, before it waits on the picker to close.
  settle(!!(cursor ? await pickCursorRow(cursorIO(id, () => settle(true)), option, index)
    : body ? await sendKeys(body, id) : notice("Use the keyboard for this option.")));
}

// What the agent hands the user to send back (classify.screen_offers), whatever the pane's
// state: choices on the pane's question card and its list card, sent with one tap.
function offersOf(pane) { return (Array.isArray(pane?.offers) ? pane.offers : []).filter((o) => typeof o === "string"); }

// This surface's half of the shared cursor walk. The tap's pending answer greys the
// options for the walk's length, but what keeps a second walk from starting is the
// module's own one-at-a-time lock — `sending` is released between every step — which is
// where that belongs since both surfaces have the same gap.
function cursorIO(id, committed) {
  // Two separate things, both needed. sendKeys takes the captured id, so no move can ever
  // be posted to a pane the walk was not started for — it defaults to `active` for every
  // other caller, but a default resolved at call time is exactly what a multi-second walk
  // must not rely on. And question() reports nothing once `active` has moved off that
  // pane, which STOPS the walk: a picker the user has navigated away from should not go on
  // being driven, let alone committed, out of sight.
  const pane = () => (active === id ? panes.find((p) => p.pane_id === id) : null);
  // What /send holds a step to: the picker screen q was read off. No pane birth is needed:
  // another pane under a recycled id refuses unless it shows that very screen, and then
  // the key does exactly what the walk computed. With no digest (classify grounded no
  // highlight) there is nothing to hold a key to, so none is sent and the walk stops here.
  const send = (body, q) => q.picker
    && sendKeys({ ...body, enter: false, prompt: q.prompt ?? "", picker: q.picker }, id);
  return {
    question: () => pane()?.question || null,
    parsedAt: () => pane()?.parsed_at || 0,
    sendKey: (k, q) => send({ keys: k, literal: false }, q),
    sendText: (t, q) => send({ keys: t, literal: true }, q),
    note: notice,
    committed,
  };
}
// Send a draft to pane `id`: the pane's own composer and a sidebar card's Reply both come
// through here, so they share one endpoint and one confirmation. Resolves to delivered.
// `value` is a Composer, or the password input, whose text goes as the one "secret" field.
// The draft is cleared as it goes, not when the reply lands: typing straight after Enter
// is the next message, and clearing on the reply erased it. A failed send puts the sent
// draft back in front of whatever was typed since (a password only into an empty field).
// A reply to a pane that needs you holds its card as a tapped answer does.
async function compose(id, value) {
  const pane = panes.find((p) => p.pane_id === id);
  const settle = pane && needsYou(pane) && !isAnswered(pane) ? hold(id, { key: questionKey(pane.question), since: pane.state_since }) : () => {};
  const secret = value === $("secret"), sent = secret ? value.value : value.segments();
  const form = new FormData();
  if (secret) form.append("secret", sent);
  else for (const segment of sent) {
    if (segment.file) form.append("image", segment.file);
    else form.append("text", segment.text);
  }
  if (secret) value.value = "";
  else { value.replace([]); value.pendingEnter = false; }
  sending = true; notice(); render();
  try {
    await paneInput.run(id, () => request(paneUrl(id, "compose"), { method: "POST", body: form }, 45000));
    // Not over a next draft typed while this one went: that one is unsent.
    if (active === id && !(secret ? value.value : draft(id).segments().length)) text($("draft-status"), "Sent");
    settle(true); startState();
    return true;
  } catch {
    settle(false);
    if (secret) { if (active === id && !value.value) value.value = sent; } // the one field is the open pane's
    else {
      // Into whichever composer holds this pane's draft now: opening the pane mid-send moves
      // a sidebar Reply's draft into the pane's own composer and detaches the Reply.
      const owner = renderSidebar.drafts.get(id) === value ? value : draft(id), typed = owner.segments();
      owner.replace([...owner.adopt(sent), ...typed.length ? [{ text: "\n" }] : [], ...typed]);
    }
    notice("Delivery could not be confirmed. Draft kept; check the terminal before retrying."); return false;
  } finally { sending = false; render(); }
}
$("reply-form").onsubmit = (event) => {
  event.preventDefault();
  if (!sending && !$("send").disabled) compose(active, $("secret").hidden ? draft() : $("secret"));
};
$("secret").oninput = updateComposer;
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

for (const [id, name] of Object.entries({ "menu-open": "menu", collapse: "panel", "dash-nav": "gauge", "scratch-nav": "images", "close-viewer": "back", "settings-open": "settings", "close-settings": "x", back: "back", "close-pane": "x", "pane-menu-button": "ellipsis", "new-window": "plus", "search-icon": "search", "clear-search": "x", send: "up", attach: "paperclip", keyboard: "keyboard", "close-launch": "x", "close-expunge": "x", "close-move": "x", "zoom-in": "plus", "zoom-out": "minus", tail: "down" })) icon(id, name);
// The phone's tab bar: Sessions, home, in the middle (Chat and Voice are live.js's). The gauge is Dashboard's
// everywhere; the 2×2 grid is the layout picker's. Only Needs you carries a count.
for (const [id, label, glyph] of [["dashboard", "Dashboard", "gauge"], ["all", "Sessions", "layers"], ["attention", "Needs you", "alert"]]) {
  html($(`${id}-tab`), `<span class="nav-icon">${licon(glyph)}${id === "attention" ? '<span id="attention-count" class="count"></span>' : ""}</span><span>${label}</span>`);
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
$("dashboard-tab").onclick = $("dash-nav").onclick = () => openDashboard();
// A phone's ☰ drawer: each row presses the control that already goes there (a tab, Scratch,
// Settings, the docs link), shown when it is and current while it is pressed. A modal
// dialog, so focus stays in it and returns to ☰; a tap off it, Escape or a drag left closes it.
// It slides every way it comes and goes (settle, below), a row's place opening once it is gone.
html($("drawer-nav"), [["dashboard-tab", "Dashboard", "gauge"], ["all-tab", "Sessions", "layers"], ["attention-tab", "Needs you", "alert"], ["scratch-nav", "Scratch", "images"], ["settings-open", "Settings", "settings"], ["docs", "Documentation", "book"]]
  .map(([via, label, glyph]) => `<button type="button" data-via="${via}">${licon(glyph)}<span>${label}</span><span class="count"></span></button>`).join(""));
$("menu-open").onclick = (e) => {
  // One current row, the most specific pressed (rows run general to specific): Scratch, not
  // also the Dashboard it opens within.
  const rows = [...$("drawer-nav").children], here = [...rows].reverse().find((row) => $(row.dataset.via).getAttribute("aria-pressed") === "true");
  for (const row of rows) { row.hidden = $(row.dataset.via).hidden; row.setAttribute("aria-current", row === here ? "page" : "false"); }
  text($("drawer-nav").querySelector('[data-via="attention-tab"] .count'), $("attention-count").textContent);
  openRecent();
  // ☰ first, so closing returns there even when an edge swipe opened it (and not to search, whose
  // focus would bring the keyboard back); then the drawer, not its first row: no ring until Tab.
  // Focus goes back to ☰ only for a keyboard (a click with no pointer, or Escape); after a tap
  // nothing is left focused, so Safari paints no ring.
  keyed = !e.detail && !drag?.live;
  $("menu-open").focus({ preventScroll: true }); drawer.showModal(); drawer.focus();
  if (!drag?.live) { slide(-drawer.offsetWidth); getComputedStyle(drawer, "::backdrop").opacity; settle(true); } // read: the scrim starts clear
};
$("drawer").onclick = (e) => {
  keyed ||= !e.detail; const row = e.target.closest("button, a");
  if (row || e.target === drawer) settle(false, () => row?.dataset.via ? $(row.dataset.via).click() : row?.dataset.pane && navigate(row.dataset.pane));
};
$("drawer").addEventListener("cancel", (e) => { e.preventDefault(); keyed = true; settle(false); });
// Below the places, what you opened last (GET /api/recent, read only as the drawer opens: a
// closed drawer costs a poll nothing), each with when it was opened. The panes paint at once, as
// last read (before any visit, the list's most active, with their age), and again when the read
// lands; scratch's pages then, beneath them, each opening in a tab of its own as the gallery's
// cards do.
let opened = [];
const recentList = (label, rows) => rows.length ? `<h2>${label}</h2>${rows.join("")}` : "";
function paintRecent() {
  const byId = new Map(panes.map((p) => [p.pane_id, p]));
  const recent = opened.length ? opened.flatMap((v) => byId.has(v.pane_id) ? [[byId.get(v.pane_id), since({ state_since: v.at })]] : [])
    : [...panes].sort(newestFirst).slice(0, 5).map((p) => [p, age(p)]);
  html($("drawer-panes"), recentList("Recent", recent.map(([p, when]) => `<button type="button" data-pane="${esc(p.pane_id)}"><span class="pane-icon"><img alt=""></span><span>${esc(paneName(p))}</span><time>${when}</time></button>`)));
  $("drawer-panes").querySelectorAll("img:first-child").forEach((img, i) => markWorking(img, recent[i][0], LOGOS));
}
function openRecent() {
  paintRecent();
  request("/api/recent").then((recent) => {
    opened = recent.panes; paintRecent();
    html($("drawer-scratch"), recentList("Recent scratch", recent.scratch.map((s) => `<a href="/scratch/${esc(s.href)}" target="_blank">${licon("images")}<span>${esc(s.title)}</span><time>${since({ state_since: s.at })}</time></a>`)));
  }, () => {});
}
// It tracks the finger, as Gmail's does: dragged sideways while open, or in from the list's left
// edge, it follows with its scrim fading in step, then settles open or shut, shut once a third of
// it has gone or on a flick. A drag that starts vertical is left to scroll.
const drawer = $("drawer"), still = matchMedia("(prefers-reduced-motion: reduce)");
// .settling animates the release (or an open or close). Only the latest may finish: a new drag
// or settle cancels the one still settling.
let drag = null, settling, keyed;
const slide = (x) => { if (drag) drag.pos = x; drawer.style.transform = `translateX(${x}px)`; drawer.style.setProperty("--shown", 1 + x / drawer.offsetWidth); };
function settle(open, then) {
  clearTimeout(settling); drawer.classList.add("settling"); slide(open ? 0 : -drawer.offsetWidth);
  settling = setTimeout(() => { drawer.classList.remove("settling"); drawer.style.transform = ""; drawer.style.removeProperty("--shown"); if (!open) { drawer.close(); if (!keyed) document.activeElement?.blur(); then?.(); } }, still.matches ? 0 : 200);
}
addEventListener("touchmove", (e) => {
  if (!drag) return;
  const t = e.touches[0], dx = t.clientX - drag.x, dy = Math.abs(t.clientY - drag.y);
  if (!drag.live) {
    if (dy > Math.abs(dx)) { if (dy > 10) drag = null; return; } // vertical: a scroll, not a drag
    if (Math.abs(dx) < 10 || !drag.open && dx < 0) return;
    // From where it is, which may be partway through settling, read before .settling is dropped.
    const x = drawer.open && new DOMMatrix(getComputedStyle(drawer).transform).m41;
    drag.live = true; clearTimeout(settling); drawer.classList.remove("settling"); if (!drawer.open) $("menu-open").click(); drag.base = x === false ? -drawer.offsetWidth : x;
  }
  drag.v = (t.clientX - drag.at) / Math.max(1, e.timeStamp - drag.t); drag.at = t.clientX; drag.t = e.timeStamp;
  slide(Math.min(0, Math.max(-drawer.offsetWidth, drag.base + dx)));
}, { passive: true });
$("scratch-nav").onclick = (e) => { e.preventDefault(); openPage("scratch"); };
$("empty-needs").onclick = () => { const needs = $("empty-needs").dataset.all === "needs"; if (needs) $("search").value = ""; filter = needs ? ["attention"] : []; scope.tools = []; stayPut({ acct: [] }); };
// Close only leaves the pane, exactly like Back (which stands in for it on a narrow screen).
$("back").onclick = $("close-pane").onclick = () => navigate();
// A phone's Back is also a swipe in from the left edge: it presses the Back of the pane, the
// view or Scratch's viewer (whose left edge is kept for it; on the list, the same swipe draws the drawer in). Not under an open sheet, not from a strip scrolled sideways (that swipe scrolls
// it), and not once the URL moved under the finger: Safari's own edge swipe went back already.
// Installed only: in a browser tab that edge is the browser's own Back, and both at once slide
// two things in (☰ still opens the drawer there, and an open one still drags).
let edge = null;
addEventListener("touchstart", (e) => {
  if (drag?.live) return; // a second finger: the drag stays the first one's
  const t = e.touches[0], scrolled = (n) => n instanceof Element && (n.scrollLeft > 0 || scrolled(n.parentNode));
  edge = standalone() && !WIDE.matches && e.touches.length === 1 && t.clientX < 20 && !scrolled(e.target) && !document.querySelector("dialog[open]:not(#scratch-viewer)") ? { x: t.clientX, y: t.clientY, href: location.href } : null;
  drag = !WIDE.matches && e.touches.length === 1 && (drawer.open || edge && !active && !page) ? { x: t.clientX, y: t.clientY, at: t.clientX, t: e.timeStamp, v: 0, open: drawer.open } : null;
}, { passive: true });
// A gesture the page moved under (a touchcancel, or a route() mid-swipe) springs back to where it began.
const dropGesture = () => { edge = null; if (drag?.live) settle(drag.open); drag = null; };
addEventListener("touchcancel", dropGesture);
addEventListener("touchend", (e) => {
  const t = e.changedTouches[0], dx = edge && t.clientX - edge.x;
  if (drag?.live && e.timeStamp - drag.t > 100) drag.v = 0; // held before letting go: no flick
  if (drag?.live) settle(drag.v > .5 || drag.v > -.5 && drag.pos > -drawer.offsetWidth * (drag.open ? 1 : 2) / 3);
  else if (dx > 60 && Math.abs(t.clientY - edge.y) < dx / 2 && location.href === edge.href) (viewer.open ? $("close-viewer") : active ? $("back") : page && $("dash-page").querySelector('[data-key="page-back"]'))?.click();
  edge = drag = null;
});
// Typing only rewrites the URL's q (url-state.js) in place: a whole route() per keystroke would refetch the pane.
const searched = () => { writeHash(formatHash({ ...parseHash(location.hash), q: $("search").value })); renderList(); };
$("search").oninput = searched;
$("clear-search").onclick = () => { $("search").value = ""; searched(); $("search").focus(); };
function setUsageMode(mode) {
  usageMode = mode;
  try { localStorage.setItem("tmuxrc-usage", usageMode); } catch {}
  paintUsage(); paintSettings();
}
// Wide, the strip's top edge is a seam like the fleet chart's: drag it taller to show more
// meters without scrolling them; a double-click restores its own
// height. Dragged under one row it closes, which turns the setting Off rather than keeping
// a zero height, so the menu says why it is gone and the notice's Undo brings it back. The
// height is per screen, like the sidebar's width; unset (the default) is the natural height,
// and CSS caps it so the pane list keeps most of the column.
const USAGE_KEY = "tmuxrc-usage-height", USAGE_MIN = 72;
let usageHeight = 0, usageDrag = 0;
try { usageHeight = Number(localStorage.getItem(USAGE_KEY)) || 0; } catch {}
function sizeUsage(px = usageHeight, persist = false) {
  const el = $("usage"), h = px && Math.max(USAGE_MIN, px);
  el.classList.toggle("sized", !!h);
  el.classList.toggle("closing", !persist && px > 0 && px < USAGE_MIN); // a drag only: keys stop at the floor
  el.style.setProperty("--usage-h", `${h}px`);
  if (persist) {
    usageHeight = h && el.offsetHeight; // what CSS let it be, not an over-drag
    el.style.setProperty("--usage-h", `${usageHeight}px`); // so a taller window later does not grow it past that
    try { if (usageHeight) localStorage.setItem(USAGE_KEY, String(usageHeight)); else localStorage.removeItem(USAGE_KEY); } catch {}
  }
}
// The separator's value and range (a focusable one owes them), from the height
// CSS let the strip be: capped at 60% of the sidebar, natural, or 0 while hidden. Observed, so
// they follow a paint showing or hiding it and a window resize moving the cap alike.
const fitUsage = new ResizeObserver(() => {
  const shown = $("usage").offsetHeight;
  for (const [key, value] of Object.entries({ min: USAGE_MIN, max: Math.round($("sessions").clientHeight * 0.6), now: shown })) $("usage-handle").setAttribute(`aria-value${key}`, value);
});
for (const el of ["usage", "sessions"]) fitUsage.observe($(el));
sizeUsage(); // before the first meters arrive, so they draw at the chosen height
// Only a release commits: a cancelled or lost capture (crossing the breakpoint) puts it back.
seam($("usage-handle"), (e) => sizeUsage(usageDrag = Math.max(1, $("usage").getBoundingClientRect().bottom - e.clientY)), (e) => {
  const was = usageMode, px = e.type === "pointerup" && usageDrag;
  usageDrag = 0;
  if (px >= USAGE_MIN) return sizeUsage(px, true);
  sizeUsage();
  if (!px) return; // a click, or no release
  setUsageMode("off");
  notice("Plan usage hidden. Turn it back on in Settings › Plan usage.", 8000, ["Undo", () => setUsageMode(was)]);
});
$("usage-handle").ondblclick = () => sizeUsage(0, true);
$("usage-handle").onkeydown = (e) => {
  const step = { ArrowUp: 16, ArrowDown: -16 }[e.key];
  if (step) { e.preventDefault(); sizeUsage($("usage").offsetHeight + step, true); }
};
// The strip is a glance; tapping it opens the usage view, where the trend and times read,
// by way of the dashboard, so its Back lands on the Plan usage card.
// An agent filter with no plan meters (it hides the card) is dropped, so the view has some.
const openUsage = () => {
  if (!usage.some((a) => scope.tools.includes(a.provider))) scope.tools = [];
  openDashboard(); openPage("usage");
};
$("usage").onclick = openUsage;
$("usage").onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openUsage(); } };
// Needs you is a one-of filter; Sessions leaves it for the list, keeping any the menu set.
$("list-nav").querySelectorAll("[data-filter]").forEach((button) => { button.onclick = () => { filter = button.dataset.filter === "attention" ? ["attention"] : String(filter) === "attention" ? [] : filter; navigate(); }; }); // a tab is a request for the list
// System follows the device, as index.html's boot script does with nothing stored.
function setTheme(theme) {
  try { if (theme === "system") localStorage.removeItem("tmuxrc-theme"); else localStorage.setItem("tmuxrc-theme", theme); } catch {}
  const light = theme === "light" || (theme === "system" && matchMedia("(prefers-color-scheme: light)").matches);
  document.documentElement.classList.toggle("light", light);
  document.querySelector('meta[name="theme-color"]').content = light ? "#f5f7f6" : "#101312";
  document.querySelector('meta[name="apple-mobile-web-app-status-bar-style"]').content = light ? "default" : "black";
  paintSettings(); tellScratch();
}
matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", () => { if (savedTheme() === "system") setTheme("system"); });
const savedTheme = () => { try { return localStorage.getItem("tmuxrc-theme") || "system"; } catch { return "system"; } };

// Settings (docs/design/sidebar.md): the things you set once. Each choice is a row of
// buttons, one pressed; the accounts are listed, and Manage accounts… opens their sheet.
function choices(id, options, value, pick) {
  html($(id), Object.entries(options).map(([v, label]) => `<button type="button" data-v="${v}" aria-pressed="${v === value}">${label}</button>`).join("")); // html() skips unchanged markup
  // The pressed button is redrawn: keep keyboard focus on its replacement.
  $(id).onclick = (e) => { const b = e.target.closest("[data-v]"); if (b) { pick(b.dataset.v); $(id).querySelector(`[data-v="${b.dataset.v}"]`).focus(); } };
}
function paintSettings() {
  if (!$("settings").open) return;
  html($("settings-accounts"), accounts.rows());
  choices("theme", { system: "System", light: "Light", dark: "Dark" }, savedTheme(), setTheme);
  // Wide, every meter shows, so it is on or off; a phone also has Auto (the urgent ones).
  // Its On is the phone's On when that is the setting, else Auto, so a click on it changes nothing.
  choices("usage-mode", WIDE.matches ? { [usageMode === "on" ? "on" : "auto"]: "On", off: "Off" } : USAGE_MODES, usageMode, setUsageMode);
  choices("sort", SORTS, sort, setSort);
  choices("pin-needs", { on: "On", off: "Off" }, pinNeeds ? "on" : "off", setPinNeeds);
  choices("fit-default", FIT_DEFAULTS, fitDefault, setFitDefault);
  $("tall-rows").value = tallRows;
  show("sort-row", !WIDE.matches); // wide, the filter menu's Sort by sets it
}
$("settings-open").onclick = () => {
  if ($("settings").open) return $("settings").close();
  openBeside($("settings"), $("settings-open")); paintSettings();
  accounts.refresh(); // repaints Settings, as every refresh does while it is open
};
$("close-settings").onclick = () => $("settings").close();
// Wide, Settings and New window dock as a panel beside the sidebar, not over the screen:
// non-modal, so the list stays in view and live, and closed by Escape or a click anywhere
// but the panel or the button that opened it. A phone keeps its sheet.
function openBeside(dialog, opener) {
  if (!WIDE.matches) return dialog.showModal();
  besides().forEach((d) => d !== dialog && d.close()); // one panel at a time
  dialog._opener = opener;
  dialog.classList.add("beside"); dialog.show();
  dialog.focus(); // show() does not move focus as showModal() does: the next Tab enters the panel
}
const besides = () => document.querySelectorAll("dialog.beside[open]");
// Capture phase, so a click that closes a modal over the panel (its ×) still sees the modal open.
document.addEventListener("click", (e) => {
  if (!document.querySelector("dialog:modal")) besides().forEach((d) => { if (!d.contains(e.target) && !d._opener.contains(e.target)) d.close(); });
}, true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !document.querySelector("dialog:modal")) besides().forEach((d) => d.close()); });
// A click in a frame (the Scratch gallery) never reaches this document, but it takes the focus.
addEventListener("blur", () => { if (document.activeElement?.tagName === "IFRAME" && !document.querySelector("dialog:modal")) besides().forEach((d) => d.close()); });
// Closed by its × or Escape, focus goes back to what opened it; not after a launch, whose
// navigation has already put focus on the pane.
for (const d of [$("settings"), $("launch-dialog")]) d.addEventListener("close", () => {
  if (d.classList.contains("beside") && [null, document.body].includes(document.activeElement)) d._opener?.focus({ preventScroll: true });
  d.classList.remove("beside");
});
// A phone's sheets close as their × does when dragged down, or tapped above (on the dim
// backdrop: modal, so the tap reaches nothing under it). Every sheet's heading leads with its ×.
for (const d of document.querySelectorAll("dialog:not(#drawer)")) { // the drawer drags itself
  const close = () => d.querySelector(".dialog-heading button").click();
  dragToDismiss(d, { onClose: close });
  d.addEventListener("click", (e) => {
    const r = d.getBoundingClientRect();
    if (e.target === d && !WIDE.matches && d.matches(":modal") && (e.clientY < r.top || e.clientX < r.left || e.clientX > r.right)) close();
  });
}
// Crossing the breakpoint changes which form a panel takes (sheet or docked): close it.
WIDE.addEventListener?.("change", () => [$("settings"), $("launch-dialog"), $("drawer")].forEach((d) => d.close()));
function zoom(delta) { fontSize = Math.max(9, Math.min(22, fontSize + delta)); $("terminal-scroll").style.setProperty("--term-font", `${fontSize}px`); text($("font-size"), fontSize); $("zoom-out").disabled = fontSize === 9; $("zoom-in").disabled = fontSize === 22; refit(); }
$("zoom-in").onclick = () => zoom(1); $("zoom-out").onclick = () => zoom(-1);
$("tail").onclick = () => { $("terminal-scroll").scrollTop = $("terminal-scroll").scrollHeight; wheelHome(active); };
// Overscroll past the top of the live view scrolls the pane's own app (overscroll.js). Only
// tools whose fullscreen mode takes the wheel: Codex, omp and shells write their history
// into tmux, which the view already shows, and the daemon refuses anything else that has
// not asked for mouse reports (an inline Claude Code), after which this pane stops asking.
// Touch takes the same path, since a fullscreen agent's phone view is one screen and nothing
// else reaches older output.
const WHEEL_TOOLS = new Set(["claude", "opencode", "gemini", "antigravity"]);
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
  if (dy < 0 && box.scrollTop === 0 && (earlier.top > 0 || earlier.end < latestGeometry[0])) { earlier.asked = true; loadEarlier(); }
  if (wheel.off || !dy || !WHEEL_TOOLS.has(pane?.tool) || (dy < 0 && earlier.top > 0)) return; // tmux history first
  if (dy < 0 ? box.scrollTop > 0 : box.scrollTop + box.clientHeight < box.scrollHeight - 1) return;
  wheelQueued += overscroll(wheel, dy, performance.now());
  paintWheelCue(); flushWheel();
  clearTimeout(wheelSpring);
  wheelSpring = setTimeout(() => { if (!wheel.net) wheel.pull = 0; paintWheelCue(); }, IDLE_MS);
}
// Pinch-zoom arrives as a ctrl+wheel. deltaMode counts in px, ~16px lines (Firefox) or pages.
$("terminal-scroll").addEventListener("scroll", loadEarlier, { passive: true });
$("terminal-scroll").addEventListener("wheel", (e) => { if (!e.ctrlKey) overscrollPane(e.deltaY * [1, 16, e.currentTarget.clientHeight][e.deltaMode]); }, { passive: true });
$("terminal-scroll").addEventListener("touchstart", (e) => { touchY = e.touches.length === 1 ? e.touches[0].clientY : null; }, { passive: true });
// A second finger drops the baseline, so the move after a pinch starts a new one.
$("terminal-scroll").addEventListener("touchmove", (e) => {
  if (e.touches.length !== 1) { touchY = null; return; }
  const y = e.touches[0].clientY;
  if (touchY !== null) overscrollPane(touchY - y);
  touchY = y;
}, { passive: true });
// A toolbar mode switch, on by default and persisted per browser as "on"/"off". `apply`
// paints the button's label and tip and applies the mode; returns whether it is on.
function modeSwitch(id, storageKey, apply) {
  let on = true;
  try { on = localStorage.getItem(storageKey) !== "off"; } catch {}
  const paint = () => { $(id).setAttribute("aria-pressed", String(on)); $(id).ariaLabel = $(id).dataset.tip = apply(on); };
  paint();
  $(id).onclick = () => { on = !on; paint(); try { localStorage.setItem(storageKey, on ? "on" : "off"); } catch {} };
  return () => on;
}
// Click mode: a tap on the terminal is a mouse click in the pane (the daemon drops it
// unless the pane's app asked for mouse reports). Select mode is the plain text view, for
// copying. Two explicit modes rather than guessing intent from drag-vs-tap, because a tap
// that meant "place the selection" would otherwise click whatever is under it.
modeSwitch("click-mode", "tmuxrc-click-mode", (on) => {
  $("capture").classList.toggle("clicks", on);
  html($("click-mode"), `${licon(on ? "pointer" : "cursor", 16)}${on ? "Click" : "Select"}`);
  return on
    ? "Click mode: taps click inside the app, like menus and agent rows. Tap to switch to Select for copying text."
    : "Select mode: drag to select and copy text. Tap to switch to Click to use the app's menus and rows.";
});
// Desktop key passthrough (keys.js): with the terminal focused, keystrokes go to the pane,
// so Esc, Tab, arrows, Ctrl chords and plain typing work as in a terminal. Focus is the
// switch: clicking the terminal hands it the keys, clicking the composer takes them back,
// and the terminal's ring shows which has them. An EMPTY composer (or password field)
// still hands on Esc and Ctrl-C, the interrupt keys, since there is nothing in it for them to act on. Off restores
// the browser's own keys (Tab between controls, Ctrl-F find). F6, the browser's own
// move-focus key, is held back from the pane and moves focus to this switch, so a
// keyboard user is never trapped in the terminal. Wide only: a phone has the key row, and
// the CSS hides this switch there.
const keysOn = modeSwitch("key-mode", "tmuxrc-key-passthrough", (on) => {
  $("terminal").classList.toggle("keys-live", on);
  html($("key-mode"), `${licon("keyboard", 16)}${on ? "Keys" : "Keys off"}`);
  return on
    ? "Keys on: with the terminal focused, typing, Esc, Tab, arrows and Ctrl keys go to the pane; F6 leaves the terminal. Click to keep them in the browser."
    : "Keys off: the browser keeps its keys. Click to send keystrokes to the pane while the terminal is focused.";
});
$("key-mode").addEventListener("click", () => { if (keysOn()) $("terminal-scroll").focus(); });
// Not sendKeys: a keystroke must not hold the global `sending` lock (which locks the
// composer and refuses the next key), nor refresh the whole state on every press.
const paneInput = inputQueue(({ pane, keys, literal }) => post(paneUrl(pane, "send"), { keys, literal, enter: false })
  .then(() => true, () => { notice("A keystroke could not be delivered. Check the terminal before typing on."); return false; }));
const passing = () => keysOn() && WIDE.matches && active && terminalVisible();
document.addEventListener("keydown", (e) => {
  if (!passing() || e.defaultPrevented) return;
  const key = tmuxKey(e, !getSelection().isCollapsed);
  // Only the open pane's own composer hands keys on. A sidebar card's Reply is about a
  // pane that may not be on screen, and an interrupt sent there would be sent blind.
  const empty = $("reply").contains(e.target) ? !draft().segments().length : e.target === $("secret") && !$("secret").value;
  const handOff = empty && ["Escape", "C-c"].includes(key?.keys);
  // The scroll box itself, not a link inside it: a focused link keeps Enter and Tab.
  if (!key || !(e.target === $("terminal-scroll") || handOff)) return;
  e.preventDefault();
  if (key.keys === "F6") $("key-mode").focus();
  else paneInput({ pane: active, repeat: e.repeat, ...key });
});
// A paste into the focused terminal types the clipboard's text into the pane.
$("terminal-scroll").addEventListener("paste", (e) => {
  const keys = e.clipboardData.getData("text/plain");
  if (!passing() || !keys) return;
  e.preventDefault();
  paneInput({ pane: active, keys, literal: true });
});
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
  const body = { from_bottom: shown.count - 1 - shown.kept[row], col, frame: paintedFrame }, id = active;
  // A tap that lands on nothing answers sent:false, not an error; a failure is a gone or
  // replaced pane, and it drops any keys queued behind it, so it has to say so.
  paneInput.run(id, () => post(paneUrl(id, "click"), body)).catch(() => notice("The click could not be delivered. Check the terminal before typing on."));
};

// One dialog for both: the session <select> ends in "New session…" (value ""), which swaps
// in directory + name fields and posts /api/sessions instead of /api/windows.
const launchMode = () => {
  const fresh = !$("launch-session").value;
  show("launch-new", fresh); text($("launch-title"), "New session");
};
$("launch-session").onchange = launchMode;
// The name defaults to the directory's basename, shown as the placeholder so typing a
// name of your own simply overrides it.
const sessionName = (dir) => (dir.replace(/\/+$/, "").split("/").pop() || "").replace(/^~$/, "home").replace(/[^\w-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64) || "main"; // the API's limit
$("launch-dir").oninput = () => { $("launch-name").placeholder = sessionName($("launch-dir").value.trim()); };
document.querySelectorAll(".start-session").forEach((b) => { b.onclick = () => openLaunch(true, b); }); // docked beside the sidebar too
$("new-window").onclick = () => openLaunch(false, $("new-window"));
// A phone's New session floats over the list like Gmail's Compose: scrolling down folds it to
// its +, scrolling up or reaching the top opens it out again.
let listTop = 0;
$("sessions").addEventListener("scroll", () => {
  const y = $("sessions").scrollTop;
  if (Math.abs(y - listTop) > 8 || y <= 0) { $("new-window").classList.toggle("folded", y > listTop && y > 0); listTop = y; }
}, { passive: true });
const LAST_DIR = "tmuxrc-last-dir";
let launchPick = null, launchLoad = 0; // the account picker (accounts.js), with several accounts; the newest load
const LAUNCH_DESC = { claude: "Claude Code", codex: "OpenAI Codex CLI", gemini: "Google Gemini CLI", antigravity: "Google Antigravity CLI", opencode: "OpenCode CLI", omp: "omp agent", shell: "A plain shell in the session's folder" };
async function openLaunch(fresh, opener) {
  launchPick?.remove(); launchPick = null;
  const load = ++launchLoad; // a second open, or a close, while this one loads makes it stale
  if (opener) openBeside($("launch-dialog"), opener); else $("launch-dialog").showModal(); text($("launch-error"), "Loading agents..."); $("launch-choices").replaceChildren();
  const sessions = [...new Set(panes.map((p) => p.session).filter(Boolean))];
  $("launch-session").replaceChildren(...sessions.map((s) => new Option(s, s)), new Option("New session…", ""));
  if (fresh || !sessions.length) $("launch-session").value = "";
  $("launch-name").value = ""; // the placeholder (the directory's name) is the default
  try { $("launch-dir").value = localStorage.getItem(LAST_DIR) || "~"; } catch { $("launch-dir").value = "~"; }
  $("launch-dir").oninput(); launchMode();
  // Suggestions only (open panes' and past agent sessions' directories); any path works.
  request("/api/sessions/dirs").then((d) => $("launch-dirs").replaceChildren(...d.dirs.map((dir) => new Option(dir))), () => {});
  try {
    // The accounts too: the picker below draws from them, and on a first load they may not be in yet.
    const [data] = await Promise.all([request("/api/launchers"), accounts.refresh()]);
    if (load !== launchLoad || !$("launch-dialog").open) return;
    text($("launch-error"), "");
    $("launch-choices").replaceChildren(...data.launchers.map((launcher) => {
      const button = document.createElement("button");
      const logo = document.createElement("img");
      logo.src = Object.prototype.hasOwnProperty.call(LOGOS, launcher.icon) ? LOGOS[launcher.icon] : launcher.icon || "/tmux-logomark.svg"; logo.alt = "";
      const label = document.createElement("span");
      const name = document.createElement("strong"); name.textContent = launcher.label;
      // What it starts, one muted line: a variant's model (its label's parenthesis), else the
      // tool; after the account it starts on, where it has several (accounts.picker).
      const desc = document.createElement("small");
      desc.innerHTML = `<span class="acct-on" hidden></span><span class="desc"></span>`;
      desc.lastChild.textContent = /\((.+)\)/.exec(launcher.label)?.[1] || LAUNCH_DESC[launcher.icon] || LAUNCH_DESC[launcher.provider] || "A configured launcher";
      label.append(name, desc);
      // A launcher whose command the daemon can't find stays VISIBLE — the user
      // configured it, so hiding it would only be a second mystery — but is disabled and
      // states the reason, instead of opening a window that dies in milliseconds.
      // The marker outlives the disabled flag, which launchWindow's `finally` clears on
      // every button: without it one failed launch would re-arm the entries the daemon
      // has just told us cannot run.
      if (launcher.unavailable) { button.dataset.unavailable = launcher.unavailable; desc.lastChild.textContent = launcher.unavailable; }
      button.append(logo, label);
      if (!launcher.unavailable) button.insertAdjacentHTML("beforeend", licon("plus"));
      button.disabled = !!launcher.unavailable;
      button.dataset.provider = launcher.provider || "";
      button.onclick = () => launchWindow(launcher.label, button);
      return button;
    }));
    launchPick = accounts.picker($("launch-choices"), $("launch-session"));
  } catch { if (load === launchLoad) text($("launch-error"), "Could not load launchers. Close and try again."); }
}
$("close-launch").onclick = () => $("launch-dialog").close();
let launching = false, launched = null;
// Go to a window the daemon just opened (a launcher, or a chat resume). Record the id BEFORE
// navigating to it: startState only *starts* a fetch, so the hashchange this triggers
// reaches render() while `panes` is still the previous poll's, without the pane that was
// created a moment ago. See awaitingLaunch. `at` is when the daemon reported it.
function openLaunched(id, at = Date.now()) {
  launched = { id, at };
  // The exemption expires on a clock, but only a render can act on it, and renders are
  // driven by /api/state — which may be parked on a 25s long poll. One scheduled render
  // at the deadline is what makes LAUNCH_GRACE_MS mean anything at all. No cancellation: an
  // extra render is idempotent, and both the pane-appeared and user-moved-on cases are
  // already handled (by the pane being found, and by leaveMissingPane's stillOnPane).
  setTimeout(render, at + LAUNCH_GRACE_MS - Date.now());
  startState(); navigate(id);
}
async function launchWindow(launcher, button) {
  const session = $("launch-session").value, name = $("launch-name").value.trim() || $("launch-name").placeholder;
  if (launching) return;
  if (!session && !/^[\w-]{1,64}$/.test(name)) return text($("launch-error"), "Session names are letters, digits, - and _ only.");
  launching = true; text($("launch-error"), session ? "Creating window..." : "Starting session...");
  $("launch-choices").querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    const account = launchPick?.account(button?.dataset.provider);
    const data = await (session ? post("/api/windows", { session, launcher, account })
      : post("/api/sessions", { name, cwd: $("launch-dir").value.trim() || "~", launcher, account }));
    if (!session) try { localStorage.setItem(LAST_DIR, $("launch-dir").value.trim()); } catch {}
    $("launch-dialog").close(); openLaunched(data.pane_id);
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

const standalone = () => navigator.standalone || matchMedia("(display-mode: standalone)").matches; // installed, not a browser tab
function fitViewport() {
  const viewport = window.visualViewport, root = document.documentElement;
  const focused = document.activeElement;
  const textInput = focused?.tagName === "INPUT"
    && ["text", "search", "email", "url", "tel", "number", "password"].includes(focused.type);
  const editing = focused?.isContentEditable
    || ((textInput || focused?.tagName === "TEXTAREA") && !focused.readOnly && !focused.disabled);
  // Installed mode lets iOS reserve the status bar outside the app. Fill that
  // available viewport while browsing; editors still follow the keyboard.
  root.classList.toggle("standalone-fill", !!standalone() && !editing);
  root.classList.toggle("editing", !!editing); // the keyboard is up: the tab bar steps aside (style.css)
  // Only an editor measures: iOS resizes the visual viewport, not the layout viewport, for
  // its keyboard. At rest CSS sizes the app from dvh, so a height measured with the keyboard
  // up (or mid-dismissal, or while backgrounded) has nothing left to strand.
  if (!editing) for (const name of ["--app-height", "--app-top"]) root.style.removeProperty(name);
  else if (viewport?.scale === 1) {
    root.style.setProperty("--app-height", `${viewport.height}px`);
    root.style.setProperty("--app-top", `${viewport.offsetTop}px`);
  }
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
const CHROME = { "sb-head": ["brand", "connection", "chat", "live-mode", "collapse"], "sb-nav": ["settings-open"], "sb-sess": ["new-window"], "sb-sticky": ["find"] };
const homes = {};
let railPlaced = null;
function placeChrome() {
  railPlaced = null;
  for (const [slot, ids] of Object.entries(CHROME)) for (const id of ids) {
    if (!homes[id]) $(id).before(homes[id] = document.createComment(id));
    if (WIDE.matches) $(slot).append($(id)); else homes[id].after($(id));
  }
  $("sb-sticky").before($("sb-nav"));
  // The account filter (accounts.js) is a group in the filter menu in both layouts (a phone's
  // opens as a bottom sheet); the active filters' chips are placed by placePill().
  renderSidebar.picker.lastChild.append(accounts.filter, ...renderSidebar.views);
  placePill();
}
// The active filters' chips: under search (a phone's list hides them there: its filter button
// and the list's foot say it), and on a phone's dashboard (no search there) under its header,
// so a filter is never on without saying so. There the filter button moves into the header
// too, beside New window: the dashboard's filter row folds behind it (style.css). A card's own
// view follows the same filters, and on a phone its head is the bar: the button goes there.
function placePill() {
  const head = !WIDE.matches && CARDS[page] && $("dash-page").querySelector(".landing-head"), dash = !WIDE.matches && dashboard;
  const at = head || (dash ? document.querySelector(".app-header") : $("find")), picker = renderSidebar.picker;
  if (accounts.pill.previousElementSibling !== at) at.after(accounts.pill);
  if (head) { if (picker.parentElement !== head) head.append(picker); }
  else if (dash && picker.nextElementSibling !== $("new-window")) $("new-window").before(picker);
  else if (!dash && picker.parentElement !== $("find")) $("find").append(picker);
}
// Folded to the rail, the controls move in the DOM to the order they show in (expand, +,
// search, filter, Dashboard, Chat, Voice, the panes, Settings), so Tab walks them top down.
function placeRail() {
  const rail = $("app").classList.contains("rail");
  if (rail === railPlaced) return;
  if (rail) { $("sb-sticky").after($("sb-nav")); $("sb-nav").append($("chat"), $("live-mode")); $("side-list").after($("settings-open")); } else placeChrome();
  railPlaced = rail;
}
// Two filters that combine, not one radio group: the radios still group by their name.
renderSidebar.picker.lastChild.setAttribute("role", "group");
placeChrome();
// route() again, not just render(): a pane URL without a view opens on a different tab once wide.
const resizeWorkspace = () => { placeChrome(); paintUsage(); route(); };
if (WIDE.addEventListener) WIDE.addEventListener("change", resizeWorkspace);
else if (WIDE.addListener) WIDE.addListener(resizeWorkspace);
// Kill the pane's whole tmux window. Buried in the overflow menu, not on the X: an X reads
// as "close this view", and pressing it should never end a process. 404 means it is already
// gone, the outcome asked for.
dismissable($("pane-menu"));
const closePaneMenu = () => { $("pane-menu").open = false; $("pane-menu-button").focus(); }; // the item just hid: keep keyboard focus on a visible control
// The Layout picker's trigger: one fixed layout glyph (a grid, as Meet's "Adjust view"); its value says which layout is on.
$("review-layout-picker").querySelector("summary").insertAdjacentHTML("afterbegin", licon("dashboard", 18));
html($("kill-pane"), `${licon("power", 18)}<span>Kill window</span>`);
// `birth` is the incarnation the menu or sheet was drawn for: a newer pane under its %N is refused.
async function killWindow(pane, birth) {
  if (!pane || !confirm("Kill this tmux window? Whatever is running in it will end.")) return;
  try { await endPane(pane, `close?birth=${encodeURIComponent(birth ?? "")}`, "Closing…"); } catch (error) { if (error.status !== 404) notice(`Could not kill this window: ${error.detail || error.message}`, 6000); }
}
$("kill-pane").onclick = () => { closePaneMenu(); killWindow(active, panes.find((p) => p.pane_id === active)?.birth); };
// Expunge: kill the window AND delete its agent session's local files (docs/design/expunge.md).
// The server names what it would delete first, and the confirmation is bound to that session id.
html($("expunge-pane"), `${licon("trash", 18)}<span>Expunge</span>`);
// A reload mid-request would hide the outcome of a kill or deletion still running.
const busy = () => sending || launching || [...ending.values()].some((e) => !e.done);
const items = (n) => `${n} local file${n === 1 ? " or folder" : "s or folders"}`;
$("close-expunge").onclick = $("expunge-cancel").onclick = () => $("expunge-dialog").close();
// The sheet says what Expunge would delete, why it can't, or (a window with no saved
// conversation, like a sign-in) offers to close the window instead. `act` is its one action.
const expungeSheet = (what, act, kill) => {
  text($("expunge-what"), what);
  $("expunge-caveat").hidden = !act || kill;
  $("expunge-confirm").hidden = !act;
  html($("expunge-confirm"), kill ? `${licon("power", 18)}<span>Kill window</span>` : `${licon("trash", 18)}<span>Expunge permanently</span>`);
  $("expunge-confirm").onclick = () => { $("expunge-dialog").close(); act(); };
  $("expunge-dialog").showModal();
};
$("expunge-pane").onclick = async () => {
  closePaneMenu();
  const pane = active, title = $("pane-title").textContent;
  const birth = panes.find((p) => p.pane_id === pane)?.birth; // the incarnation this menu is for
  if (!pane) return;
  let plan;
  try { plan = await request(`${paneUrl(pane, "expunge")}?birth=${encodeURIComponent(birth ?? "")}`); } catch (error) { return expungeSheet(error.detail || "Could not find this pane's session."); }
  if (plan.nothing) return expungeSheet("This window has no saved conversation to expunge. Close the window instead?", () => killWindow(pane, birth), true);
  const harness = plan.harness === "codex" ? "Codex" : "Claude Code", n = plan.files.length;
  expungeSheet(`This kills the tmux window of “${title}”, ending everything running in it, and permanently deletes its ${harness} session ${plan.session_id.slice(0, 8)}: ${items(n)}${plan.shared.length ? `, plus its entries in ${plan.shared.join(", ")}` : ""}.${plan.harness === "codex" ? " Codex's own databases keep their copy of the thread." : ""}`, async () => {
    try {
      const done = await endPane(pane, "expunge", "Expunging…", { session_id: plan.session_id, birth }, NO_TIMEOUT);
      notice(`Expunged: ${items(done.files.length)} and ${done.lines} history line${done.lines === 1 ? "" : "s"} deleted.`, 6000);
    } catch (error) { // a refusal (pane gone, 404; or 409) touched nothing and clears; a failure stays
      const refused = [404, 409].includes(error.status);
      // No status means no answer at all: the request may have run to completion.
      if (error.status === undefined) notice("Connection lost; the expunge may have completed. Check that the session no longer resumes.");
      else notice(`Expunge ${refused ? "refused" : "failed"}: ${error.detail || error.message}`, refused ? 6000 : 0);
    }
  });
};
// Move to another account: copy the pane's agent session into another account's dir and
// resume it there in a new window; this pane and the original are left alone
// (docs/design/agent-accounts.md).
html($("move-pane"), `${licon("users", 18)}<span>Move to another account…</span>`);
// Not while a choice runs: the sheet is where its outcome shows.
$("close-move").onclick = () => $("move-choices").inert || $("move-dialog").close();
$("move-dialog").addEventListener("cancel", (e) => { if ($("move-choices").inert) e.preventDefault(); });
$("move-pane").onclick = async () => {
  closePaneMenu();
  const pane = panes.find((p) => p.pane_id === active);
  if (!pane) return;
  // Which account each pane runs on, fresh: a stale list offers the pane its own.
  if (!await accounts.refresh()) return notice("Could not load the accounts.", 6000);
  const targets = accounts.moveTargets(pane.pane_id, pane.tool);
  if (!targets.length) return notice("No other signed-in account to move it to.", 6000);
  // The session it runs now, which every request below is bound to (Expunge's preview names it).
  let session_id;
  try { ({ session_id } = await request(`${paneUrl(pane.pane_id, "expunge")}?birth=${encodeURIComponent(pane.birth ?? "")}`)); }
  catch (error) { return notice(error.detail || "Could not find this pane's session.", 6000); }
  if (!session_id) return notice("This window has no saved conversation to move.", 6000);
  const sheet = (what, choices) => {
    text($("move-what"), what);
    $("move-choices").replaceChildren(...choices.map(([label, sub, act]) => {
      const button = document.createElement("button");
      html(button, `<span><strong>${label}</strong><small></small></span>${licon("chevron")}`);
      text(button.querySelector("small"), sub);
      button.onclick = async () => { // one choice at a time: a second tap waits out the first
        if ($("move-choices").inert) return;
        $("move-choices").inert = true;
        try { await act(); } finally { // and the tapped choice is gone: focus stays in the sheet
          $("move-choices").inert = false;
          $("move-choices").firstElementChild?.focus();
        }
      };
      return button;
    }));
  };
  const go = (id) => { $("move-dialog").close(); openLaunched(id); };
  // No status means no answer at all: the server may have done it, so never just "could not".
  const failed = (what, maybe, error) => notice(error.status === undefined ? `Connection lost; ${maybe}` : `Could not ${what}: ${error.detail || error.message}`, 6000);
  const move = async (p, flags = {}) => {
    try {
      const r = await post(paneUrl(pane.pane_id, "move"), { dir: p.dir, birth: pane.birth, session_id, ...flags }, NO_TIMEOUT);
      sheet(`It is running on ${p.name} in a new window. This window still runs it on this account until it exits.`, [
        ["Go to the new window", "", () => go(r.pane_id)],
        ["Exit the old pane", "Types /exit here, then goes to the new window", async () => {
          try { await post(paneUrl(pane.pane_id, "exit"), { birth: pane.birth, session_id }); }
          catch (error) { failed("exit the old pane", "the old pane may have exited.", error); }
          go(r.pane_id);
        }]]);
    } catch (error) {
      if (error.copied) return sheet(`An older copy exists on ${p.name} (copied ${since({ state_since: error.copied })} ago).`, [
        ["Replace it", "Copies this session over that one's files, then resumes it", () => move(p, { replace: true })],
        ...error.resumable ? [["Resume that copy", "Opens it as it is", () => move(p, { resume_existing: true })]] : []]);
      $("move-dialog").close();
      failed("move the session", "the move may have completed. Look for its new window before trying again.", error);
    }
  };
  sheet(`Copies this ${pane.tool === "codex" ? "Codex" : "Claude Code"} session to the account you pick and resumes it there, in a new window in ${pane.session}. It is a copy, never a move: the original stays readable on this account, and this window keeps running until you exit it. An account that has never opened this folder asks you to trust it first.`,
    targets.map((p) => [accounts.chip(p), p.sub, () => move(p)]));
  $("move-dialog").showModal();
};
window.addEventListener("hashchange", route);
// A push tapped while the app is open: sw.js names the address to show, and it is routed
// here like any move. pushState, not location.hash, so the bare-list "/m" routes too.
navigator.serviceWorker?.addEventListener("message", ({ data }) => {
  if (data?.open) { history.pushState(null, "", data.open); route(); }
});
// Only catch up a frame that was held for a selection; composer keystrokes also fire this.
document.addEventListener("selectionchange", () => { if (terminalVisible() && captureDirty) paintCapture(); });
document.addEventListener("visibilitychange", () => { sendPresence(); startState(); restartDetail(); fitViewport(); });
window.addEventListener("online", () => { startState(); restartDetail(); });
window.addEventListener("pageshow", () => { startState(); restartDetail(); fitViewport(); });
window.addEventListener("pagehide", () => { stateController?.abort(); detailController?.abort(); });
fitViewport(); route(); startState();
setupPush($("push"), notice, licon("bell"));
// A chat Open button offered within the launch grace may name a window too new for
// /api/state; a stale one whose pane has since closed takes the normal gone-pane path.
const openOffered = (id, at) => !panes.some((p) => p.pane_id === id) && awaitingLaunch({ id, at }, id)
  ? openLaunched(id, at) : navigate(id);
const live = setupLiveMode({ request, session: liveSession, licon, wide: WIDE, open: openOffered, report: reportError, onVersion: observeVersion, account: accounts.consent, panes: () => panes, logos: LOGOS });
document.documentElement.classList.add("booted"); // icons in, chrome placed: unveil (style.css)
let assetVersion = null;
function hasDrafts() {
  return [...drafts.values(), ...renderSidebar.drafts.values()].some((value) => value.pendingEnter || value.files.size || value.editor.textContent.length);
}
function observeVersion(version) {
  if (typeof version !== "string" || !version) return;
  if (assetVersion === null) assetVersion = version;
  const changed = version !== assetVersion;
  show("update-notice", changed);
  $("reload-update").disabled = busy();
  // A deploy must not eat another pane's draft, an in-flight action, or a voice session.
  if (changed && !document.hidden && !busy() && !hasDrafts() && !live.isActive() && !document.querySelector("dialog[open]")) location.reload();
}
$("reload-update").onclick = () => {
  if (busy()) return;
  if ((hasDrafts() || live.isActive()) && !confirm("Reload now? Unsent drafts will be discarded and Live Mode will end.")) return;
  location.reload();
};
setInterval(() => { if (!document.hidden) live.refresh(); }, VERSION_POLL_MS);
