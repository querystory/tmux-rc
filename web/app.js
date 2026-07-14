// termiphone PWA. Polls /api/state, renders one card per pane, floats waiting
// panes to the top, and posts answers back. No framework, no build step.

const ICONS = { claude: "☁️", codex: "🔷", gemini: "💎", shell: "$", unknown: "•" };
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
  // Waiting first, then running, then idle; stable within group.
  const order = { waiting: 0, running: 1, idle: 2, unknown: 3 };
  states.sort((a, b) => (order[a.activity] ?? 9) - (order[b.activity] ?? 9));
  panesEl.replaceChildren(...states.map(card));
}

function card(s) {
  const el = document.createElement("div");
  el.className = "card" + (s.activity === "waiting" ? " waiting" : "");

  const row = document.createElement("div");
  row.className = "row";
  row.innerHTML = `
    <span class="icon">${ICONS[s.tool] ?? "•"}</span>
    <div class="meta">
      <div class="name">${esc(s.label)} <span style="color:#6e7681;font-weight:400">${esc(s.tool)}</span></div>
      <div class="status">${esc(s.status_line || "—")}</div>
    </div>
    <span class="badge b-${s.activity}">${s.activity === "idle" ? "idle " + fmtIdle(s.idle_seconds) : s.activity}</span>`;
  el.appendChild(row);

  if (s.context_pct != null) {
    const bar = document.createElement("div");
    bar.className = "ctx";
    bar.innerHTML = `<i style="width:${s.context_pct}%"></i>`;
    el.appendChild(bar);
  }

  if (s.question) el.appendChild(question(s));
  el.appendChild(timeline(s));
  return el;
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
    ft.innerHTML = `<input placeholder="Type a reply…" /><button>Send</button>`;
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
  busy = true;
  try {
    await fetch(`/api/panes/${encodeURIComponent(s.pane_id)}/send`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ keys, enter: true }),
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
