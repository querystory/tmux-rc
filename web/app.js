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
    const states = await r.json();
    liveEl.className = "dot";
    render(states);
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
  // Header: icon, name/summary, activity badge. Status_line leads (the summary of
  // what's being worked on); the working detail (verb·elapsed·tokens) is a subline.
  const working =
    s.activity === "running" && (s.elapsed || s.tokens)
      ? `<div class="sub">${[s.working_verb, s.elapsed, s.tokens && "↓" + s.tokens]
          .filter(Boolean).map(esc).join(" · ")}</div>`
      : "";
  row.innerHTML = `
    <span class="icon">${iconFor(s.tool)}</span>
    <div class="meta">
      <div class="name">${esc(s.label)}</div>
      <div class="status">${esc(s.status_line || "—")}</div>
      ${working}
    </div>
    <span class="badge b-${s.activity}">${badge}</span>`;
  el.appendChild(row);

  el.appendChild(metaRow(s));
  if (s.question) el.appendChild(question(s));
  el.appendChild(inputRow(s));
  el.appendChild(timeline(s));
  return el;
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
      <input data-pane="${esc(s.pane_id)}" placeholder="Type into ${esc(s.label)}…" />
      <button class="send">Send</button>
    </div>
    <div class="keys">
      <button data-k="Enter">⏎ Enter</button>
      <button data-k="Escape">Esc</button>
      <button data-k="Up">↑</button>
      <button data-k="Down">↓</button>
      <button data-k="C-c">Ctrl-C</button>
    </div>`;
  const input = wrap.querySelector("input");
  wrap.querySelector(".send").onclick = () => {
    if (input.value) { answer(s, input.value); input.value = ""; }
  };
  // Special keys are tmux key-names, sent literally (no appended Enter).
  wrap.querySelectorAll(".keys button").forEach((b) => {
    b.onclick = () => sendRaw(s, b.dataset.k);
  });
  return wrap;
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

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
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
