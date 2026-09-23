import { headerPicker } from "/m/header-picker.js";
import { renderAtlas, refreshAtlasHistory } from '/m/atlas.js';
import { renderCaptureLines, linkifyText } from "/terminal.js";
import { setupLiveMode } from "/m/live.js";
import { Composer } from "/m/composer.js";
import { pickCursorRow } from "/cursor-pick.js";
import { needsYou, activityLabel, activityClass, isRunning, isRecent, matchesFilter, lastActivity, stillOnPane, paneName, awaitingLaunch, LAUNCH_GRACE_MS } from "/m/pane-model.js";

const refreshSortPicker = headerPicker(document.getElementById("sort"));
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
  mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/>',
  clipboard: '<rect width="8" height="4" x="8" y="2" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>',
  paperclip: '<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  keyboard: '<rect width="20" height="12" x="2" y="6" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h.01M18 14h.01M9 14h6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  monitor: '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8M12 17v4"/>',
  pointer: '<path d="M4.037 4.688a.495.495 0 0 1 .651-.651l16 6.5a.5.5 0 0 1-.063.947l-6.124 1.58a2 2 0 0 0-1.438 1.435l-1.579 6.126a.5.5 0 0 1-.947.063z"/>',
  cursor: '<path d="M17 22h-1a4 4 0 0 1-4-4V6a4 4 0 0 1 4-4h1M7 22h1a4 4 0 0 0 4-4v-1M7 2h1a4 4 0 0 1 4 4v1"/>',
};
const licon = (name, size = 20) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${LUCIDE[name]}</svg>`;
const $ = (id) => document.getElementById(id);
const text = (node, value = "") => { if (node.textContent !== String(value)) node.textContent = value; };
const html = (node, value) => { if (node._html !== value) { node.innerHTML = value; node._html = value; } };
const show = (id, visible) => { $(id).hidden = !visible; };
const icon = (id, name) => html($(id), licon(name));
const paneUrl = (id, path) => `/api/panes/${encodeURIComponent(id)}/${path}`;
const LOGOS = { claude: "/claude.png", codex: "/openai.svg", gemini: "/gemini.svg", shell: "/bash.png" };
const EMPTY_MESSAGE = { all: "No tmux panes are open.", attention: "Nothing needs your attention.", running: "No panes are running.", recent: "No recently active panes." };
// The desktop workspace shows context alongside the live terminal; phones retain tabs.
const WIDE = matchMedia("(min-width: 1100px)");
let reviewLayout = "side";
try { const saved = localStorage.getItem("tmuxrc-review-layout"); if (["side", "stack", "focus"].includes(saved)) reviewLayout = saved; } catch {}
const reviewing = () => WIDE.matches && reviewLayout !== "focus";
const terminalVisible = () => reviewing() || view === "terminal";
const overviewVisible = () => reviewing() || view === "summary";
const drafts = new Map();
let dashboard = false;
const dashboardVisible = () => !active && (WIDE.matches || dashboard);
let panes = [], active = null, view = "summary", filter = "all", loaded = false, booted = false;
let sort = "updated";
let sending = false, prefix = "C-b", stateController, detailController, detailId = null;
let eventsKey = null, latestCapture = "", fontSize = 13, pendingAnswer = null;
// Per-line nodes under #capture, in document order; each caches the markup last written
// to it (_html). Set when a frame was held back for a selection, so selectionchange
// knows there is something to catch up on.
let captureLines = [], captureDirty = false;
let latestFrame = "", paintedFrame = "";
const liveSession = (() => {
  try { return crypto.randomUUID(); }
  catch { return ""; } // Like desktop SESSION_ID: CSPRNG-random or omitted, never guessed.
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
function pause(ms, signal) {
  return new Promise((resolve) => {
    const finish = () => { clearTimeout(timer); signal?.removeEventListener("abort", finish); resolve(); };
    const timer = setTimeout(finish, ms);
    if (signal?.aborted) finish();
    else signal?.addEventListener("abort", finish, { once: true });
  });
}
function notice(message = "") { text($("notice"), message); show("notice", !!message); }

function hashFor(id, nextView) {
  const params = new URLSearchParams();
  if (filter !== "all") params.set("filter", filter);
  if (sort !== "updated") params.set("sort", sort);
  if (id) { params.set("pane", id); if (nextView === "terminal") params.set("view", "terminal"); }
  if (!id && nextView === "dashboard") params.set("view", "dashboard");
  return params.toString();
}

function navigate(id = null, nextView = "summary") {
  location.hash = hashFor(id, nextView);
}

// Leaving a pane the user did not choose to leave — it closed under them. Unlike
// navigate() this must NOT push a history entry: the pane is gone, so a Back that
// returns to it would land on a dead deep link and bounce straight out again. replaceState
// drops the dead URL instead of stacking it, and because it fires no hashchange we route
// synchronously — otherwise `active` stays on the dead pane long enough for route() to
// POST /select for a pane that no longer exists. `id` is the pane the caller believes is on
// screen; stillOnPane rejects the call when the user has already tapped their way somewhere
// else and only the queued hashchange is late (see pane-model.js).
function leaveMissingPane(id) {
  if (!stillOnPane(location.hash, id)) return;
  const hash = hashFor(null);
  history.replaceState(null, "", hash ? `#${hash}` : location.pathname + location.search);
  route();
}

function route() {
  const params = new URLSearchParams(location.hash.slice(1));
  const next = params.get("pane");
  const changed = next !== active;
  active = next;
  dashboard = !active && params.get("view") === "dashboard";
  view = params.get("view") === "terminal" ? "terminal" : "summary";
  filter = ["attention", "running", "recent"].includes(params.get("filter")) ? params.get("filter") : "all";
  sort = params.get("sort") === "session" ? "session" : "updated";
  $("sort").value = sort;
  refreshSortPicker();
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
  if (active && changed) post(paneUrl(active, "select")).catch(() => notice("Could not focus this pane on the host."));
  if (changed && active) $("back").focus({ preventScroll: true });
}

function makeRow(pane) {
  const button = document.createElement("button");
  button.className = "pane-row";
  button.innerHTML = '<span class="pane-icon"><img width="28" height="28" alt=""></span><span class="row-body"><span class="row-title"><strong></strong><span class="badge"></span></span><span class="row-status"></span><span class="row-meta"></span></span>' + licon("chevron", 16);
  button.onclick = () => navigate(pane.pane_id);
  return button;
}
function updateRow(button, pane) {
  if (pane.pane_id === active) button.setAttribute("aria-current", "true");
  else button.removeAttribute("aria-current");
  button.classList.toggle("needs-you", needsYou(pane));
  const logo = button.querySelector(".pane-icon img");
  const src = Object.prototype.hasOwnProperty.call(LOGOS, pane.tool) ? LOGOS[pane.tool] : "/tmux-logomark.svg";
  if (logo.getAttribute("src") !== src) logo.src = src;
  logo.alt = pane.tool || "tmux";
  text(button.querySelector("strong"), paneName(pane));
  const badge = button.querySelector(".badge");
  badge.className = `badge ${activityClass(pane)}`;
  text(badge, activityLabel(pane));
  text(button.querySelector(".row-status"), pane.question?.prompt || pane.status_line || pane.session_summary || "No recent activity");
  text(button.querySelector(".row-meta"), [sort === "updated" ? pane.session : "", pane.tool, pane.model, pane.window_index !== "" && pane.window_index != null ? `Window ${pane.window_index}` : ""].filter(Boolean).join(" / "));
}
function emptyMessage(query) {
  if (!loaded) return "Loading sessions...";
  if (!booted) return "Reading terminal sessions...";
  if (query) return "No matching panes.";
  return EMPTY_MESSAGE[filter] || EMPTY_MESSAGE.all;
}
function renderList() {
  const query = $("search").value.trim().toLowerCase();
  const subset = panes.filter((p) => matchesFilter(p, filter) && [p.session, p.title, p.label, p.window_name, p.pane_id, p.tool, p.model,
    p.question?.prompt, p.headline, p.status_line, p.session_summary, activityLabel(p),
    p.window_index !== "" && p.window_index != null ? `Window ${p.window_index}` : ""].filter(Boolean).join(" ").toLowerCase().includes(query));
  const sessions = [...new Set(subset.map((p) => p.session))];
  const rows = sort === "updated"
    ? subset.sort((a, b) => lastActivity(b) - lastActivity(a))
    : sessions.flatMap((session) => [{ session, group: true }, ...subset.filter((p) => p.session === session)]);
  reconcile($("pane-list"), rows, (p) => p.group ? `session:${p.session}` : p.pane_id, (p) => {
    if (!p.group) return makeRow(p);
    const label = document.createElement("h2"); label.className = "session-label"; return label;
  }, (node, p) => p.group ? text(node, p.session || "Session") : updateRow(node, p));
  show("empty", !subset.length);
  text($("empty"), emptyMessage(query));
  const waiting = panes.filter(needsYou).length;
  text($("all-count"), panes.length);
  text($("attention-count"), waiting);
  text($("running-count"), panes.filter(isRunning).length);
  text($("recent-count"), panes.filter((pane) => isRecent(pane)).length);
  $("list-nav").querySelectorAll("button").forEach((button) => button.setAttribute("aria-pressed", (!dashboard || WIDE.matches) && filter === button.dataset.filter));
  $("dashboard-tab").setAttribute("aria-pressed", String(dashboard));
  $("new-window").disabled = !panes.length;
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
  text($("landing-title"), !booted ? "Reading sessions…" : panes.length ? "Session atlas" : "No panes yet");
  // With no panes there is no session to open a window IN: + is disabled and the server
  // refuses /api/windows outright. Pointing at it would be advice the UI cannot take, so
  // the empty state says where a session actually comes from instead.
  text($("landing-sub"), !booted ? "Saved history is available while the current inventory loads." : panes.length
    ? "Your workspace at a glance. Explore a cluster, follow a topic, or pick up a waiting pane."
    : "No tmux panes are open. Start a session on the host and it will appear here.");
  renderAtlas($("session-atlas"), panes, navigate, LOGOS);
  landingRows("landing-attention", waiting);
}

// Drag the seam between the sidebar and the main column. Width is a CSS variable the
// grid clamps, so a stored value from a wider window can never strand the layout, and
// persistence is per browser (localStorage) because it is a per-screen preference, not
// something the daemon should know. Arrow keys move it too: the handle is a focusable
// separator, and a pointer-only affordance would be unreachable from the keyboard.
const SIDEBAR_KEY = "tmuxrc-sidebar", SIDEBAR_DEFAULT = 340;
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

const divider = $("divider");
divider.addEventListener("pointerdown", (e) => {
  // Primary button only. A right-click on the seam is a context-menu gesture, not a
  // resize, and preventDefault() here would swallow it.
  if (e.button !== 0) return;
  e.preventDefault();
  divider.setPointerCapture(e.pointerId);
  divider.classList.add("dragging");
  document.body.classList.add("resizing");
});
divider.addEventListener("pointermove", (e) => {
  if (!divider.hasPointerCapture(e.pointerId)) return;
  // Measured from the app's left edge, not the viewport, so it stays correct if the
  // layout ever gains an outer margin.
  setSidebar(e.clientX - $("app").getBoundingClientRect().left);
});
// body.resizing kills pointer-events on the main column, so it sticking on would deaden
// the whole pane. pointerup/pointercancel cover the ordinary endings (capture guarantees
// one of them even if the pointer leaves the window), and lostpointercapture is the
// backstop for the rest: crossing the wide breakpoint mid-drag hides #divider, which
// drops capture without firing either of the other two.
const endResize = (e) => {
  if (e.pointerId !== undefined && divider.hasPointerCapture(e.pointerId)) {
    divider.releasePointerCapture(e.pointerId);
  }
  divider.classList.remove("dragging");
  document.body.classList.remove("resizing");
};
divider.addEventListener("pointerup", endResize);
divider.addEventListener("pointercancel", endResize);
divider.addEventListener("lostpointercapture", endResize);
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
function sizeReview(persist = false, requested = reviewSizes[reviewLayout]) {
  if (!reviewing()) return;
  const side = reviewLayout === "side", rect = $("detail").getBoundingClientRect();
  const min = side ? 240 : 120;
  const max = Math.max(min, Math.floor(side ? rect.width * .45 : rect.height * .5));
  const size = Math.round(Math.max(min, Math.min(max, requested)));
  $("detail").style.setProperty("--review-size", `${size}px`);
  reviewDivider.setAttribute("aria-orientation", side ? "vertical" : "horizontal");
  for (const [key, value] of Object.entries({ min, max, now: size })) reviewDivider.setAttribute(`aria-value${key}`, value);
  if (persist) {
    reviewSizes[reviewLayout] = size;
    try { localStorage.setItem(`tmuxrc-review-${reviewLayout}`, String(size)); } catch {}
  }
}
new ResizeObserver(() => sizeReview()).observe($("detail"));
$("mobile-view-toggle").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-view]");
  if (!button) return;
  $("review-layout").value = button.dataset.view;
  $("review-layout").dispatchEvent(new Event("change", { bubbles: true }));
});
$("review-layout").onchange = (e) => {
  const choice = e.target.value;
  if (WIDE.matches) {
    reviewLayout = ["summary", "terminal"].includes(choice) ? "focus" : choice;
    try { localStorage.setItem("tmuxrc-review-layout", reviewLayout); } catch {}
  }
  if (["summary", "terminal"].includes(choice)) { view = choice; navigate(active, view); }
  restartDetail(); render();
};
reviewDivider.onpointerdown = (e) => {
  if (e.button !== 0) return;
  e.preventDefault(); reviewDivider.setPointerCapture(e.pointerId);
  reviewDivider.classList.add("dragging");
};
reviewDivider.onpointermove = (e) => {
  if (!reviewDivider.hasPointerCapture(e.pointerId)) return;
  const rect = $("detail").getBoundingClientRect();
  sizeReview(true, reviewLayout === "side" ? rect.right - e.clientX : e.clientY - $("overview").getBoundingClientRect().top);
};
const endReviewResize = (e) => {
  if (reviewDivider.hasPointerCapture(e.pointerId)) reviewDivider.releasePointerCapture(e.pointerId);
  reviewDivider.classList.remove("dragging");
};
reviewDivider.onpointerup = reviewDivider.onpointercancel = reviewDivider.onlostpointercapture = endReviewResize;
reviewDivider.onkeydown = (e) => {
  const steps = reviewLayout === "side" ? { ArrowLeft: 16, ArrowRight: -16 } : { ArrowUp: -16, ArrowDown: 16 };
  const step = steps[e.key];
  if (!step) return;
  e.preventDefault(); sizeReview(true, Number(reviewDivider.getAttribute("aria-valuenow")) + step);
};
reviewDivider.ondblclick = () => sizeReview(true, reviewLayout === "side" ? 360 : 280);

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
  // the list and the filter tabs stay up, and Back has nothing to go back TO — the
  // sidebar it would return you to is already there. The brand keeps its slot for the
  // same reason. Narrow is unchanged.
  const wide = WIDE.matches;
  show("sessions", (!inPane && !dashboard) || wide); show("list-nav", !inPane || wide);
  show("brand", !inPane || wide);
  show("back", inPane && !wide); show("heading", inPane); show("detail", inPane);
  // The main column is never blank on a wide screen: with no pane chosen it answers the
  // question the sidebar cannot, which is what the whole fleet is doing right now.
  show("landing", dashboardVisible());
  show("divider", wide);
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
  $("detail").dataset.layout = reviewing() ? reviewLayout : "focus";
  const layouts = [["summary", "Overview"], ["terminal", "Terminal"]];
  if (wide) layouts.unshift(["side", "Side by side"], ["stack", "Overview above"]);
  const picker = $("review-layout");
  if (picker.options.length !== layouts.length) picker.replaceChildren(...layouts.map(([value, label]) => new Option(label, value)));
  picker.value = wide && reviewLayout !== "focus" ? reviewLayout : view;
  refreshViewPicker();
  $("mobile-view-toggle").querySelectorAll("button").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  });
  show("review-divider", reviewing());
  show("overview", overviewVisible()); show("terminal", terminalVisible());
  sizeReview(false);
  text($("activity"), pane ? activityLabel(pane) : settled ? "Unavailable" : "Loading");
  $("activity").className = `badge ${pane ? activityClass(pane) : "unknown"}`;
  text($("tool"), pane?.tool || "");
  const missing = loaded && !pane ? (settled ? "This pane is no longer available." : "Reading terminal sessions...") : "Waiting for activity...";
  const headline = pane?.headline || pane?.status_line || pane?.session_summary || missing;
  html($("status-line"), linkifyText(headline));
  const summary = pane?.session_summary && pane.session_summary !== headline ? pane.session_summary : "";
  html($("session-summary"), linkifyText(summary)); show("session-summary", !!summary);
  const elapsed = pane?.working?.elapsed ?? pane?.elapsed;
  const tokens = pane?.working?.tokens ?? pane?.tokens;
  text($("metadata"), [pane?.model, pane?.context_pct != null ? `${pane.context_pct}% context` : "", pane?.cost, elapsed].filter(Boolean).join(" / "));
  const chips = [pane?.model, pane?.context_pct != null ? `${pane.context_pct}% context` : "", pane?.cost,
    elapsed, tokens ? `${tokens} tokens` : "",
    ...(Array.isArray(pane?.status_entries) ? pane.status_entries.slice(0, 4) : []),
    ({ plan: "Plan mode", "accept-edits": "Accept edits", bypass: "Bypass permissions" })[pane?.mode],
    pane?.agents ? `${pane.agents} agents` : ""].filter(Boolean);
  reconcile($("session-chips"), chips, (_, i) => i, () => document.createElement("span"), (node, value) => text(node, value));
  show("question", !!pane?.question && needsYou(pane));
  const question = pane?.question;
  text($("prompt"), question?.prompt || "");
  const answered = pendingAnswer && pendingAnswer.id === active && pendingAnswer.signature === JSON.stringify(question);
  show("answer-status", !!answered);
  text($("answer-status"), "Answer sent. Waiting for the pane...");
  const options = (Array.isArray(question?.options) ? question.options : []).map((option, index) => ({ option, index })).filter(({ option }) => typeof option === "string" && !/^(type\b|other\b|something else|let me|custom|free.?text|write )/i.test(option.trim()));
  reconcile($("options"), options, (o) => `${active}:${question.prompt}:${o.index}:${o.option}`, () => {
    const button = document.createElement("button");
    button.onclick = () => {
      const current = panes.find((p) => p.pane_id === active);
      if (!current?.question || !needsYou(current)) return;
      let keys = button._option.option;
      // A cursor list answers to neither of the other two styles: the row's text and a
      // digit both land in the picker's search box. It needs a verified walk, shared with
      // the desktop so this surface can't drift behind it again (web/cursor-pick.js).
      if (current.question.answer_style === "cursor") {
        pickCursorRow(cursorIO(active), keys, button._option.index);
        return;
      }
      if (current.question.answer_style === "menu") {
        if (current.question.options.length === 2 && /^(yes|no)$/i.test(keys)) keys = keys[0].toLowerCase();
        else if (current.question.options.length > 2) keys = String(button._option.index + 1);
      }
      sendKeys({ keys, enter: true, literal: true }, true);
    };
    return button;
  }, (button, option) => { button._option = option; text(button, option.option); button.disabled = sending || !!answered; });
  renderTasks(pane);
  renderRichContent(pane);
  updateComposer();
  if (pane && overviewVisible()) loadEvents(pane);
}

function renderRichContent(pane) {
  const links = (Array.isArray(pane?.links) ? pane.links : []).filter((link) => {
    try { return /^https?:$/.test(new URL(link.href).protocol); } catch { return false; }
  });
  show("link-section", !!links.length);
  reconcile($("links"), links, (link, i) => `${i}:${link.href}`, () => {
    const anchor = document.createElement("a");
    anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
    anchor.innerHTML = '<span></span><small></small>' + licon("chevron", 16);
    return anchor;
  }, (anchor, link) => {
    const host = new URL(link.href).host;
    anchor.href = link.href;
    text(anchor.firstChild, Array.from(String(link.text || host).replace(/[\u202A-\u202E\u2066-\u2069]/g, "")).slice(0, 160).join(""));
    text(anchor.querySelector("small"), host);
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
  const records = (items) => Array.isArray(items) ? items.filter((item) => item && typeof item === "object" && !Array.isArray(item)) : [];
  const tasks = records(pane?.tasks), agents = records(pane?.subagents), copyables = records(pane?.copyables);
  show("task-section", !!tasks.length); show("agent-section", !!agents.length); show("copy-section", !!copyables.length);
  text($("task-count"), `${tasks.filter((t) => t.done).length}/${tasks.length}`);
  for (const [id, values] of [["tasks", tasks], ["agents", agents]]) {
    reconcile($(id), values, (value, i) => `${i}:${value.text || value.label}`, () => {
      const node = document.createElement("div"); node.innerHTML = "<span></span><span></span>"; return node;
    }, (node, value) => {
      const done = value.done || value.state === "done";
      node.className = `${id === "tasks" ? "task" : "agent"}${done ? " done" : ""}`;
      html(node.firstChild, licon(done ? "check" : "circle", 16));
      text(node.lastChild, [value.text || value.label, value.elapsed].filter(Boolean).join(" / "));
    });
  }
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
  eventsKey = null;
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
// Line-diff painter (desktop paintTerm): only lines whose markup changed are written, so
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
    refreshAtlasHistory(request, () => { if (dashboardVisible()) renderLanding(); });
    pollState(stateController.signal);
  }
}
async function pollState(signal) {
  let version = null;
  while (!signal.aborted) {
    try {
      const data = await request(`/api/state${version ? `?v=${version}` : ""}`, { signal }, LONG_POLL_TIMEOUT_MS);
      if (signal.aborted) return;
      version = Number.isFinite(data.version) && data.version > 0 ? data.version : null;
      panes = data.panes || []; loaded = true; booted = data.booted !== false; prefix = data.prefix || "C-b";
      refreshAtlasHistory(request, () => { if (dashboardVisible()) renderLanding(); });
      pruneDrafts();
      text($("connection"), data.stale ? "Stalled" : "Live");
      $("connection").classList.toggle("online", !data.stale);
      $("connection").title = data.stale ? "Watcher stalled; pane summaries may be out of date" : "Connected";
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
      pendingAnswer = { id, signature };
      setTimeout(() => { if (pendingAnswer?.id === id && pendingAnswer.signature === signature) { pendingAnswer = null; render(); } }, ANSWER_PENDING_MS);
    }
    if (active === id) text($("draft-status"), "Sent");
    startState();
  } catch { notice("Delivery could not be confirmed. Check the terminal before retrying."); }
  finally { sending = false; render(); }
  return delivered;
}

// This surface's half of the shared cursor walk. No send here sets `pendingAnswer`:
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
$("reply-form").onsubmit = async (event) => {
  event.preventDefault();
  if (sending || $("send").disabled) return;
  const id = active, value = draft(), segments = value.segments();
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
  } catch { notice("Delivery could not be confirmed. Draft kept; check the terminal before retrying."); }
  finally { sending = false; render(); }
};
// Enter SENDS, Shift+Enter inserts a newline — the same contract as the full UI's
// composer, which this one is a port of. It shipped requiring Cmd/Ctrl+Enter, a shortcut
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
$("reply-form").onkeydown = (event) => {
  if (!$("reply").contains(event.target)) return;
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  $("reply-form").requestSubmit();
};
let fileTarget = null;
$("attach").onpointerdown = () => { if (active) draft().saveCaret(); };
$("attach").onclick = () => { fileTarget = active; $("image-file").click(); };
$("image-file").onchange = () => { if (active === fileTarget && !sending) draft().attach($("image-file").files[0]); $("image-file").value = ""; };

for (const [id, name] of Object.entries({ back: "back", theme: "sun", "full-ui": "monitor", "new-window": "plus", "search-icon": "search", send: "up", attach: "paperclip", keyboard: "keyboard", "close-launch": "x", "zoom-in": "plus", "zoom-out": "minus", tail: "down" })) icon(id, name);
for (const [id, label, glyph] of [["all", "All", "layers"], ["running", "Running", "terminal"], ["recent", "Recent", "clock"], ["attention", "Needs you", "alert"]]) {
  html($(`${id}-tab`), `<span class="nav-icon">${licon(glyph)}<span id="${id}-count" class="count">0</span></span><span>${label}</span>`);
}
// The keys worth a thumb on a phone. This row SHARES most of the full UI's key bar but
// is not a copy of it, and diffing the two lists for parity will mislead you: Tab and
// S-Left are here and not there, and Ctrl-B — a literal prefix byte, for nested tmux —
// is there and not here. Each row earns its own entries.
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
// Fourth slot is the SPOKEN name, defaulting to the visible label. Only a button labelled
// with a glyph needs one: a screen reader handed "⇧←" announces two arrow characters,
// or nothing at all — useless for the very key you opened the row to press. The icon
// buttons (Up/Down) already carry words, so they need nothing extra.
for (const [label, key, name, aria = label] of [["Esc", "Escape"], ["Tab", "Tab"], ["Up", "Up", "up"], ["Down", "Down", "down"], ["\u21e7\u2190", "S-Left", null, "Shift+Left"], ["Enter", "Enter"], ["Ctrl-C", "C-c"], ["Ctrl-D", "C-d"], ["Ctrl-O", "C-o"], ["Prefix", "prefix"]]) {
  const button = document.createElement("button"); button.title = aria; button.setAttribute("aria-label", aria);
  if (name) html(button, licon(name, 18)); else text(button, label);
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
$("dashboard-tab").onclick = () => navigate(null, "dashboard");
$("back").onclick = () => navigate();
$("search").oninput = renderList;
$("sort").onchange = () => { sort = $("sort").value; navigate(); };
$("list-nav").querySelectorAll("button[data-filter]").forEach((button) => { button.onclick = () => { filter = button.dataset.filter; navigate(); }; });
function applyTheme(light) {
  document.documentElement.classList.toggle("light", light);
  document.querySelector('meta[name="theme-color"]').content = light ? "#f5f7f6" : "#101312";
  icon("theme", light ? "moon" : "sun");
  $("theme").title = $("theme").ariaLabel = light ? "Use dark theme" : "Use light theme";
}
applyTheme(document.documentElement.classList.contains("light"));
$("theme").onclick = () => { const light = !document.documentElement.classList.contains("light"); applyTheme(light); try { localStorage.setItem("tmuxrc-theme", light ? "light" : "dark"); } catch {} };
function zoom(delta) { fontSize = Math.max(9, Math.min(22, fontSize + delta)); $("capture").style.fontSize = `${fontSize}px`; text($("font-size"), fontSize); $("zoom-out").disabled = fontSize === 9; $("zoom-in").disabled = fontSize === 22; }
$("zoom-in").onclick = () => zoom(1); $("zoom-out").onclick = () => zoom(-1);
$("tail").onclick = () => { $("terminal-scroll").scrollTop = $("terminal-scroll").scrollHeight; };
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

$("new-window").onclick = async () => {
  $("launch-dialog").showModal(); text($("launch-error"), "Loading agents..."); $("launch-choices").replaceChildren();
  const sessions = [...new Set(panes.map((p) => p.session).filter(Boolean))];
  $("launch-session").replaceChildren(...sessions.map((s) => new Option(s, s)));
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
      button.disabled = !sessions.length || !!launcher.unavailable;
      button.onclick = () => launchWindow(launcher.label, button);
      return button;
    }));
  } catch { text($("launch-error"), "Could not load launchers. Close and try again."); }
};
$("close-launch").onclick = () => $("launch-dialog").close();
let launching = false, launched = null;
async function launchWindow(launcher, button) {
  if (launching) return;
  launching = true; text($("launch-error"), "Creating window...");
  $("launch-choices").querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    const data = await post("/api/windows", { session: $("launch-session").value, launcher });
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
  document.documentElement.style.setProperty("--app-height", `${viewport.height}px`);
  document.documentElement.style.setProperty("--app-top", `${viewport.offsetTop}px`);
}
window.visualViewport?.addEventListener("resize", fitViewport);
window.visualViewport?.addEventListener("scroll", fitViewport);
window.addEventListener("resize", fitViewport);
// Crossing the breakpoint changes which elements are hidden, and only render() knows
// that. Without this, widening the window leaves the list hidden until the next poll
// repaints — and narrowing it leaves a sidebar with no room, which is the worse half.
// Older iOS Safari has only the deprecated addListener, and calling the modern name
// unguarded would throw here and abort the whole module — breaking the phone UI to add
// a wide-screen affordance those browsers can never show. Same feature test as the
// desktop app's scheme listener.
const resizeWorkspace = () => { restartDetail(); render(); };
if (WIDE.addEventListener) WIDE.addEventListener("change", resizeWorkspace);
else if (WIDE.addListener) WIDE.addListener(resizeWorkspace);
window.addEventListener("hashchange", route);
// Only catch up a frame that was held for a selection; composer keystrokes also fire this.
document.addEventListener("selectionchange", () => { if (terminalVisible() && captureDirty) paintCapture(); });
document.addEventListener("visibilitychange", () => { startState(); restartDetail(); });
window.addEventListener("online", () => { startState(); restartDetail(); });
window.addEventListener("pageshow", () => { startState(); restartDetail(); fitViewport(); });
window.addEventListener("pagehide", () => { stateController?.abort(); detailController?.abort(); });
fitViewport(); route(); startState();
const live = setupLiveMode({ request, session: liveSession, licon, onVersion: observeVersion });
let assetVersion = null;
function hasDrafts() {
  return [...drafts.values()].some((value) => value.pendingEnter || value.files.size || value.editor.textContent.length);
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
