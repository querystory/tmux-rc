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
    render(data.panes || []);
  } catch {
    liveEl.className = "dot off";
  }
}

function render(states) {
  if (!states.length) {
    panesEl.innerHTML = '<div class="empty">No tmux pane found.<br>Start a session and it will appear here.</div>';
    return;
  }
  // Preserve the focused text input across the re-render (re-rendering replaces all
  // cards, which would otherwise wipe what the user is typing). Remember which pane's
  // input had focus, its value, and caret, then restore after rebuilding.
  const active = document.activeElement;
  const saved =
    active && active.tagName === "INPUT" && active.dataset.pane
      ? { pane: active.dataset.pane, value: active.value, start: active.selectionStart, end: active.selectionEnd }
      : null;

  // Waiting first, then running, then idle; stable within group.
  const order = { waiting: 0, running: 1, idle: 2, unknown: 3 };
  states.sort((a, b) => (order[a.activity] ?? 9) - (order[b.activity] ?? 9));
  panesEl.replaceChildren(...states.map(card));

  if (saved) {
    const input = panesEl.querySelector(`input[data-pane="${CSS.escape(saved.pane)}"]`);
    if (input) {
      input.value = saved.value;
      input.focus();
      try { input.setSelectionRange(saved.start, saved.end); } catch {}
    }
  }
}

function card(s) {
  const el = document.createElement("div");
  el.className = "card" + (s.activity === "waiting" ? " waiting" : "");

  const row = document.createElement("div");
  row.className = "row";
  const badge =
    s.activity === "idle"
      ? "idle " + fmtIdle(s.idle_seconds)
      : s.activity === "running"
        ? '<span class="pulse"></span>working'
        : s.activity;
  // Header: icon, name + headline (the task summary), activity badge. The working
  // detail (verb·elapsed·↓tokens) is a subline. Fields come straight from the parser
  // JSON (headline, nested working.*), so the UI renders whatever the model provides.
  const w = s.working || {};
  const working =
    s.activity === "running" && (w.elapsed || w.tokens)
      ? `<div class="sub">${[w.verb, w.elapsed, w.tokens && "↓" + w.tokens]
          .filter(Boolean).map(esc).join(" · ")}</div>`
      : "";
  row.innerHTML = `
    <span class="icon">${iconFor(s.tool)}</span>
    <div class="meta">
      <div class="name">${esc(s.label || "")}</div>
      <div class="status">${esc(s.headline || "—")}</div>
      ${working}
    </div>
    <span class="badge b-${s.activity}">${badge}</span>`;
  el.appendChild(row);

  el.appendChild(metaRow(s));
  if (s.rewind) el.appendChild(rewindView(s));
  if (s.question) el.appendChild(question(s));
  if (Array.isArray(s.tasks) && s.tasks.length) el.appendChild(tasksView(s.tasks));
  if (Array.isArray(s.notable) && s.notable.length) el.appendChild(notableView(s.notable));
  el.appendChild(inputRow(s));
  el.appendChild(timeline(s));
  return el;
}

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

// "What's going on" bullets — from parser JSON notable[]. The activity narrative in
// miniature until the full scrollback-summary feature lands.
function notableView(items) {
  const box = document.createElement("div");
  box.className = "notable";
  box.innerHTML = items.map((n) => `<div class="note-item">${esc(n)}</div>`).join("");
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
function inputRow(s) {
  const wrap = document.createElement("div");
  wrap.className = "raw";
  wrap.innerHTML = `
    <div class="freetext">
      <button class="attach" title="Attach image">📎</button>
      <button class="paste" title="Paste image from clipboard">📋</button>
      <input data-pane="${esc(s.pane_id)}" placeholder="Type into ${esc(s.label)}…" />
      <button class="send">Send</button>
      <input type="file" accept="image/*" hidden />
    </div>
    <div class="keys">
      <button data-k="Enter">⏎ Enter</button>
      <button data-k="Escape">Esc</button>
      <button data-k="Up">↑</button>
      <button data-k="Down">↓</button>
      <button data-k="C-o">Ctrl-O</button>
      <button data-k="C-b">Ctrl-B</button>
      <button data-k="C-c">Ctrl-C</button>
    </div>`;
  const input = wrap.querySelector('input[data-pane]');
  const filePicker = wrap.querySelector('input[type=file]');
  wrap.querySelector(".send").onclick = () => {
    if (input.value) { answer(s, input.value); input.value = ""; }
  };
  // Attach: open the file/camera picker (the reliable mobile path — Photo Library
  // shows recent screenshots). Upload the chosen image.
  wrap.querySelector(".attach").onclick = () => filePicker.click();
  filePicker.onchange = () => filePicker.files[0] && uploadImage(s, filePicker.files[0], input);
  // Paste button: read the clipboard via the async Clipboard API (works on mobile on
  // a tap, unlike input.onpaste). Requires a secure context (HTTPS or localhost);
  // over plain-HTTP LAN the API is unavailable and we say so instead of failing silently.
  wrap.querySelector(".paste").onclick = () => pasteImage(s, input);
  // Also keep desktop input-paste working.
  input.onpaste = (e) => {
    const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
    if (item) { e.preventDefault(); uploadImage(s, item.getAsFile(), input); }
  };
  // Special keys are tmux key-names, sent literally (no appended Enter).
  wrap.querySelectorAll(".keys button").forEach((b) => {
    b.onclick = () => sendRaw(s, b.dataset.k);
  });
  return wrap;
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

  if (s.question.options.length) {
    const opts = document.createElement("div");
    opts.className = "opts";
    s.question.options.forEach((opt, i) => {
      const b = document.createElement("button");
      b.className = "opt";
      b.textContent = opt;
      // For numbered menus we send the index+1; for y/n send first letter.
      b.onclick = () => answer(s, keyFor(s.question, opt, i));
      opts.appendChild(b);
    });
    q.appendChild(opts);
  } else {
    const ft = document.createElement("div");
    ft.className = "freetext";
    ft.innerHTML = `<input data-pane="${esc(s.pane_id)}:q" placeholder="Type a reply…" /><button>Send</button>`;
    const input = ft.querySelector("input");
    ft.querySelector("button").onclick = () => input.value && answer(s, input.value);
    q.appendChild(ft);
  }
  return q;
}

// Decide what keystroke represents the chosen option. y/n prompts want a letter;
// numbered menus want the number; otherwise send the literal option text.
function keyFor(question, opt, i) {
  const lc = opt.toLowerCase();
  if (question.options.length === 2 && (lc === "yes" || lc === "no")) return lc[0];
  if (question.options.length > 2) return String(i + 1);
  return opt;
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

function timeline(s) {
  const d = document.createElement("details");
  d.className = "timeline";
  if (openTimelines.has(s.pane_id)) d.open = true;
  d.ontoggle = () => d.open ? openTimelines.add(s.pane_id) : openTimelines.delete(s.pane_id);
  const sum = document.createElement("summary");
  sum.textContent = "Timeline";
  d.appendChild(sum);
  const strip = document.createElement("div");
  strip.className = "snaps";
  d.appendChild(strip);
  d.addEventListener("toggle", () => { if (d.open) loadSnaps(s.pane_id, strip); }, { once: false });
  if (d.open) loadSnaps(s.pane_id, strip);
  return d;
}

async function loadSnaps(paneId, strip) {
  const r = await fetch(`/api/panes/${encodeURIComponent(paneId)}/snapshots`);
  const list = await r.json();
  strip.replaceChildren(...await Promise.all(
    list.slice(-12).reverse().map(async (snap) => {
      const t = await (await fetch(`/api/panes/${encodeURIComponent(paneId)}/snapshots/${snap.id}`)).text();
      const div = document.createElement("div");
      div.className = "snap";
      div.textContent = t.split("\n").slice(-14).join("\n");
      return div;
    })
  ));
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
