import { renderCapture, linkifyText } from "/terminal.js";
import { setupLiveMode } from "/m/live.js";

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
  x: '<path d="m18 6-12 12M6 6l12 12"/>',
  clipboard: '<rect width="8" height="4" x="8" y="2" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>',
  paperclip: '<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  keyboard: '<rect width="20" height="12" x="2" y="6" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h.01M18 14h.01M9 14h6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
};
const licon = (name, size = 20) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${LUCIDE[name]}</svg>`;
const $ = (id) => document.getElementById(id);
const text = (node, value = "") => { if (node.textContent !== String(value)) node.textContent = value; };
const html = (node, value) => { if (node._html !== value) { node.innerHTML = value; node._html = value; } };
const show = (id, visible) => { $(id).hidden = !visible; };
const icon = (id, name) => html($(id), licon(name));
const needsYou = (pane) => pane.activity === "waiting" && pane.waiting_on !== "external";
const activity = (pane) => needsYou(pane) ? "Needs you" : ({ running: "Running", waiting: "Working", idle: "Idle", compacting: "Compacting", unknown: "Unknown" }[pane.activity] || "Unknown");
const paneUrl = (id, path) => `/api/panes/${encodeURIComponent(id)}/${path}`;
const LOGOS = { claude: "/claude.png", codex: "/openai.svg", gemini: "/gemini.svg", shell: "/bash.png" };
const activityClass = (pane) => pane.activity === "waiting" && !needsYou(pane) ? "running" : pane.activity;
const isRunning = (pane) => ["running", "compacting"].includes(activityClass(pane));
function isRecent(pane) {
  const since = pane.state_since == null ? NaN : Number(pane.state_since);
  const idle = Number.isFinite(since) ? Math.max(0, Date.now() / 1000 - since) : pane.idle_seconds || 0;
  return pane.activity !== "idle" || idle < 600;
}
const matchesFilter = (pane) => filter === "attention" ? needsYou(pane) : filter === "running" ? isRunning(pane) : filter === "recent" ? isRecent(pane) : true;
const drafts = new Map();
let panes = [], active = null, view = "summary", filter = "all", loaded = false, booted = false;
let sort = "session";
let sending = false, prefix = "C-b", stateController, detailController, detailId = null;
let eventsKey = null, latestCapture = "", fontSize = 13, pendingAnswer = null;
const liveSession = crypto.randomUUID();

function draft(id = active) {
  if (!drafts.has(id)) drafts.set(id, { text: "", file: null, url: null });
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

async function request(url, options = {}, timeout = 8000) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  const external = options.signal;
  if (external?.aborted) controller.abort();
  external?.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(abort, timeout);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
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

function navigate(id = null, nextView = "summary") {
  const params = new URLSearchParams();
  if (filter !== "all") params.set("filter", filter);
  if (sort !== "session") params.set("sort", sort);
  if (id) { params.set("pane", id); if (nextView === "terminal") params.set("view", "terminal"); }
  location.hash = params.toString();
}

function route() {
  const params = new URLSearchParams(location.hash.slice(1));
  const next = params.get("pane");
  const changed = next !== active;
  if (active) draft().text = $("reply").value;
  active = next;
  view = params.get("view") === "terminal" ? "terminal" : "summary";
  filter = ["attention", "running", "recent"].includes(params.get("filter")) ? params.get("filter") : "all";
  sort = params.get("sort") === "updated" ? "updated" : "session";
  $("sort").value = sort;
  if (changed) {
    $("reply").value = active ? draft().text : "";
    $("overview").scrollTop = 0;
    text($("draft-status"), "");
    show("keys", false);
    $("keyboard").setAttribute("aria-expanded", "false");
    notice();
  }
  render();
  if (active && changed) post(paneUrl(active, "select")).catch(() => notice("Could not focus this pane on the host."));
  restartDetail();
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
  button.classList.toggle("needs-you", needsYou(pane));
  const logo = button.querySelector(".pane-icon img");
  const src = Object.hasOwn(LOGOS, pane.tool) ? LOGOS[pane.tool] : "/tmux-logomark.svg";
  if (logo.getAttribute("src") !== src) logo.src = src;
  logo.alt = pane.tool || "tmux";
  text(button.querySelector("strong"), pane.label || pane.window_name || pane.pane_id);
  const badge = button.querySelector(".badge");
  badge.className = `badge ${activityClass(pane)}`;
  text(badge, activity(pane));
  text(button.querySelector(".row-status"), pane.question?.prompt || pane.status_line || pane.session_summary || "No recent activity");
  text(button.querySelector(".row-meta"), [sort === "updated" ? pane.session : "", pane.tool, pane.model, pane.window_index !== "" && pane.window_index != null ? `Window ${pane.window_index}` : ""].filter(Boolean).join(" / "));
}
function renderList() {
  const query = $("search").value.trim().toLowerCase();
  const subset = panes.filter((p) => matchesFilter(p) && `${p.session} ${p.label} ${p.tool} ${p.status_line}`.toLowerCase().includes(query));
  const sessions = [...new Set(subset.map((p) => p.session))];
  const rows = sort === "updated"
    ? subset.sort((a, b) => (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0))
    : sessions.flatMap((session) => [{ session, group: true }, ...subset.filter((p) => p.session === session)]);
  text($("visible-count"), `${subset.length} pane${subset.length === 1 ? "" : "s"}`);
  reconcile($("pane-list"), rows, (p) => p.group ? `session:${p.session}` : p.pane_id, (p) => {
    if (!p.group) return makeRow(p);
    const label = document.createElement("h2"); label.className = "session-label"; return label;
  }, (node, p) => p.group ? text(node, p.session || "Session") : updateRow(node, p));
  show("empty", !subset.length);
  text($("empty"), !loaded ? "Loading sessions..." : !booted ? "Reading terminal sessions..." : query ? "No matching panes." : filter === "attention" ? "Nothing needs your attention." : filter === "running" ? "No panes are running." : filter === "recent" ? "No recently active panes." : "No tmux panes are open.");
  const waiting = panes.filter(needsYou).length;
  text($("session-count"), loaded ? `${panes.length} panes / ${waiting} need${waiting === 1 ? "s" : ""} you` : "Connecting to your workspace");
  text($("all-count"), panes.length);
  text($("attention-count"), waiting);
  $("all-tab").setAttribute("aria-pressed", filter !== "attention");
  $("attention-tab").setAttribute("aria-pressed", filter === "attention");
  text($("filter-all-count"), panes.length);
  text($("filter-running-count"), panes.filter(isRunning).length);
  text($("filter-recent-count"), panes.filter(isRecent).length);
  $("activity-filters").querySelectorAll("button").forEach((button) => button.setAttribute("aria-pressed", filter === button.dataset.filter));
  $("new-window").disabled = !panes.length;
}

function render() {
  const pane = panes.find((p) => p.pane_id === active);
  const inPane = !!active;
  show("sessions", !inPane); show("list-nav", !inPane); show("brand", !inPane);
  show("back", inPane); show("heading", inPane); show("detail", inPane);
  renderList();
  if (!inPane) return;
  text($("pane-title"), pane?.label || "Pane unavailable");
  text($("pane-location"), pane ? `${pane.session} / ${pane.window_name || pane.pane_id}` : "Waiting for session state");
  $("summary-tab").setAttribute("aria-pressed", view === "summary");
  $("terminal-tab").setAttribute("aria-pressed", view === "terminal");
  show("overview", view === "summary"); show("terminal", view === "terminal");
  text($("activity"), pane ? activity(pane) : "Unavailable");
  $("activity").className = `badge ${pane ? activityClass(pane) : "unknown"}`;
  text($("tool"), pane?.tool || "");
  text($("status-line"), pane?.status_line || pane?.session_summary || (loaded && !pane ? "This pane is no longer available." : "Waiting for activity..."));
  text($("metadata"), [pane?.model, pane?.context_pct != null ? `${pane.context_pct}% context` : "", pane?.cost, pane?.elapsed].filter(Boolean).join(" / "));
  $("full-ui").href = "/";
  show("question", !!pane?.question && needsYou(pane));
  const question = pane?.question;
  text($("prompt"), question?.prompt || "");
  const answered = pendingAnswer && pendingAnswer.id === active && pendingAnswer.signature === JSON.stringify(question);
  show("answer-status", !!answered);
  text($("answer-status"), "Answer sent. Waiting for the pane...");
  const options = (question?.options || []).map((option, index) => ({ option, index })).filter(({ option }) => !/^(type\b|other\b|something else|let me|custom|free.?text|write )/i.test(option.trim()));
  reconcile($("options"), options, (o) => `${active}:${question.prompt}:${o.index}:${o.option}`, () => {
    const button = document.createElement("button");
    button.onclick = () => {
      const current = panes.find((p) => p.pane_id === active);
      if (!current?.question || !needsYou(current)) return;
      let keys = button._option.option;
      if (current.question.answer_style === "menu") {
        if (current.question.options.length === 2 && /^(yes|no)$/i.test(keys)) keys = keys[0].toLowerCase();
        else if (current.question.options.length > 2) keys = String(button._option.index + 1);
      }
      sendKeys({ keys, enter: true, literal: true }, true);
    };
    return button;
  }, (button, option) => { button._option = option; text(button, option.option); button.disabled = sending || !!answered; });
  renderTasks(pane);
  updateComposer();
  if (pane && view === "summary") loadEvents(pane);
}

function renderTasks(pane) {
  const tasks = pane?.tasks || [], agents = pane?.subagents || [], copyables = pane?.copyables || [];
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
  if (eventsKey === key || !detailController || detailController.signal.aborted) return;
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
    latestCapture = ""; html($("capture"), ""); detailId = active;
  }
  if (!active || document.hidden) return;
  const pane = panes.find((p) => p.pane_id === active);
  if (view === "terminal") streamTerminal(active, detailController.signal);
  else if (pane) loadEvents(pane);
}

function paintCapture() {
  const selection = getSelection();
  if (selection && !selection.isCollapsed && $("capture").contains(selection.anchorNode)) return;
  const scroll = $("terminal-scroll");
  const follow = scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 48;
  html($("capture"), renderCapture(latestCapture, { color: true }));
  if (follow) scroll.scrollTop = scroll.scrollHeight;
}
async function streamTerminal(id, signal) {
  let frame = "";
  text($("terminal-status"), "Connecting");
  while (!signal.aborted) {
    try {
      const query = new URLSearchParams({ frame, session: liveSession });
      const data = await request(`${paneUrl(id, "live")}?${query}`, { signal }, 35000);
      if (signal.aborted) return;
      frame = data.frame || "";
      if (typeof data.text === "string") { latestCapture = data.text; paintCapture(); }
      text($("terminal-status"), "Live terminal");
      await pause(100, signal);
    } catch {
      if (signal.aborted) return;
      text($("terminal-status"), "Reconnecting...");
      frame = "";
      await pause(1500, signal);
    }
  }
}

function startState() {
  stateController?.abort();
  stateController = new AbortController();
  if (!document.hidden) pollState(stateController.signal);
}
async function pollState(signal) {
  let version = null;
  while (!signal.aborted) {
    try {
      const data = await request(`/api/state${version ? `?v=${version}` : ""}`, { signal }, 35000);
      if (signal.aborted) return;
      version = Number.isFinite(data.version) && data.version > 0 ? data.version : null;
      panes = data.panes || []; loaded = true; booted = data.booted !== false; prefix = data.prefix || "C-b";
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

function resizeReply() { $("reply").style.height = "auto"; $("reply").style.height = `${Math.min(132, $("reply").scrollHeight)}px`; }
function updateComposer() {
  if (!active) return;
  const available = panes.some((p) => p.pane_id === active);
  const value = draft();
  $("reply").readOnly = sending || !available;
  $("send").disabled = sending || !available || (!value.text.trim() && !value.file);
  $("attach").disabled = sending || !available;
  $("remove-image").disabled = sending;
  $("keys").querySelectorAll("button").forEach((button) => { button.disabled = sending || !available; });
  show("attachment", !!value.file);
  if (value.file) { $("attachment-preview").src = value.url; text($("attachment-name"), value.file.name); }
  else $("attachment-preview").removeAttribute("src");
  resizeReply();
}
function clearImage(value) {
  if (value.url) URL.revokeObjectURL(value.url);
  value.file = value.url = null;
}
async function sendKeys(body, answer = false) {
  if (sending || !panes.some((p) => p.pane_id === active)) return;
  const id = active;
  const signature = JSON.stringify(panes.find((p) => p.pane_id === id)?.question);
  sending = true; notice(); render();
  try {
    await post(paneUrl(id, "send"), body);
    if (answer) {
      pendingAnswer = { id, signature };
      setTimeout(() => { if (pendingAnswer?.id === id && pendingAnswer.signature === signature) { pendingAnswer = null; render(); } }, 10000);
    }
    if (active === id) text($("draft-status"), "Sent");
    startState();
  } catch { notice("Delivery could not be confirmed. Check the terminal before retrying."); }
  finally { sending = false; render(); }
}
$("reply-form").onsubmit = async (event) => {
  event.preventDefault();
  if (sending || $("send").disabled) return;
  const id = active, value = draft(), message = value.text;
  sending = true; notice(); render();
  try {
    if (value.file) {
      const form = new FormData(); form.append("file", value.file);
      await request(paneUrl(id, "image"), { method: "POST", body: form }, 30000);
      // An uploaded image is already in tmux. Never upload it twice after a later send failure.
      clearImage(value);
    }
    await post(paneUrl(id, "send"), { keys: message, enter: true, literal: true });
    value.text = "";
    if (active === id) { $("reply").value = ""; text($("draft-status"), "Sent"); }
    startState();
  } catch { notice("Delivery could not be confirmed. Draft kept; check the terminal before retrying."); }
  finally { sending = false; render(); }
};
$("reply").oninput = () => { draft().text = $("reply").value; text($("draft-status"), "Draft"); updateComposer(); };
$("reply").onkeydown = (event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.isComposing) { event.preventDefault(); $("reply-form").requestSubmit(); } };
$("attach").onclick = () => $("image-file").click();
function attachFile(file) {
  if (!file || !active || sending) return;
  if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type) || file.size > 20 * 1024 * 1024) { notice("Choose a PNG, JPEG, WebP, or GIF under 20 MB."); return; }
  const value = draft(); clearImage(value); value.file = file; value.url = URL.createObjectURL(file); updateComposer();
}
$("image-file").onchange = () => { attachFile($("image-file").files[0]); $("image-file").value = ""; };
$("reply").onpaste = (event) => { const file = [...(event.clipboardData?.files || [])].find((f) => f.type.startsWith("image/")); if (file) { event.preventDefault(); attachFile(file); } };
$("remove-image").onclick = () => { clearImage(draft()); updateComposer(); };

for (const [id, name] of Object.entries({ back: "back", theme: "sun", "new-window": "plus", "search-icon": "search", send: "up", attach: "paperclip", "remove-image": "x", "close-launch": "x", "zoom-in": "plus", "zoom-out": "minus", tail: "down" })) icon(id, name);
html($("keyboard"), licon("keyboard", 16) + "<span>Keys</span>");
html($("all-tab"), licon("layers") + '<span>Sessions</span><span id="all-count" class="count">0</span>');
html($("attention-tab"), licon("alert") + '<span>Needs you</span><span id="attention-count" class="count">0</span>');
for (const [label, key, name] of [["Esc", "Escape"], ["Tab", "Tab"], ["Up", "Up", "up"], ["Down", "Down", "down"], ["Enter", "Enter"], ["Ctrl-C", "C-c"], ["Prefix", "prefix"]]) {
  const button = document.createElement("button"); button.title = label; button.setAttribute("aria-label", label);
  if (name) html(button, licon(name, 18)); else text(button, label);
  button.onclick = () => sendKeys({ keys: key === "prefix" ? prefix : key, enter: false, literal: false });
  $("keys").append(button);
}
$("keyboard").onclick = () => { const open = $("keys").hidden; show("keys", open); $("keyboard").setAttribute("aria-expanded", open); };
$("back").onclick = () => navigate();
$("summary-tab").onclick = () => navigate(active, "summary");
$("terminal-tab").onclick = () => navigate(active, "terminal");
$("all-tab").onclick = () => { filter = "all"; navigate(); };
$("attention-tab").onclick = () => { filter = "attention"; navigate(); };
$("search").oninput = renderList;
$("sort").onchange = () => { sort = $("sort").value; navigate(); };
$("activity-filters").querySelectorAll("button").forEach((button) => { button.onclick = () => { filter = button.dataset.filter; navigate(); }; });
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

$("new-window").onclick = async () => {
  $("launch-dialog").showModal(); text($("launch-error"), ""); $("launch-submit").disabled = true;
  const sessions = [...new Set(panes.map((p) => p.session).filter(Boolean))];
  $("launch-session").replaceChildren(...sessions.map((s) => new Option(s, s)));
  try {
    const data = await request("/api/launchers");
    $("launcher").replaceChildren(...data.launchers.map((l) => new Option(l.label, l.label)));
    $("launch-submit").disabled = !sessions.length || !data.launchers.length;
  } catch { text($("launch-error"), "Could not load launchers. Close and try again."); }
};
$("close-launch").onclick = () => $("launch-dialog").close();
$("launch-form").onsubmit = async (event) => {
  event.preventDefault();
  if ($("launch-submit").disabled) return;
  $("launch-submit").disabled = true; text($("launch-error"), "");
  try {
    const data = await post("/api/windows", { session: $("launch-session").value, launcher: $("launcher").value });
    $("launch-dialog").close(); startState(); navigate(data.pane_id);
  } catch { text($("launch-error"), "Creation could not be confirmed. Check sessions before retrying."); }
  finally { $("launch-submit").disabled = false; }
};

function fitViewport() {
  // iOS resizes the visual viewport, not the layout viewport, when its keyboard opens.
  const viewport = window.visualViewport;
  if (!viewport || viewport.scale !== 1) return;
  document.documentElement.style.setProperty("--app-height", `${viewport.height}px`);
  window.scrollTo(0, 0);
}
window.visualViewport?.addEventListener("resize", fitViewport);
window.addEventListener("resize", fitViewport);
window.addEventListener("hashchange", route);
document.addEventListener("selectionchange", () => { if (view === "terminal") paintCapture(); });
document.addEventListener("visibilitychange", () => { startState(); restartDetail(); });
window.addEventListener("online", () => { startState(); restartDetail(); });
window.addEventListener("pageshow", () => { startState(); restartDetail(); fitViewport(); });
window.addEventListener("pagehide", () => { stateController?.abort(); detailController?.abort(); });
fitViewport(); route(); startState();
setupLiveMode({ request, session: liveSession });
