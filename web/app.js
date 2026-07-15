// termiphone PWA. Polls /api/state, renders one card per pane, floats waiting
// panes to the top, and posts answers back. No framework, no build step.

// Real Claude app icon (downloaded from claude.ai) — no hand-drawn SVG.
const CLAUDE_IMG = '<img src="/claude.png" width="22" height="22" alt="Claude" style="border-radius:5px" />';
const ICONS = { codex: "🔷", gemini: "💎", shell: "$", unknown: "•" };
const iconFor = (tool) => (tool === "claude" ? CLAUDE_IMG : (ICONS[tool] ?? "•"));
const panesEl = document.getElementById("panes");
const liveEl = document.getElementById("live");

// Track which pane's timeline is expanded so a re-render doesn't collapse it.
const openTimelines = new Set();
let busy = false; // suppress polling flicker while an answer is in flight

// The single bottom input bar targets ONE pane at a time (activePane). Tap a card to
// set it; defaults to the top card. Sticky across re-renders so typing isn't lost.
let activePane = null;
let panesById = {}; // latest state per pane, for the bottom bar to act on
function setActive(id) {
  activePane = id;
  render(Object.values(panesById)); // re-render to move the highlight + placeholder
  // Also focus this pane in tmux itself, so the host follows what you tapped.
  fetch(`/api/panes/${encodeURIComponent(id)}/select`, { method: "POST" }).catch(() => {});
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
    const data = await r.json();
    // stale = the watcher loop stopped ticking (dead/stalled); served cards are frozen.
    liveEl.className = data.stale ? "dot off" : "dot";
    liveEl.title = data.stale ? "watcher stalled — cards may be frozen" : "live";
    // Compact usage readout in the top bar (next to the live dot): tokens · $cost ·
    // failed/total calls · calls/min. Lets you SEE the API-call volume (what tripped
    // the 429) at a glance, without a big scary banner for transient errors.
    showUsage(data.usage, data.llm_error);
    render(data.panes || []);
  } catch {
    liveEl.className = "dot off";
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
function escAttr(s) { return String(s).replace(/"/g, "&quot;"); }

function render(states) {
  // Accumulate this poll's events into each pane's running client-side log first.
  states.forEach((s) => accumulateEvents(s.pane_id, s.events));
  panesById = Object.fromEntries(states.map((s) => [s.pane_id, s]));
  if (!states.length) {
    panesEl.innerHTML = '<div class="empty">No tmux pane found.<br>Start a session and it will appear here.</div>';
    updateBar(null);
    return;
  }
  // Waiting first, then running, then idle; stable within group.
  const order = { waiting: 0, running: 1, idle: 2, unknown: 3 };
  states.sort((a, b) => (order[a.activity] ?? 9) - (order[b.activity] ?? 9));
  // Default the active pane to the top card if unset or the active pane vanished.
  if (!activePane || !panesById[activePane]) activePane = states[0].pane_id;
  panesEl.replaceChildren(...states.map(card));
  updateBar(panesById[activePane]);
}

function card(s) {
  const el = document.createElement("div");
  el.className = "card" + (s.activity === "waiting" ? " waiting" : "")
    + (s.pane_id === activePane ? " active" : "");
  // Tapping a card makes it the target of the single bottom input bar.
  el.onclick = (e) => {
    if (e.target.closest("button, input, a, summary, details")) return; // don't steal option/timeline taps
    setActive(s.pane_id);
  };

  const row = document.createElement("div");
  row.className = "row";
  const badge =
    s.activity === "idle"
      ? "idle " + fmtIdle(s.idle_seconds)
      : s.activity === "running"
        ? '<span class="pulse"></span>working'
        : s.activity;
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
      <div class="name">${esc(s.label || "")} ${working}</div>
      <div class="status">${esc(s.headline || "—")}</div>
    </div>
    <span class="badge b-${s.activity}">${badge}</span>`;
  el.appendChild(row);

  el.appendChild(metaRow(s));
  if (s.rewind) el.appendChild(rewindView(s));
  // Tables render BEFORE the question so they act as context above the options.
  if (Array.isArray(s.tables)) s.tables.forEach((t) => el.appendChild(tableView(t)));
  if (s.question) el.appendChild(question(s));
  if (Array.isArray(s.tasks) && s.tasks.length) el.appendChild(tasksView(s.tasks));
  const log = eventLog[s.pane_id] || [];
  if (log.length) el.appendChild(eventsView(log, s.pane_id, s.summary));
  // No per-card input anymore — a single persistent bar at the bottom of the page
  // handles text/keys/images for whichever card is active (see the #bar element).
  el.appendChild(timeline(s));
  return el;
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

// Compact metadata row: model, context bar, cost, mode badge, agent count. Only
// renders the chips that have values, so a plain shell shows nothing here.
const MODE_LABEL = { plan: "plan", "accept-edits": "accept edits", bypass: "bypass perms" };
function metaRow(s) {
  const row = document.createElement("div");
  row.className = "metarow";
  const chips = [];
  if (s.model) chips.push(`<span class="chip">${esc(s.model)}</span>`);
  if (s.context_pct != null)
    chips.push(
      `<span class="chip ctxchip"><i style="width:${s.context_pct}%"></i>${s.context_pct}% ctx</span>`
    );
  if (s.cost) chips.push(`<span class="chip">${esc(s.cost)}</span>`);
  if (s.mode && s.mode !== "normal" && s.mode !== "unknown")
    chips.push(`<span class="chip mode mode-${s.mode}">${MODE_LABEL[s.mode] ?? s.mode}</span>`);
  if (s.agents > 0) chips.push(`<span class="chip agents">⛓ ${s.agents} agents</span>`);
  row.innerHTML = chips.join("");
  return row;
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
};
function activeState() {
  return panesById[activePane] || { pane_id: activePane, label: "" };
}
function updateBar(s) {
  bar.input.placeholder = s ? `Type into ${s.label || "pane"}…` : "No pane";
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
      b.onclick = () => { activePane = s.pane_id; answer(s, keyFor(s.question, opt, i)); };
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

// "View screen" — drill down to the actual terminal. The old inline snapshot strip
// flickered (rebuilt every poll) and was too small to read. This is a button that
// opens ONE full-screen, scrollable (both axes) view of the pane's latest raw capture,
// fetched ON DEMAND — so nothing re-renders on the poll loop.
function timeline(s) {
  const btn = document.createElement("button");
  btn.className = "viewbtn";
  btn.textContent = "▤ View screen";
  btn.onclick = (e) => { e.stopPropagation(); openScreen(s.pane_id, s.label); };
  return btn;
}

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
    `<pre class="screen-body"></pre>`;
  ov.querySelector(".screen-body").textContent = text;  // textContent = safe, no esc needed
  const close = () => ov.remove();
  ov.querySelector(".screen-close").onclick = close;
  ov.onclick = (e) => { if (e.target === ov) close(); };
  document.body.appendChild(ov);
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
