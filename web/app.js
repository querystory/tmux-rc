// tmux-rc PWA. Polls /api/state, renders one card per pane, floats waiting
// panes to the top, and posts answers back. No framework, no build step.

// Real brand marks per agent (served from web/). One img template so every logo-backed
// tool renders identically; emoji/text fallback for the rest. `tool` comes from parser
// JSON, so look it up with hasOwnProperty (a value like "toString"/"constructor" would
// otherwise resolve up the prototype chain and render garbage) and escape it into alt.
const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const LOGOS = { claude: "/claude.png", codex: "/openai.svg", gemini: "/gemini.svg" };
const ICONS = { shell: "$", unknown: "•" };
const img = (src, alt) => `<img src="${src}" width="22" height="22" alt="${escAttr(alt)}" style="border-radius:5px" />`;
const iconFor = (tool) => (has(LOGOS, tool) ? img(LOGOS[tool], tool) : (has(ICONS, tool) ? ICONS[tool] : "•"));
const panesEl = document.getElementById("panes");
const liveEl = document.getElementById("live");

// Track which pane's timeline is expanded so a re-render doesn't collapse it.
const openTimelines = new Set();
let busy = false; // suppress polling flicker while an answer is in flight

// The web surface is a dumb remote control for tmux — ALL state is in tmux. The active
// pane is whatever tmux reports as focused (state.tmux_active). Tapping a card just
// tells tmux to focus that pane; the next poll renders the new truth. No client-side
// selection state.
let panesById = {}; // latest state per pane, for the bottom bar to act on
function activeId() {
  const focused = Object.values(panesById).find((s) => s.tmux_active);
  return focused ? focused.pane_id : Object.keys(panesById)[0] || null;
}
function setActive(id) {
  fetch(`/api/panes/${encodeURIComponent(id)}/select`, { method: "POST" }).catch(() => {});
  // Immediately mark this pane as tmux_active so the highlight updates without
  // waiting for the next poll (which is 2s away — feels broken without this).
  Object.values(panesById).forEach((s) => { s.tmux_active = s.pane_id === id; });
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
  if (!states.length) {
    panesEl.innerHTML = '<div class="empty">No tmux pane found.<br>Start a session and it will appear here.</div>';
    updateBar(null);
    return;
  }
  // Waiting first, then running, then idle; stable within group.
  const order = { waiting: 0, running: 1, idle: 2, unknown: 3 };
  states.sort((a, b) => (order[a.activity] ?? 9) - (order[b.activity] ?? 9));
  panesEl.replaceChildren(...states.map(card));
  updateBar(panesById[activeId()]);
}

function card(s) {
  const el = document.createElement("div");
  el.className = "card" + (s.activity === "waiting" ? " waiting" : "")
    + (s.pane_id === activeId() ? " active" : "");
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
        ? '<span class="pulse"></span>running'
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
  el.appendChild(timeline(s));
  return el;
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
  // one chip each, no schema change per metric.
  for (const t of (s.status_entries || []).slice(0, 4))
    if (t) chips.push(`<span class="chip">${esc(t)}</span>`);
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
function pinchZoom(container, el) {
  let scale = 1, tx = 0, ty = 0;
  let start = null; // {dist, cx, cy} for pinch, or {x,y} for pan
  const apply = () => { el.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`; };
  el.style.transformOrigin = "0 0";
  // Start at the BOTTOM-left, not the top: the end of a capture is the live state
  // (everything above a trailing prompt has already exited).
  ty = Math.min(0, container.clientHeight - el.offsetHeight);
  apply();
  const dist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  const mid = (t) => ({ x: (t[0].clientX + t[1].clientX) / 2, y: (t[0].clientY + t[1].clientY) / 2 });
  container.addEventListener("touchstart", (e) => {
    if (e.touches.length === 2) { const m = mid(e.touches); start = { dist: dist(e.touches), s: scale, tx, ty, cx: m.x, cy: m.y }; }
    else if (e.touches.length === 1) start = { pan: true, x: e.touches[0].clientX - tx, y: e.touches[0].clientY - ty };
  }, { passive: false });
  container.addEventListener("touchmove", (e) => {
    if (!start) return;
    e.preventDefault();
    if (start.pan && e.touches.length === 1) {
      tx = e.touches[0].clientX - start.x; ty = e.touches[0].clientY - start.y;
    } else if (e.touches.length === 2) {
      const f = dist(e.touches) / start.dist;
      scale = Math.min(6, Math.max(0.4, start.s * f));
      // keep the pinch midpoint stationary
      tx = start.cx - (start.cx - start.tx) * (scale / start.s);
      ty = start.cy - (start.cy - start.ty) * (scale / start.s);
    }
    apply();
  }, { passive: false });
  container.addEventListener("touchend", (e) => { if (e.touches.length === 0) start = null; });
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
