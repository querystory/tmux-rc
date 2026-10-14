// Agent accounts (openbus/accounts.py): one per provider config dir. Account UI draws only
// for a provider with two or more signed-in accounts; with one each, everything here is a
// no-op and the app looks as it always did. The one exception is Accounts… in the menu,
// since adding a second account has to start somewhere. See docs/design/agent-accounts.md.
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
const ENV = { claude: "CLAUDE_CONFIG_DIR", codex: "CODEX_HOME" };
const LOGIN = { claude: "claude", codex: "codex login" };
// Codex's two ways in from afar: its browser sign-in, whose localhost redirect the sheet
// takes back by paste (server: /api/accounts/login/callback), or a device code.
const CODEX_WAYS = [["codex login", "Browser sign-in", "sign in, then paste the localhost address you land on"],
  ["codex login --device-auth", "Device code", "if your workspace allows it"]];
// The sign-in command as last edited (what follows VAR=<dir>), per provider and per browser;
// the server still pins the dir and the binary. Kept here too, where storage is blocked.
const cmds = {}, cmdKey = (p) => `tmuxrc-signin-${p}`;
const cmdOf = (p) => cmds[p] ??= (() => { try { return localStorage.getItem(cmdKey(p)); } catch { return null; } })() || LOGIN[p];
const LEAD = /^\s*(?:[A-Za-z_]\w*=(?:'[^']*'|"(?:[^"\\]|\\.)*"|\\.|[^\s'"\\])*\s+)*/; // the leading VAR=value words
// A dir as one shell word, for the line (shlex.split reads it back whole).
const shq = (s) => /^[\w@%+=:,./~-]+$/.test(s) ? s : `'${s.replace(/'/g, `'"'"'`)}'`;
// Where a browser `codex login` is finished from afar: the sheet's waiting step and the
// sign-in pane's own card both offer it (server: /api/accounts/login/callback).
const pasteForm = (id, value = "", busy = false) => `<label for="${id}">Paste the address your browser ended up on</label><div class="acct-paste"><input id="${id}" value="${esc(value)}" placeholder="http://127.0.0.1:1455/auth/callback?code=…" autocomplete="off" autocapitalize="off" spellcheck="false"><button type="button" data-callback ${value.trim() && !busy ? "" : "disabled"}>Finish</button></div>`;
// The line in words as the server's shlex.split reads it: quotes and backslashes, and null
// for one left open, so the sheet refuses exactly what the server would.
export const shellWords = (s) => {
  const out = []; let word = null, q = "";
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (q === "'") { if (c === "'") q = ""; else word += c; continue; }
    if (c === "\\" && (!q || '"\\'.includes(s[i + 1]))) { if (i + 1 >= s.length) return null; word = (word ?? "") + s[++i]; continue; }
    if (q === '"') { if (c === '"') q = ""; else word += c; continue; }
    if (/\s/.test(c)) { if (word !== null) out.push(word); word = null; continue; }
    word ??= "";
    if (c === "'" || c === '"') q = c; else word += c;
  }
  if (q) return null;
  if (word !== null) out.push(word);
  return out;
};
const CALLBACK_MS = 40000; // past the server's two 15 s local GETs: a slow exchange still lands
const setCmd = (p, v) => { cmds[p] = v; try { v === LOGIN[p] ? localStorage.removeItem(cmdKey(p)) : localStorage.setItem(cmdKey(p), v); } catch {} };
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "new";
// A dir in one spelling, as the server folds it (os.path.normpath): no "//", ".", ".." or trailing "/".
const norm = (d) => (d.startsWith("/") ? "/" : "") + d.split("/").reduce((o, s) => (s === ".." ? o.pop() : s && s !== "." && o.push(s), o), []).join("/");
const title = (provider) => provider === "codex" ? "Codex" : "Claude";

export function setupAccounts({ request, post, licon, notice, repaint, navigate, sessions, paneAlive, openPane, query, clearQuery }) {
  const $ = (id) => document.getElementById(id);
  // The filter is the URL's `acct` (url-state.js): app.js routes it here through follow().
  let profiles = [], defaults = {}, shared = {}, filter = null;
  const signedIn = () => profiles.filter((p) => p.signed_in);
  const several = (provider) => signedIn().filter((p) => p.provider === provider).length > 1;
  const any = () => Object.keys(ENV).some(several);
  const byDir = (dir) => profiles.find((p) => p.dir === dir);
  // The account worth naming for a pane: only when its provider has several.
  const ofPane = (id) => { const p = profiles.find((a) => a.panes.includes(id)); return p && several(p.provider) ? p : null; };
  // The URL's name for an account: its dir, which is unique and outlives a rename, with the
  // usual "~/." dropped so it reads (acct=claude-work). Every other dir keeps its leading
  // "~" or "/", so no two dirs share a name.
  const key = (p) => p.tilde.replace(/^~\/\./, "");
  const active = () => any() && signedIn().find((p) => key(p) === filter) || null;
  // The login's email, where the name and organisation leave it unsaid.
  const login = (p) => [p.who, p.name].some((s) => s?.includes(p.login)) ? "" : p.login;
  // The line under a name in a pick list: what the name leaves unsaid, then the dir.
  const sub = (p) => [p.who !== p.name && p.who, login(p), p.tilde].filter(Boolean).join(" · ");
  // Two dirs on one login (a /login in the wrong dir) look like two accounts: say so.
  const same = (p) => p.same_as?.length ? `<p class="acct-msg warn">Signed in as the same account as ${esc(p.same_as.map((d) => byDir(d)?.name).join(", "))}: sign this folder in again with <code>/login</code></p>` : "";
  const dot = (p) => `<i class="acct-dot" style="--a:var(--acct-${p.color})"></i>`;
  const badge = (p, after = "") => `<span class="acct-badge" style="--a:var(--acct-${p.color})" title="${esc(`${p.name}: ${p.tilde}`)}">${dot(p)}<span>${esc(p.name)}</span>${after}</span>`;
  // The account a new window in this session starts on: the dir its tmux environment sets,
  // else (nothing sets it) the provider's standard dir; null if that is no signed-in account.
  const defaultOf = (session, provider) => {
    const dir = defaults[session]?.[provider], p = dir ? byDir(dir) : profiles.find((a) => a.provider === provider && a.default);
    return p?.signed_in ? p : null;
  };

  // Only the newest answer counts: an older one landing late would undo a change. A caller
  // whose request was overtaken gets the newer one's outcome: true once the data is fresh.
  let asked = 0, newest;
  const refresh = () => newest = load(++asked);
  async function load(mine) {
    const data = await request("/api/accounts").catch(() => null);
    if (mine !== asked) return newest; // whether this one failed or not
    if (!data) return false;
    ({ profiles, sessions: defaults, shared } = data);
    paintFilter(); repaint();
    if (dlg.open && F.step === "list" && !F.renaming) render();
    return true;
  }
  function setFilter(k) {
    const menu = filterGroup.closest("details"), focused = [filterGroup, pill].some((el) => el.contains(document.activeElement));
    if (menu) menu.open = false;
    navigate(k || null); // writes the URL, which routes back to follow()
    // The button pressed was just redrawn or hidden: keep keyboard focus on the menu's trigger,
    // else the chip, else (clearing the pill on a phone) the dashboard's tab or a pane's Back.
    if (focused) [menu?.querySelector("summary"), filterGroup.querySelector('[aria-pressed="true"]'), document.querySelector('#list-nav [aria-pressed="true"]'), $("back")]
      .find((el) => el?.getClientRects().length)?.focus();
  }

  // ---- the filter: "Panes from" in the wide list's filter dropdown, chips under a phone's
  // search (app.js places it); its pick's dot on the dropdown's label, and the "Filtered: X" pill,
  // which also names the search box's text (query: app.js, a wide list's only).
  const filterGroup = document.createElement("div");
  filterGroup.id = "acct-filter"; filterGroup.setAttribute("role", "group"); filterGroup.ariaLabel = "Panes from";
  const tag = document.createElement("i");
  tag.className = "acct-dot";
  const pill = document.createElement("div");
  pill.className = "acct-pill"; // no live region: it would read the whole line out on every keystroke in search
  filterGroup.hidden = tag.hidden = pill.hidden = true; // until the accounts arrive
  // Redrawn only when changed: the refresh every 20s would otherwise drop a focused button's focus.
  const draw = (el, markup) => { if (el._markup !== markup) el.innerHTML = el._markup = markup; };
  function paintFilter() {
    const on = active(), opts = [[null], ...signedIn().map((p) => [p, p.name])];
    filterGroup.hidden = !any();
    draw(filterGroup, any() ? '<hr><div class="menu-sec">Panes from</div>' + opts.map(([p, name]) =>
      `<button type="button" class="acct-opt" data-acct="${esc(p ? key(p) : "")}" aria-pressed="${(on || null) === p}">${p ? dot(p) : '<i class="acct-dot none"></i>'}<span>${p ? esc(name) : 'All<span class="acct-all"> accounts</span>'}</span></button>`).join("") : "");
    filterGroup.onscroll?.(); // a phone's chips: their edge fades (strip)
    const q = query(), clear = (what) => `<button type="button" aria-label="Clear the ${what}" title="Clear">${licon("x", 14)}</button>`;
    tag.hidden = !on; pill.hidden = !on && !q;
    tag.title = on ? `Panes from ${on.name}` : "";
    tag.style.setProperty("--a", on ? `var(--acct-${on.color})` : "");
    draw(pill, on || q ? `Filtered: ${on ? badge(on, clear("account filter")) : ""}${q ? `<span class="acct-badge" data-q><span>${esc(`"${q}"`)}</span>${clear("search")}</span>` : ""}` : "");
  }
  filterGroup.onclick = (e) => { const b = e.target.closest("[data-acct]"); if (b) setFilter(b.dataset.acct); };
  pill.onclick = (e) => { const b = e.target.closest("button"); if (b) b.closest("[data-q]") ? clearQuery() : setFilter(null); };

  // ---- usage: named by profile, filtered, and with several, one strip by urgency ----
  // A meter's account is the profile of its first listed dir; one no longer listed (removed)
  // drops out. Before the first answer the meters stand as they come.
  function usage(list) {
    if (!profiles.length) return list;
    const on = active();
    // A row's dirs can be several profiles of one login: the filtered one, if it is among them.
    return list.map((a) => { const mine = (a.dirs || []).map(byDir).filter((p) => p?.signed_in); return { ...a, acct: mine.includes(on) ? on : mine[0] }; })
      .filter((a) => a.acct && (!on || a.acct === on))
      .map((a) => several(a.provider) ? { ...a, label: a.acct.name, chip: badge(a.acct) } : { ...a, label: null });
  }
  // Already full first, then full soonest, then the highest pace at the reset; unreadable last.
  const urgency = (w) => !w ? -1 : w.pct >= 100 ? Infinity : w.limit_at ? 1e15 - w.limit_at : w.projected ?? w.pct;
  const byUrgency = (list) => list.flatMap((a) => a.windows?.length ? a.windows.map((w) => ({ ...a, windows: [w] })) : [a])
    .sort((a, b) => urgency(b.windows?.[0]) - urgency(a.windows?.[0]));
  // One swipeable row: the edge fades only while there is more that way, rechecked on a
  // resize too (a wider strip fits more meters: style.css).
  function strip(el, on) {
    el.classList.toggle("acct-strip", on);
    el.onscroll ||= () => { // reads the class: the board's groups() may since have dropped it
      const on = el.classList.contains("acct-strip");
      el.classList.toggle("more-r", on && el.scrollLeft + el.clientWidth < el.scrollWidth - 2);
      el.classList.toggle("more-l", on && el.scrollLeft > 2);
    };
    el.onscroll();
    (el._resize ||= new ResizeObserver(() => el.onscroll())).observe(el);
  }
  // The wide dashboard and usage view: each account's meters as a group under its chip,
  // details in its tooltip. The chip names the account once over its meters, so they drop it.
  function groups(el, list, render) {
    el.className = "usage-groups"; el.hidden = !list.length; // a board drawn before the accounts came may have hidden it
    const markup = list.map((a, i) => {
      const p = a.acct, tip = [p.name, p.who, p.plan, p.tilde, `${p.panes.length} pane${p.panes.length === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
      return `<div class="ug" data-i="${i}" style="--n:${a.windows?.length || 1}"><div class="ug-label" title="${esc(tip)}">${badge(p)}</div><div class="ug-cells"></div></div>`;
    }).join("");
    if (el._groups !== markup) { el.innerHTML = markup; el._groups = markup; }
    el.querySelectorAll(".ug").forEach((g) => render(g.lastChild, [{ ...list[g.dataset.i], label: null, chip: null }]));
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
        return `<label>${Object.keys(ENV).filter(several).length > 1 ? `${title(provider)} account` : "Account"}</label><div class="acct-pick">`
          + mine.map((p) => `<button type="button" data-provider="${provider}" data-dir="${esc(p.dir)}" aria-pressed="${p === pick}" style="--a:var(--acct-${p.color})">${dot(p)}<span><strong>${esc(p.name)}</strong><small>${esc(sub(p))}</small></span></button>`).join("")
          + `<button type="button" class="add" data-add>+ Add account</button></div>`
          + `<p class="acct-hint">${hint(session, provider, def)}</p>`
          // Always in view: the session's default, set with tmux set-environment on change.
          + (session ? `<label class="acct-default">Default for this session <select data-default-for="${provider}">`
            + `<option value="" ${set(session, provider) ? "" : "selected"}>Not set</option>`
            // Set to a dir that is none of these: shown as it is, not as "Not set".
            + (set(session, provider) && !def ? `<option selected disabled>${esc(defaults[session][provider])}</option>` : "")
            + mine.map((p) => `<option value="${esc(p.dir)}" ${set(session, provider) && p === def ? "selected" : ""}>${esc(p.name)}</option>`).join("") + "</select></label>" : "");
      }).join("");
      for (const b of choices.querySelectorAll("button[data-provider]")) {
        const p = several(b.dataset.provider) && byDir(picks[b.dataset.provider]), small = b.querySelector(".acct-on") || b.querySelector("span").appendChild(Object.assign(document.createElement("small"), { className: "acct-on" }));
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
      if (!provider) return;
      try { await post("/api/accounts/default", { session: select.value, dir: e.target.value, provider }); } catch { notice("The session default could not be set.", 6000); }
      delete picks[provider]; await refresh(); paint();
    };
    select.addEventListener("change", () => { for (const k of Object.keys(picks)) delete picks[k]; paint(); }, { signal: done.signal });
    choices.before(box); paint();
    return {
      account: (provider) => several(provider) ? picks[provider] : undefined,
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
  const who = (p) => [esc(p.signed_in ? p.who || "signed in" : "not signed in"), esc(login(p)), esc(p.plan), `<code>${esc(p.tilde)}</code>`, `${p.panes.length} pane${p.panes.length === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
  function render() {
    const s = F.step, owner = s === "name" && profiles.find((p) => [p.dir, p.tilde].includes(norm(F.dir)));
    if (s === "list") dlg.innerHTML = head("Accounts") + profiles.map((p) => F.renaming === p.dir
      ? `<div class="acct-row">${dot(p)}<input id="acct-rename" value="${esc(p.label || p.name)}" aria-label="Account name"><button class="primary" data-rename-save>Save</button><button data-goto="list">Cancel</button></div>`
      : F.removing === p.dir
        ? `<div class="acct-row col"><b>Remove ${esc(p.name)} from tmux-rc?</b><p class="acct-note">It leaves the meters, badges and pickers. <code>${esc(p.tilde)}</code>, its login and its transcripts stay on disk, and the panes running on it keep running.</p>${btns('<button data-goto="list">Keep</button>', '<button class="primary" data-remove-ok>Remove from list</button>')}</div>`
        : `<div class="acct-row">${p.signed_in ? dot(p) : '<i class="acct-dot none"></i>'}<div class="who"><b>${esc(p.name)}</b> <span class="muted">${p.provider}</span><small>${who(p)}</small>${same(p)}</div>`
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
      + (F.pane ? waiting()
        : `<p class="acct-note">tmux-rc opens a new window that runs the provider's own sign-in on this dir:</p><input id="acct-cmd" class="acct-cmd" value="${esc(lineOf())}" aria-label="Sign-in command" autocomplete="off" autocapitalize="off" spellcheck="false">`
          + (lineError() ? `<p class="acct-msg err">${esc(lineError())}</p>` : "")
          + (F.provider === "codex" ? ways() : "")
          + (lineOf() === lineFor(LOGIN[F.provider]) ? "" : `<button type="button" class="ghost" data-reset-cmd>Reset to <code>${LOGIN[F.provider]}</code></button>`)
          + `<label for="acct-session">Open it in session</label><select id="acct-session">${sessions().map((x) => `<option ${x === F.session ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>`
          + `<p class="acct-note">You sign in there yourself, so your password and tokens never pass through tmux-rc.</p>`
          + btns('<button class="ghost" data-goto="list">Later</button>', `<button class="primary" data-login ${sessions().length && !lineError() ? "" : "disabled"}>Open sign-in window</button>`));
    else if (s === "abandoned") dlg.innerHTML = head("Sign-in not finished")
      + `<p>${esc(F.name)} is not signed in yet, so it is listed as "not signed in" until you finish or remove it.</p><p class="acct-note"><code>${esc(F.dir)}</code> holds no login; any links you chose are in place and harmless.</p>`
      + (F.pane && paneAlive(F.pane) ? `<label class="acct-check"><input type="checkbox" id="acct-close" checked><span>Close the sign-in window<small>the window tmux-rc opened for this, and nothing else</small></span></label>` : "")
      + btns('<button class="ghost" data-remove-ok>Remove</button>', "<button data-later>Later</button>", '<button class="primary" data-retry>Try again</button>');
    else if (s === "done") {
      const p = byDir(F.dir) || { name: F.name, color: 0, who: "", plan: "", tilde: F.dir };
      dlg.innerHTML = head(`${F.name} is ready`, 4)
        + `<div class="acct-row">${dot(p)}<div class="who"><b>${esc(p.name)}</b> <span class="muted">${F.provider}</span><small>${esc([p.who && `signed in as ${p.who}`, p.plan].filter(Boolean).join(" · "))} · <code>${esc(p.tilde)}</code></small></div></div>`
        + `<p class="acct-note">Read from the dir's non-secret account info.${F.pane ? ` The sign-in window stays open as a normal ${title(F.provider)} pane on ${esc(F.name)}.` : " It was signed in already."}</p>`
        + (F.session ? `<label class="acct-check"><input type="checkbox" id="acct-make-default" checked><span>Make ${esc(F.name)} the default for session <b>${esc(F.session)}</b><small>New windows there start on ${esc(F.name)}; running panes keep their account.</small></span></label>` : "")
        + btns('<button class="primary" data-done>Done</button>');
    }
    // Above the buttons, or under the rename row, which has none.
    const b = dlg.querySelector(".acct-btns"), at = b || dlg.querySelector("#acct-rename")?.parentElement;
    if (F.error) at?.insertAdjacentHTML(b ? "beforebegin" : "afterend", `<p class="acct-msg err" role="alert">${esc(F.error)}</p>`);
  }
  function open(step) {
    F = { step, name: "", provider: "claude", dir: "~/.claude-new", session: sessions()[0] || "" };
    render(); if (!dlg.open) dlg.showModal();
  }
  const go = (step, extra = {}) => { Object.assign(F, { renaming: null, removing: null, error: null, line: null, ...extra, step }); render(); };
  // The sign-in line: as edited in this flow, else VAR=<dir> and the remembered command.
  const lineFor = (cmd) => `${ENV[F.provider]}=${shq(F.dir)} ${cmd}`, lineOf = () => F.line ?? lineFor(cmdOf(F.provider));
  const ways = () => `<div class="acct-seg">${CODEX_WAYS.map(([cmd, name]) => `<button type="button" data-way="${cmd}" aria-pressed="${device() === cmd.includes("--device-auth")}">${name}</button>`).join("")}</div><p class="acct-note">${CODEX_WAYS.map(([, name, how]) => `<b>${name}</b>: ${how}.`).join(" ")}</p>`;
  // The sign-in window is open: its link and code (read off its screen) and the paste field
  // right here, so nobody has to leave the sheet; and a restart, if the other way was picked.
  const waiting = () => {
    const w = F.window, codex = F.provider === "codex", other = codex && w && device() !== w.device;
    return `<p class="acct-note">Window <b>sign in: ${esc(F.name)}</b> is open${F.session ? ` in <b>${esc(F.session)}</b>` : ""}. ${!codex ? "Finish <code>/login</code> there." : w?.device ? "Open the sign-in page and enter the code." : "Open the sign-in page and sign in. Your browser then lands on a <code>localhost</code> page that does not load: paste its address below. tmux-rc hands it to the sign-in waiting on this machine and keeps none of it."}</p>`
      + (!codex ? "" : w?.url ? `<a class="acct-open" href="${esc(w.url)}" target="_blank" rel="noopener noreferrer">Open sign-in page ↗</a>` : '<p class="acct-wait"><i class="acct-spin"></i>Starting sign-in…</p>')
      + (w?.others?.length ? `<p class="acct-msg warn">${w.others.length} older sign-in window${w.others.length === 1 ? " is" : "s are"} open for ${esc(F.name)}; a link from ${w.others.length === 1 ? "it" : "them"} will not work here.</p><button type="button" data-close-others>Close the older ${w.others.length === 1 ? "window" : "windows"}</button>` : "")
      + (w?.device && w.code ? `<p class="acct-note">One-time code: <code class="acct-code">${esc(w.code)}</code></p>` : "")
      + (codex && w && !w.device ? pasteForm("acct-callback", F.callback, F.sending) : "")
      + (codex ? ways() + (other ? `<button type="button" data-restart>Restart as ${device() ? "device code" : "browser sign-in"}</button>` : "") : "")
      + `<p class="acct-wait"><i class="acct-spin"></i>Waiting for ${esc(F.name)} to sign in…</p>`
      + btns('<button class="ghost" data-abandon>Cancel sign-in</button>', "<button data-go>Go to window</button>");
  };
  const device = () => !!shellWords(lineOf())?.includes("--device-auth");
  // What the server would refuse: another dir or variable, or another program.
  const lineError = () => {
    const words = shellWords(lineOf()), v = ENV[F.provider];
    if (!words) return "A quote or a backslash is left open";
    const n = words.findIndex((w) => !/^[A-Za-z_]\w*=/.test(w)), env = n < 0 ? words : words.slice(0, n);
    const p = byDir(F.dir), bin = LOGIN[F.provider].split(" ")[0];
    // ~ as the server expands it: the home the profile's own dir and ~-spelling share.
    const home = p?.tilde?.startsWith("~") ? p.dir.slice(0, p.dir.length - p.tilde.length + 1) : "";
    const canon = (d) => norm(home && /^~(\/|$)/.test(d) ? home + d.slice(1) : d);
    if (env.length !== 1 || !env[0].startsWith(`${v}=`) || ![F.dir, p?.dir].filter(Boolean).map(canon).includes(canon(env[0].slice(v.length + 1))))
      return `Must stay ${v}=${shq(p?.tilde || F.dir)}`;
    return words[env.length] === bin ? "" : `Must run ${bin}`;
  };
  const fail = (error) => { F.error = error.detail || "That did not work. Try again."; render(); };
  const typed = (id) => { const at = dlg.querySelector(`#${id}`).selectionStart; render(); const i = dlg.querySelector(`#${id}`); i.focus(); i.setSelectionRange(at, at); };
  // Not mid-composition: re-rendering would detach an IME's input; compositionend catches up.
  const onType = (e) => {
    if (e.isComposing) return;
    if (e.target.id === "acct-name") { F.name = e.target.value; if (!F.dirTouched) F.dir = `~/.${F.provider}-${slug(F.name)}`; typed("acct-name"); }
    if (e.target.id === "acct-dir") { F.dir = e.target.value.trim(); F.dirTouched = true; typed("acct-dir"); }
    if (e.target.id === "acct-callback") { F.callback = e.target.value; typed("acct-callback"); }
    if (e.target.id === "acct-cmd") { F.line = e.target.value; setCmd(F.provider, F.line.slice(F.line.match(LEAD)[0].length).trim() || LOGIN[F.provider]); typed("acct-cmd"); }
  };
  dlg.addEventListener("input", onType);
  dlg.addEventListener("compositionend", onType);
  dlg.addEventListener("change", (e) => { if (e.target.id === "acct-session") F.session = e.target.value; });
  // Closing stops the sign-in watch, unless the user left for the sign-in window (Go to window).
  dlg.addEventListener("close", () => { if (!F.away) clearTimeout(poll); refresh(); });
  // Sign-in is detected, never driven: the dir's account info appearing, or its window gone.
  const windowOf = (query) => request(`/api/accounts/login/window?${query}`).catch(() => null);
  const openLogin = async () => {
    ({ pane_id: F.pane, pid: F.pid } = await post("/api/accounts/login", { dir: F.dir, session: F.session, command: lineOf() }));
    Object.assign(F, { window: null, seen: false, openedAt: Date.now() }); render(); return wait();
  };
  async function wait() {
    clearTimeout(poll);
    if (F.step !== "login" || !F.pane) return;
    await refresh();
    if (!dlg.open && !F.away) return; // closed while that was in flight: the watch is over
    // Signed in, even while away in the sign-in window: back to the sheet, at Done.
    if (byDir(F.dir)?.signed_in) { F.away = false; go("done"); if (!dlg.open) dlg.showModal(); return; }
    // Gone once seen, or never seen within a grace: the page's state can lag the new window.
    if (paneAlive(F.pane)) F.seen = true;
    else if (F.seen || Date.now() - F.openedAt > 15000) return go("abandoned");
    const w = await windowOf(`pane_id=${encodeURIComponent(F.pane)}`);
    if (w && JSON.stringify(w) !== JSON.stringify(F.window)) {
      F.window = w;
      const focused = document.activeElement?.id; // the paste field, mid-typing
      if (dlg.open && F.step === "login") { render(); if (focused) dlg.querySelector(`#${focused}`)?.focus(); }
    }
    poll = setTimeout(wait, 2000);
  }
  dlg.addEventListener("click", async (e) => {
    const t = e.target.closest("button"), d = t?.dataset || {};
    if (!t) return;
    if ("x" in d) return dlg.close();
    if (d.goto) return go(d.goto);
    // The dir follows the provider: ~/.claude-x becomes ~/.codex-x, typed or suggested.
    if (d.prov) { F.dir = F.dir.replace(new RegExp(`^~/\\.${F.provider}(?=-|$)`), `~/.${d.prov}`); F.provider = d.prov; return render(); }
    if (d.way) { setCmd(F.provider, LOGIN[F.provider]); F.line = lineFor(d.way); return render(); } // a pick, not an edit to remember
    if ("resetCmd" in d) { setCmd(F.provider, LOGIN[F.provider]); F.line = null; return render(); }
    if ("add" in d) return open("name");
    if (d.rename) return go("list", { renaming: d.rename });
    if (d.remove) return go("list", { removing: d.remove });
    if (d.resume) { const p = byDir(d.resume); return go("abandoned", { dir: p.dir, name: p.name, provider: p.provider, pane: null }); }
    if (d.finish) { // back to its sign-in window, if one is still open
      const p = byDir(d.finish), w = await windowOf(`dir=${encodeURIComponent(p.dir)}`);
      go("login", { dir: p.dir, name: p.name, provider: p.provider, pane: w?.pane_id || null, pid: w?.pid, window: w, session: w ? null : F.session, seen: false, openedAt: Date.now() });
      return w && wait();
    }
    t.disabled = true;
    try {
      if ("renameSave" in d) { await request("/api/accounts", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dir: F.renaming, label: dlg.querySelector("#acct-rename").value }) }); await refresh(); return go("list"); }
      // Leaving an unfinished sign-in: close the window it opened, if asked (that pane only).
      if (F.step === "abandoned" && dlg.querySelector("#acct-close")?.checked) {
        await post("/api/accounts/login/close", { pane_id: F.pane, pid: F.pid }); // a failed close stops the removal
        F.pane = null;
      }
      if ("removeOk" in d) { await request("/api/accounts", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dir: F.removing || F.dir, forget: true }) }); if (byDir(F.removing || F.dir) === active()) setFilter(null); await refresh(); return go("list"); }
      if ("create" in d) {
        const link = d.create ? [...dlg.querySelectorAll("[data-link]:checked")].map((i) => i.dataset.link) : [];
        const added = await post("/api/accounts", { provider: F.provider, dir: F.dir, label: F.name, link });
        await refresh();
        const dir = added.dir || F.dir; // a dir already signed in needs no sign-in window
        return go(byDir(dir)?.signed_in ? "done" : "login", { dir, existed: added.existed, pane: null });
      }
      if ("login" in d) return openLogin();
      if ("closeOthers" in d) { for (const o of F.window.others) await post("/api/accounts/login/close", o); F.window = null; return wait(); }
      if ("restart" in d) { await post("/api/accounts/login/close", { pane_id: F.pane, pid: F.pid }); F.session ||= sessions()[0]; return openLogin(); }
      if ("callback" in d) { // one at a time, across the polls' redraws: the code is single-use
        F.sending = true;
        try { await post("/api/accounts/login/callback", { pane_id: F.pane, url: F.callback }, CALLBACK_MS); } finally { F.sending = false; }
        F.callback = ""; return wait();
      }
      if ("retry" in d) return go("login", { pane: null });
      if ("later" in d) return go("list");
      if ("abandon" in d) { clearTimeout(poll); return go("abandoned"); }
      if ("go" in d) { F.away = true; dlg.close(); return openPane(F.pane); }
      if ("done" in d) {
        if (dlg.querySelector("#acct-make-default")?.checked) await post("/api/accounts/default", { session: F.session, dir: F.dir });
        dlg.close(); return notice(`${F.name} added.`, 4000);
      }
    } catch (error) { fail(error); } finally { t.disabled = false; }
  });
  $("accounts-open").onclick = () => { $("more-menu").open = false; open("list"); };

  function matches(pane) { const on = active(); return !on || profiles.find((p) => p.panes.includes(pane.pane_id)) === on; }
  return {
    refresh, usage, byUrgency, strip, groups, picker, pill, paintFilter, any, filter: filterGroup, tag,
    // The URL's account (or null) in, and out for the next URL; follow says whether it changed.
    key: () => filter, follow: (k) => { if (k === filter) return false; filter = k; paintFilter(); return true; },
    matches,
    // Open loops under a filter: only what a pane on the account holds (a PR's or worktree's
    // with no pane belongs to no account, so it is hidden with the rest).
    loops: (report) => !active() || !report?.lanes ? report : { ...report, lanes: Object.fromEntries(Object.entries(report.lanes).map(([lane, gs]) =>
      [lane, gs.map((g) => ({ ...g, items: g.items.filter((i) => [...(i.panes || []), i.pane].some((p) => p && matches(p))) })).filter((g) => g.items.length)])) },
    badge: (paneId) => { const p = ofPane(paneId); return p ? badge(p) : ""; },
    // A session's own default, per provider with several: where its dir differs from the
    // global environment's (under "", which the server folds into every session).
    sessionBadge: (session) => Object.keys(ENV).filter((pr) => several(pr) && set(session, pr) && defaults[session][pr] !== defaults[""]?.[pr]).map((pr) => defaultOf(session, pr)).filter(Boolean).map((p) => badge(p)).join(""),
    // The consent card: which account a chat action lands on.
    consent: (paneId) => { const p = ofPane(paneId); return p ? `<small class="voice-meta acct-on">on ${badge(p)} ${esc(p.tilde)}. Sending never changes a pane's account.</small>` : ""; },
    profile: byDir, list: () => profiles, chip: (p) => badge(p),
    // A browser Codex sign-in window's own card (one this daemon opened, by the server's
    // word): the same paste field as the sheet's waiting step. Drawn once per pane, so a
    // poll never wipes what is being typed.
    paneCard: async (pane, section) => {
      if (!pane || !/^sign in: /.test(pane.window_name || "")) { section.hidden = true; delete section.dataset.pane; return; }
      if (section.dataset.pane === pane.pane_id) return;
      section.dataset.pane = pane.pane_id; section.hidden = true;
      const w = await windowOf(`pane_id=${encodeURIComponent(pane.pane_id)}`);
      if (!w?.codex || w.device || section.dataset.pane !== pane.pane_id) return;
      section.hidden = false;
      section.innerHTML = "<h2>Finish sign-in</h2>" + pasteForm("pane-callback");
      const input = section.querySelector("input"), button = section.querySelector("button");
      input.oninput = () => { button.disabled = !input.value.trim(); };
      button.onclick = async () => {
        button.disabled = true;
        try { await post("/api/accounts/login/callback", { pane_id: pane.pane_id, url: input.value }, CALLBACK_MS); input.value = ""; notice("Sent to the sign-in window.", 4000); }
        catch (error) { notice(error.detail || "That did not work. Try again.", 6000); }
        finally { button.disabled = !input.value.trim(); }
      };
    },
    // Where "Move to another account" can take a pane: its provider's other signed-in
    // accounts, once it has several, each with its line under the name: one on the pane's
    // own login is listed, and says the copy would stay on that login.
    moveTargets: (paneId, provider) => { const from = ofPane(paneId); return several(provider) ? signedIn().filter((p) => p.provider === provider && !p.panes.includes(paneId)).map((p) => ({ ...p,
      sub: from?.same_as?.includes(p.dir) ? `Same login as ${from.name}: the copy stays on that login and its limits` : sub(p) })) : []; },
  };
}
