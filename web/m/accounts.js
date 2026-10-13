// Agent accounts (openbus/accounts.py): one per provider config dir. Account UI draws only
// for a provider with two or more signed-in accounts; with one each, everything here is a
// no-op and the app looks as it always did. The one exception is Accounts… in the menu,
// since adding a second account has to start somewhere. See docs/design/agent-accounts.md.
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
const KEY = "tmuxrc-account", ENV = { claude: "CLAUDE_CONFIG_DIR", codex: "CODEX_HOME" };
const LOGIN = { claude: "claude", codex: "codex login" };
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "new";
const title = (provider) => provider === "codex" ? "Codex" : "Claude";

export function setupAccounts({ request, post, licon, notice, repaint, sessions, paneAlive, openPane }) {
  const $ = (id) => document.getElementById(id);
  let profiles = [], defaults = {}, shared = {}, filter = null;
  try { filter = localStorage.getItem(KEY); } catch {}
  const signedIn = () => profiles.filter((p) => p.signed_in);
  const several = (provider) => signedIn().filter((p) => p.provider === provider).length > 1;
  const any = () => Object.keys(ENV).some(several);
  const byDir = (dir) => profiles.find((p) => p.dir === dir);
  // The account worth naming for a pane: only when its provider has several.
  const ofPane = (id) => { const p = profiles.find((a) => a.panes.includes(id)); return p && several(p.provider) ? p : null; };
  const active = () => { const p = any() && byDir(filter); return p?.signed_in ? p : null; };
  const dot = (p) => `<i class="acct-dot" style="--a:var(--acct-${p.color})"></i>`;
  const badge = (p, inner = esc(p.name)) => `<span class="acct-badge" style="--a:var(--acct-${p.color})" title="${esc(`${p.name}: ${p.tilde}`)}">${dot(p)}${inner}</span>`;
  // The account a new window in this session starts on: the dir its tmux environment sets,
  // else (nothing sets it) the provider's standard dir; null if that is no signed-in account.
  const defaultOf = (session, provider) => {
    const dir = defaults[session]?.[provider], p = dir ? byDir(dir) : profiles.find((a) => a.provider === provider && a.default);
    return p?.signed_in ? p : null;
  };

  async function refresh() {
    try { ({ profiles, sessions: defaults, shared } = await request("/api/accounts")); } catch { return; }
    paintMenu(); repaint();
    if (dlg.open && F.step === "list" && !F.renaming) render();
  }
  function setFilter(dir) {
    filter = dir || null;
    try { if (filter) localStorage.setItem(KEY, filter); else localStorage.removeItem(KEY); } catch {}
    $("more-menu").open = false; paintMenu(); repaint();
  }

  // ---- the menu's Filter (several only), its dot, and the "Filtered: X" pill ----
  const menuFilter = document.createElement("div");
  menuFilter.id = "acct-filter"; menuFilter.setAttribute("role", "group"); menuFilter.ariaLabel = "Account filter";
  $("more-menu").querySelector(".header-picker-menu").prepend(menuFilter);
  const pill = document.createElement("div");
  pill.className = "acct-pill"; pill.setAttribute("role", "status");
  function paintMenu() {
    const on = active(), opts = [[null, "All accounts"], ...signedIn().map((p) => [p, p.name])];
    menuFilter.hidden = !any();
    menuFilter.innerHTML = any() ? '<div class="menu-sec">Filter</div>' + opts.map(([p, name]) =>
      `<button type="button" class="acct-opt" role="menuitemradio" data-dir="${esc(p?.dir || "")}" aria-checked="${(on || null) === p}">${p ? dot(p) : '<i class="acct-dot none"></i>'}<span>${esc(name)}</span>${licon("check", 16)}</button>`).join("") + "<hr>" : "";
    $("more-menu").classList.toggle("filtered", !!on);
    $("more-menu").style.setProperty("--a", on ? `var(--acct-${on.color})` : "");
    pill.hidden = !on;
    pill.innerHTML = on ? `Filtered: ${badge(on, `${esc(on.name)}<button type="button" aria-label="Clear the account filter" title="Clear">${licon("x", 14)}</button>`)}` : "";
  }
  menuFilter.onclick = (e) => { const b = e.target.closest("[data-dir]"); if (b) setFilter(b.dataset.dir); };
  pill.onclick = (e) => { if (e.target.closest("button")) setFilter(null); };

  // ---- usage: named by profile, filtered, and with several, one strip by urgency ----
  // A meter's account is the profile of its first listed dir; one no longer listed (removed)
  // drops out. Before the first answer the meters stand as they come.
  function usage(list) {
    if (!profiles.length) return list;
    const on = active();
    // A row's dirs can be several profiles of one login: the filtered one, if it is among them.
    return list.map((a) => { const mine = (a.dirs || []).map(byDir).filter((p) => p?.signed_in); return { ...a, acct: mine.includes(on) ? on : mine[0] }; })
      .filter((a) => a.acct && (!on || a.acct === on))
      .map((a) => ({ ...a, label: several(a.provider) ? a.acct.name : null }));
  }
  // Already full first, then full soonest, then the highest pace at the reset; unreadable last.
  const urgency = (w) => !w ? -1 : w.pct >= 100 ? Infinity : w.limit_at ? 1e15 - w.limit_at : w.projected ?? w.pct;
  const byUrgency = (list) => list.flatMap((a) => a.windows?.length ? a.windows.map((w) => ({ ...a, windows: [w] })) : [a])
    .sort((a, b) => urgency(b.windows?.[0]) - urgency(a.windows?.[0]));
  // One swipeable row: the edge fades only while there is more that way.
  function strip(el, on) {
    el.classList.toggle("acct-strip", on);
    const edge = () => {
      el.classList.toggle("more-r", on && el.scrollLeft + el.clientWidth < el.scrollWidth - 2);
      el.classList.toggle("more-l", on && el.scrollLeft > 2);
    };
    el.onscroll = edge; edge();
  }
  // The wide dashboard: one row of capped meters, each account's group labelled, details in
  // its tooltip; it wraps only when they don't fit.
  function groups(el, list, render) {
    el.className = "usage-groups";
    const markup = list.map((a, i) => {
      const p = a.acct, tip = [p.name, p.who, p.plan, p.tilde, `${p.panes.length} pane${p.panes.length === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
      return `<div class="ug" data-i="${i}"><div class="ug-label" title="${esc(tip)}">${several(p.provider) ? dot(p) : ""}<b>${esc(p.name)}</b></div><div class="ug-cells"></div></div>`;
    }).join("");
    if (el._groups !== markup) { el.innerHTML = markup; el._groups = markup; }
    el.querySelectorAll(".ug").forEach((g) => render(g.lastChild, [{ ...list[g.dataset.i], label: null }]));
  }

  // ---- New window: an account per provider with several, preset to the session's default ----
  const set = (session, provider) => !!defaults[session]?.[provider]; // its tmux env names a dir
  function hint(session, provider, def) {
    const name = def ? `<b style="color:var(--acct-${def.color})">${esc(def.name)}</b>` : "";
    if (!session) return def ? `A new session starts on ${name}.` : "A new session starts on none of these accounts.";
    if (!set(session, provider)) return `${esc(session)} has no account default; new windows use ${name || "the standard dir"}${def ? ` (${esc(def.tilde)}, the standard dir)` : ""}.`;
    return def ? `New windows in ${esc(session)} start on ${name} (set in its tmux session environment).` : `${esc(session)}'s default is none of these accounts.`;
  }
  function picker(choices, select) {
    const box = document.createElement("div");
    box.className = "acct-picker";
    const picks = {}, done = new AbortController();
    const paint = () => {
      const session = select.value;
      box.innerHTML = Object.keys(ENV).filter(several).map((provider) => {
        const def = defaultOf(session, provider), mine = signedIn().filter((p) => p.provider === provider);
        const pick = byDir(picks[provider]) || def || mine[0];
        picks[provider] = pick.dir;
        return `<label>${Object.keys(ENV).filter(several).length > 1 ? `${title(provider)} account` : "Account"}</label><div class="acct-pick" role="radiogroup">`
          + mine.map((p) => `<button type="button" role="radio" data-provider="${provider}" data-dir="${esc(p.dir)}" aria-checked="${p === pick}" style="--a:var(--acct-${p.color})">${dot(p)}<span><strong>${esc(p.name)}</strong><small>${esc([p.who !== p.name && p.who, p.tilde].filter(Boolean).join(" · "))}</small></span></button>`).join("")
          + `<button type="button" class="add" data-add>+ Add account</button></div>`
          + `<p class="acct-hint">${hint(session, provider, def)}</p>`
          // Always in view: the session's default, set with tmux set-environment on change.
          + (session ? `<label class="acct-default">Default for this session <select data-default-for="${provider}">`
            + (set(session, provider) ? "" : '<option value="" selected>Not set</option>')
            + mine.map((p) => `<option value="${esc(p.dir)}" ${set(session, provider) && p === def ? "selected" : ""}>${esc(p.name)}</option>`).join("") + "</select></label>" : "");
      }).join("");
      for (const b of choices.querySelectorAll("button[data-icon]")) {
        const p = several(b.dataset.icon) && byDir(picks[b.dataset.icon]), small = b.querySelector(".acct-on") || b.querySelector("span").appendChild(Object.assign(document.createElement("small"), { className: "acct-on" }));
        small.hidden = !p; small.innerHTML = p ? `on ${badge(p)} ${esc(p.tilde)}` : "";
      }
    };
    box.onclick = (e) => {
      const b = e.target.closest("button[data-dir]");
      if (b) { picks[b.dataset.provider] = b.dataset.dir; paint(); }
      if (e.target.closest("[data-add]")) { select.closest("dialog").close(); open("name"); }
    };
    box.onchange = async (e) => {
      const provider = e.target.dataset.defaultFor;
      if (!provider || !e.target.value) return;
      try { await post("/api/accounts/default", { session: select.value, dir: e.target.value }); } catch { notice("The session default could not be set.", 6000); }
      delete picks[provider]; await refresh(); paint();
    };
    select.addEventListener("change", () => { for (const k of Object.keys(picks)) delete picks[k]; paint(); }, { signal: done.signal });
    choices.before(box); paint();
    return {
      account: (icon) => several(icon) ? picks[icon] : undefined,
      remove: () => { done.abort(); box.remove(); },
    };
  }

  // ---- Accounts…: the list, rename, remove, and the add flow ----
  const dlg = document.createElement("dialog");
  dlg.id = "acct-dialog"; dlg.setAttribute("aria-labelledby", "acct-title");
  document.getElementById("app").append(dlg);
  let F = {}, poll = 0;
  const head = (text, step) => `<div class="sheet-handle"></div><div class="dialog-heading"><h2 id="acct-title">${esc(text)}</h2><button type="button" class="icon-button" data-x aria-label="Close" title="Close">${licon("x")}</button></div>`
    + (step ? `<div class="acct-steps" aria-label="Step ${step} of 4">${[1, 2, 3, 4].map((i) => `<i class="${i <= step ? "on" : ""}"></i>`).join("")}</div>` : "");
  const btns = (...b) => `<div class="acct-btns">${b.join("")}</div>`;
  const who = (p) => [esc(p.signed_in ? p.who || "signed in" : "not signed in"), esc(p.plan), `<code>${esc(p.tilde)}</code>`, `${p.panes.length} panes`].filter(Boolean).join(" · ");
  function render() {
    const s = F.step, owner = s === "name" && profiles.find((p) => p.dir === F.dir || p.tilde === F.dir);
    if (s === "list") dlg.innerHTML = head("Accounts") + profiles.map((p) => F.renaming === p.dir
      ? `<div class="acct-row">${dot(p)}<input id="acct-rename" value="${esc(p.label || p.name)}" aria-label="Account name"><button class="primary" data-rename-save>Save</button><button data-goto="list">Cancel</button></div>`
      : F.removing === p.dir
        ? `<div class="acct-row col"><b>Remove ${esc(p.name)} from tmux-rc?</b><p class="acct-note">It leaves the meters, badges and pickers. <code>${esc(p.tilde)}</code>, its login and its transcripts stay on disk, and the panes running on it keep running.</p>${btns('<button data-goto="list">Keep</button>', '<button class="primary" data-remove-ok>Remove from list</button>')}</div>`
        : `<div class="acct-row">${p.signed_in ? dot(p) : '<i class="acct-dot none"></i>'}<div class="who"><b>${esc(p.name)}</b> <span class="muted">${p.provider}</span><small>${who(p)}</small></div>`
          + (p.signed_in ? `<button data-rename="${esc(p.dir)}">Rename</button>` : `<button class="primary" data-finish="${esc(p.dir)}">Finish sign-in</button>`)
          + (p.default ? "" : `<button data-remove="${esc(p.dir)}">Remove</button>`) + "</div>").join("")
      + `<button class="acct-add" data-add>+ Add account</button><p class="acct-note">One account per config dir, found in <code>~/.claude</code>, <code>~/.claude-*</code>, <code>~/.codex*</code> and any dir a live pane uses. Names come from each dir's non-secret account info. tmux-rc never reads, copies or stores a login.</p>`;
    else if (s === "name") dlg.innerHTML = head("Add account", 1)
      + `<label for="acct-name">Name</label><input id="acct-name" value="${esc(F.name)}" autocomplete="off" placeholder="Personal">`
      + `<label>Provider</label><div class="acct-seg">${Object.keys(ENV).map((p) => `<button type="button" data-prov="${p}" aria-pressed="${F.provider === p}">${title(p)}</button>`).join("")}</div>`
      + `<label for="acct-dir">Config dir</label><input id="acct-dir" value="${esc(F.dir)}" autocomplete="off" autocapitalize="off" spellcheck="false">`
      + (owner && !owner.signed_in ? `<p class="acct-msg warn">A sign-in to <code>${esc(F.dir)}</code> (${esc(owner.name)}) was started and never finished. Next picks it up.</p>`
        : owner ? `<p class="acct-msg err"><code>${esc(F.dir)}</code> is already the account ${esc(owner.name)}. Pick another dir, or rename that account instead.</p>`
        : `<p class="acct-msg">tmux-rc creates <code>${esc(F.dir)}</code> (private, mode 700), or uses it as it is if it already exists. Nothing is written yet.</p>`)
      + `<p class="acct-note">The provider keeps this account's login, settings and transcripts in that dir (${ENV[F.provider]}), apart from your other accounts.</p>`
      + btns('<button class="ghost" data-goto="list">Back</button>', owner && !owner.signed_in ? `<button class="primary" data-resume="${esc(owner.dir)}">Next</button>`
        : `<button class="primary" data-goto="share" ${owner || !F.name.trim() || !F.dir ? "disabled" : ""}>Next</button>`);
    else if (s === "share") dlg.innerHTML = head("Share your setup? (optional)", 2)
      + `<p class="acct-note">tmux-rc can symlink these from <code>~/.${F.provider}</code> into <code>${esc(F.dir)}</code>, so there is one copy to edit and both accounts follow it. Only files that exist are linked.</p>`
      + Object.entries(shared[F.provider] || {}).map(([f, on]) => `<label class="acct-check"><input type="checkbox" data-link="${esc(f)}" ${on ? "checked" : ""}><span><code>${esc(f)}</code>${on ? "" : "<small>may carry work-only hooks, MCP servers or permissions; check it first</small>"}</span></label>`).join("")
      + `<label class="acct-check locked"><input type="checkbox" disabled><span>Never linked: <code>${F.provider === "claude" ? ".credentials.json, .claude.json, projects/" : "auth.json, sessions/"}</code><small>the login and the history stay with each account</small></span></label>`
      + btns('<button class="ghost" data-goto="name">Back</button>', "<button data-create>Skip</button>", '<button class="primary" data-create="link">Link and continue</button>');
    else if (s === "login") dlg.innerHTML = head(`Sign in to ${F.name}`, 3)
      + (F.existed ? `<p class="acct-msg warn"><code>${esc(F.dir)}</code> already existed; tmux-rc kept its files.</p>` : "")
      + (F.pane ? `<p class="acct-note">Window <b>sign in: ${esc(F.name)}</b> is open in <b>${esc(F.session)}</b>. Finish ${F.provider === "claude" ? "<code>/login</code>" : "the browser sign-in"} there. tmux-rc only watches for the account to appear; it never sees the login itself.</p><p class="acct-wait"><i class="acct-spin"></i>Waiting for ${esc(F.name)} to sign in…</p>`
          + btns('<button class="ghost" data-abandon>Cancel sign-in</button>', "<button data-go>Go to window</button>")
        : `<p class="acct-note">tmux-rc opens a new window that runs the provider's own sign-in on this dir:</p><code class="acct-cmd">${ENV[F.provider]}=${esc(F.dir)} ${LOGIN[F.provider]}</code>`
          + `<label for="acct-session">Open it in session</label><select id="acct-session">${sessions().map((x) => `<option ${x === F.session ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>`
          + `<p class="acct-note">You sign in there yourself, so your password and tokens never pass through tmux-rc.</p>`
          + btns('<button class="ghost" data-goto="list">Later</button>', `<button class="primary" data-login ${sessions().length ? "" : "disabled"}>Open sign-in window</button>`));
    else if (s === "abandoned") dlg.innerHTML = head("Sign-in not finished")
      + `<p>${esc(F.name)} is not signed in yet, so it is listed as "not signed in" until you finish or remove it.</p><p class="acct-note"><code>${esc(F.dir)}</code> holds no login; any links you chose are in place and harmless.</p>`
      + (F.pane && paneAlive(F.pane) ? `<label class="acct-check"><input type="checkbox" id="acct-close" checked><span>Close the sign-in window<small>the window tmux-rc opened for this, and nothing else</small></span></label>` : "")
      + btns('<button class="ghost" data-remove-ok>Remove</button>', "<button data-later>Later</button>", '<button class="primary" data-retry>Try again</button>');
    else if (s === "done") {
      const p = byDir(F.dir) || { name: F.name, color: 0, who: "", plan: "", tilde: F.dir };
      dlg.innerHTML = head(`${F.name} is ready`, 4)
        + `<div class="acct-row">${dot(p)}<div class="who"><b>${esc(p.name)}</b> <span class="muted">${F.provider}</span><small>${esc([p.who && `signed in as ${p.who}`, p.plan].filter(Boolean).join(" · "))} · <code>${esc(p.tilde)}</code></small></div></div>`
        + `<p class="acct-note">Read from the dir's non-secret account info. The sign-in window stays open as a normal ${title(F.provider)} pane on ${esc(F.name)}.</p>`
        + (F.session ? `<label class="acct-check"><input type="checkbox" id="acct-make-default" checked><span>Make ${esc(F.name)} the default for session <b>${esc(F.session)}</b><small>New windows there start on ${esc(F.name)}; running panes keep their account.</small></span></label>` : "")
        + btns('<button class="primary" data-done>Done</button>');
    }
    if (F.error) dlg.querySelector(".acct-btns")?.insertAdjacentHTML("beforebegin", `<p class="acct-msg err">${esc(F.error)}</p>`);
  }
  function open(step) {
    F = { step, name: "", provider: "claude", dir: "~/.claude-new", session: sessions()[0] || "" };
    render(); if (!dlg.open) dlg.showModal();
  }
  const go = (step, extra = {}) => { Object.assign(F, { renaming: null, removing: null, error: null, ...extra, step }); render(); };
  const fail = (error) => { F.error = error.detail || "That did not work. Try again."; render(); };
  const typed = (id) => { const at = dlg.querySelector(`#${id}`).selectionStart; render(); const i = dlg.querySelector(`#${id}`); i.focus(); i.setSelectionRange(at, at); };
  dlg.addEventListener("input", (e) => {
    if (e.target.id === "acct-name") { F.name = e.target.value; if (!F.dirTouched) F.dir = `~/.${F.provider}-${slug(F.name)}`; typed("acct-name"); }
    if (e.target.id === "acct-dir") { F.dir = e.target.value.trim(); F.dirTouched = true; typed("acct-dir"); }
  });
  dlg.addEventListener("change", (e) => { if (e.target.id === "acct-session") F.session = e.target.value; });
  dlg.addEventListener("close", () => { clearTimeout(poll); refresh(); });
  // Sign-in is detected, never driven: the dir's account info appearing, or its window gone.
  async function wait() {
    clearTimeout(poll);
    if (!dlg.open || F.step !== "login" || !F.pane) return;
    await refresh();
    if (byDir(F.dir)?.signed_in) return go("done");
    // Gone once seen, or never seen within a grace: the page's state can lag the new window.
    if (paneAlive(F.pane)) F.seen = true;
    else if (F.seen || Date.now() - F.openedAt > 15000) return go("abandoned");
    poll = setTimeout(wait, 2000);
  }
  dlg.addEventListener("click", async (e) => {
    const t = e.target.closest("button"), d = t?.dataset || {};
    if (!t) return;
    if ("x" in d) return dlg.close();
    if (d.goto) return go(d.goto);
    if (d.prov) { F.provider = d.prov; if (!F.dirTouched) F.dir = `~/.${F.provider}-${slug(F.name)}`; return render(); }
    if ("add" in d) return open("name");
    if (d.rename) return go("list", { renaming: d.rename });
    if (d.remove) return go("list", { removing: d.remove });
    if (d.resume) { const p = byDir(d.resume); return go("abandoned", { dir: p.dir, name: p.name, provider: p.provider, pane: null }); }
    if (d.finish) { const p = byDir(d.finish); return go("login", { dir: p.dir, name: p.name, provider: p.provider, pane: null }); }
    t.disabled = true;
    try {
      if ("renameSave" in d) { await request("/api/accounts", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dir: F.renaming, label: dlg.querySelector("#acct-rename").value }) }); await refresh(); return go("list"); }
      // Leaving an unfinished sign-in: close the window it opened, if asked (that pane only).
      if (F.step === "abandoned" && dlg.querySelector("#acct-close")?.checked) {
        await post("/api/accounts/login/close", { pane_id: F.pane, pid: F.pid }).catch(() => {});
        F.pane = null;
      }
      if ("removeOk" in d) { await request("/api/accounts", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dir: F.removing || F.dir, forget: true }) }); await refresh(); return go("list"); }
      if ("create" in d) {
        const link = d.create ? [...dlg.querySelectorAll("[data-link]:checked")].map((i) => i.dataset.link) : [];
        const added = await post("/api/accounts", { provider: F.provider, dir: F.dir, label: F.name, link });
        await refresh();
        return go("login", { dir: added.dir || F.dir, existed: added.existed, pane: null });
      }
      if ("login" in d) { ({ pane_id: F.pane, pid: F.pid } = await post("/api/accounts/login", { dir: F.dir, session: F.session })); F.seen = false; F.openedAt = Date.now(); render(); return wait(); }
      if ("retry" in d) return go("login", { pane: null });
      if ("later" in d) return go("list");
      if ("abandon" in d) { clearTimeout(poll); return go("abandoned"); }
      if ("go" in d) { dlg.close(); return openPane(F.pane); }
      if ("done" in d) {
        if (dlg.querySelector("#acct-make-default")?.checked) await post("/api/accounts/default", { session: F.session, dir: F.dir });
        dlg.close(); return notice(`${F.name} added.`, 4000);
      }
    } catch (error) { fail(error); } finally { t.disabled = false; }
  });
  $("accounts-open").onclick = () => { $("more-menu").open = false; open("list"); };
  pill.hidden = true;

  return {
    refresh, usage, byUrgency, strip, groups, picker, pill, any,
    matches: (pane) => { const on = active(); return !on || profiles.find((p) => p.panes.includes(pane.pane_id)) === on; },
    badge: (paneId) => { const p = ofPane(paneId); return p ? badge(p) : ""; },
    // The consent card: which account a chat action lands on.
    consent: (paneId) => { const p = ofPane(paneId); return p ? `<small class="voice-meta acct-on">on ${badge(p)} ${esc(p.tilde)}. Sending never changes a pane's account.</small>` : ""; },
    profile: byDir, list: () => profiles, chip: (p) => badge(p),
    // Where "Move to another account" can take a pane: its provider's other signed-in
    // accounts, once it has several.
    moveTargets: (paneId, provider) => several(provider) ? signedIn().filter((p) => p.provider === provider && !p.panes.includes(paneId)) : [],
  };
}
