// The Restore card (docs/design/reboot-restore.md): the workspace of the tmux server that
// died, grouped as tmux had it, sessions over windows over panes, each pane ticked by the
// daemon's default. Nothing comes back without the tap that names the count.
import { since } from "/m/pane-model.js";

const esc = s => String(s ?? "").replace(/[&<>"]/g, c => `&#${c.charCodeAt(0)};`);
const home = d => d.replace(/^\/home\/[^/]+/, "~");
const key = (wid, p) => `${wid}:${p.index}`;

export function setupRestore({ mount, request, logos, notice, running }) {
  let offer = null, picked = new Map();

  const panesOf = (s) => s.windows.flatMap(w => offer.windows[w.id].panes.map(p => [w.id, p]));
  const restorable = () => Object.entries(offer.windows).flatMap(([wid, w]) =>
    w.panes.filter(p => !p.blocked && picked.get(key(wid, p))).map(p => [wid, p]));

  function render() {
    mount.hidden = !offer;
    if (!offer) return;
    const chosen = restorable(), agents = chosen.filter(([, p]) => p.binding).length;
    const sessions = offer.sessions.filter(s => panesOf(s).some(([w, p]) => !p.blocked && picked.get(key(w, p))));
    const ago = since({ state_since: offer.died_at }, Date.now());
    mount.innerHTML = `<h3>Restore your workspace</h3>
      <p class="muted">tmux stopped ${esc(ago)} ago. Pick what comes back; agents resume into their conversations.</p>
      ${offer.sessions.map(s => {
        const rows = panesOf(s), open = rows.filter(([w, p]) => !p.blocked && picked.get(key(w, p))).length;
        const live = rows.filter(([, p]) => !p.blocked).length;
        return `<details class="restore-session"${matchMedia("(min-width: 900px)").matches ? " open" : ""}>
          <summary><input type="checkbox" data-session="${esc(s.name)}"${open && open === live ? " checked" : ""}
            ${open && open < live ? ' data-mixed="1"' : ""}${live ? "" : " disabled"}> <b>${esc(s.name)}</b>
            <span class="muted">${open} of ${rows.length}</span></summary>
          ${rows.map(([w, p]) => `<label class="restore-pane${p.blocked ? " blocked" : ""}">
            <input type="checkbox" data-key="${esc(key(w, p))}"${picked.get(key(w, p)) && !p.blocked ? " checked" : ""}${p.blocked ? " disabled" : ""}>
            <img src="${esc(logos[p.tool] || logos.shell)}" alt="" width="16" height="16">
            <span><b>${esc(p.title || p.command || p.tool || "shell")}</b>
            <small>${esc(home(p.cwd))}${p.last_activity ? ` · idle ${esc(since({ state_since: p.last_activity }, offer.died_at * 1000))} at shutdown` : ""}
            ${p.cost ? ` · ${esc(p.cost)}` : ""}${p.blocked || p.note ? `<br>${esc(p.blocked || p.note)}` : ""}</small></span></label>`).join("")}
        </details>`;
      }).join("")}
      <div class="restore-actions"><button type="button" class="primary" data-act="restore"${chosen.length ? "" : " disabled"}>
        Restore ${agents ? `${agents} agent${agents === 1 ? "" : "s"}` : `${chosen.length} pane${chosen.length === 1 ? "" : "s"}`} in ${sessions.length} session${sessions.length === 1 ? "" : "s"}</button>
      <button type="button" data-act="dismiss">${running() ? "Keep current" : "Start fresh"}</button></div>`;
    mount.querySelectorAll("[data-mixed]").forEach(b => { b.indeterminate = true; });
  }

  mount.addEventListener("click", async (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    const body = { server: offer.server, panes: restorable().map(([wid, p]) => [wid, p.index]) };
    mount.querySelectorAll("button").forEach(b => { b.disabled = true; });
    try {
      await request(act === "restore" ? "/api/restore" : "/api/restore/dismiss",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (act === "restore") notice("Restoring: the layout is back, agents resume a few at a time.", 6000);
    } catch (err) {
      notice(err.detail || err.message, 6000);
    }
    refresh();
  });
  mount.addEventListener("change", (e) => {
    const box = e.target, session = offer.sessions.find(s => s.name === box.dataset.session);
    // One state per pane, drawn wherever a shared window shows it.
    for (const [w, p] of session ? panesOf(session) : []) if (!p.blocked) picked.set(key(w, p), box.checked);
    if (box.dataset.key) picked.set(box.dataset.key, box.checked);
    render();
  });

  async function refresh() {
    try {
      const next = (await request("/api/restore")).candidate;
      if (next?.server !== offer?.server) {
        picked = new Map(next ? Object.entries(next.windows).flatMap(([wid, w]) => w.panes.map(p => [key(wid, p), p.default])) : []);
      }
      offer = next;
    } catch {
      offer = null; // no history database: no offer, and nothing to say about it
    }
    render();
  }
  return { refresh, render };
}
