// The Restore view (docs/design/reboot-restore.md): the workspace of the tmux server that
// died, grouped as tmux had it, sessions over windows over panes, each pane ticked by the
// daemon's default. Nothing comes back without the tap that names the count. `mount` is the
// list's row that opens the view; `card` is the view's body, which app.js places.
import { since } from "/m/pane-model.js";

const esc = s => String(s ?? "").replace(/[&<>"]/g, c => `&#${c.charCodeAt(0)};`);
const home = d => d.replace(/^\/home\/[^/]+/, "~");
const key = (wid, p) => `${wid}:${p.index}`;

export function setupRestore({ mount, request, slow, logos, licon, notice, running, open, opened, closed }) {
  let offer = null, picked = new Map();
  const card = document.createElement("div");
  card.className = "restore-card";
  mount.onclick = open;

  const panesOf = (s) => s.windows.flatMap(w => offer.windows[w.id].panes.map(p => [w.id, p]));
  const restorable = () => Object.entries(offer.windows).flatMap(([wid, w]) =>
    w.panes.filter(p => !p.blocked && picked.get(key(wid, p))).map(p => [wid, p]));

  const paneRow = (w, p) => `<label class="restore-pane${p.blocked ? " blocked" : ""}">
    <input type="checkbox" data-key="${esc(key(w, p))}"${picked.get(key(w, p)) && !p.blocked ? " checked" : ""}${p.blocked ? " disabled" : ""}>
    <img src="${esc(logos[p.tool] || logos.shell)}" alt="" width="16" height="16">
    <span><b>${esc(p.title || p.command || p.tool || "shell")}</b>
    <small>${esc(home(p.cwd))}${p.last_activity ? ` · idle ${esc(since({ state_since: p.last_activity }, offer.died_at * 1000))} at shutdown` : ""}
    ${p.cost ? ` · ${esc(p.cost)}` : ""}${p.blocked || p.note ? `<br>${esc(p.blocked || p.note)}` : ""}</small></span></label>`;

  function render() {
    mount.hidden = !offer;
    // A redraw keeps what the reader did: the box they ticked keeps focus, a folded session stays folded.
    const shut = new Set([...card.querySelectorAll(".restore-session:not([open])")].map(d => d.dataset.name));
    const was = card.contains(document.activeElement) ? document.activeElement.dataset : {};
    const again = was.key ? `[data-key="${CSS.escape(was.key)}"]` : was.session ? `[data-session="${CSS.escape(was.session)}"]` : null;
    // Its own screen, not a dashboard card's: a title, no Back (the sidebar and tab bar reach the dashboard).
    const head = '<div class="landing-head"><h2>Restore your workspace</h2></div>';
    if (!offer) { card.innerHTML = `${head}<p class="muted">Nothing to restore.</p>`; return; }
    const chosen = restorable(), agents = chosen.filter(([, p]) => p.binding).length;
    const sessions = offer.sessions.filter(s => panesOf(s).some(([w, p]) => !p.blocked && picked.get(key(w, p))));
    const ago = since({ state_since: offer.died_at }, Date.now());
    const what = `${agents ? `${agents} agent${agents === 1 ? "" : "s"}` : `${chosen.length} pane${chosen.length === 1 ? "" : "s"}`} in ${sessions.length} session${sessions.length === 1 ? "" : "s"}`;
    // The folded rail shows only the icon, so the name rides on the button itself.
    mount.innerHTML = `${licon("history", 18)}<span><strong>Restore your workspace</strong><small>${esc(what)}</small></span>${licon("chevron", 16)}`;
    mount.title = `Restore your workspace: ${what}`;
    mount.setAttribute("aria-label", mount.title);
    card.innerHTML = `${head}<p class="muted">tmux stopped ${esc(ago)} ago. Pick what comes back; agents resume into their conversations.</p>
      ${offer.sessions.map(s => {
        const rows = panesOf(s), open = rows.filter(([w, p]) => !p.blocked && picked.get(key(w, p))).length;
        const live = rows.filter(([, p]) => !p.blocked).length;
        return `<details class="restore-session" data-name="${esc(s.name)}"${shut.has(s.name) ? "" : " open"}>
          <summary><input type="checkbox" data-session="${esc(s.name)}" aria-label="All of ${esc(s.name)}"${open && open === live ? " checked" : ""}
            ${open && open < live ? ' data-mixed="1"' : ""}${live ? "" : " disabled"}> <b>${esc(s.name)}</b>
            <span class="muted">${open} of ${rows.length}</span></summary>
          ${s.windows.map(w => `<div class="restore-window">${esc(w.index)}: ${esc(offer.windows[w.id].name)}</div>
            ${offer.windows[w.id].panes.map(p => paneRow(w.id, p)).join("")}`).join("")}
        </details>`;
      }).join("")}
      <div class="restore-actions"><button type="button" class="primary" data-act="restore"${chosen.length ? "" : " disabled"}>Restore ${esc(what)}</button>
      <button type="button" data-act="dismiss">${running() ? "Keep current" : "Start fresh"}</button></div>`;
    card.querySelectorAll("[data-mixed]").forEach(b => { b.indeterminate = true; });
    if (again) card.querySelector(again)?.focus({ preventScroll: true });
  }

  card.addEventListener("click", async (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    const body = { server: offer.server, panes: restorable().map(([wid, p]) => [wid, p.index]) };
    card.querySelectorAll("button").forEach(b => { b.disabled = true; });
    try {
      await request(act === "restore" ? "/api/restore" : "/api/restore/dismiss",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
        act === "restore" ? slow : undefined); // the build answers when the layout is up, however long
      if (act === "restore") notice("Restoring: the layout is back, agents resume a few at a time.", 6000);
    } catch (err) {
      notice(err.detail || err.message, 6000);
    }
    refresh();
  });
  card.addEventListener("change", (e) => {
    const box = e.target, session = offer.sessions.find(s => s.name === box.dataset.session);
    // One state per pane, drawn wherever a shared window shows it.
    for (const [w, p] of session ? panesOf(session) : []) if (!p.blocked) picked.set(key(w, p), box.checked);
    if (box.dataset.key) picked.set(box.dataset.key, box.checked);
    render();
  });

  let asked = 0, asks = 0;
  async function refresh() {
    asked = Date.now();
    const mine = ++asks, was = offer?.server;
    let next = null; // no history database (503): no offer, and nothing to say about it
    try {
      next = (await request("/api/restore")).candidate;
    } catch (err) {
      if (mine === asks && err.status !== 503) return render(); // a dropped request: keep the offer, its buttons live again
    }
    if (mine !== asks) return; // a later ask answers instead: an older one would bring a spent offer back
    offer = next;
    const changed = offer?.server !== was;
    if (changed) picked = new Map(offer ? Object.entries(offer.windows).flatMap(([wid, w]) => w.panes.map(p => [key(wid, p), p.default])) : []);
    render();
    if (!offer) closed(); // a spent offer, or a link to one, shows the dashboard
    else if (changed && !running()) opened();
  }
  // Asked again now and then, so an offer spent elsewhere goes and a failed ask is retried.
  const due = () => Date.now() - asked > 60000;
  return { refresh, render, card, due };
}
