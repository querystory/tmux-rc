// The wide-screen sidebar's pane list (round 3 of docs' layout mock: I3's sidebar with
// I5's inbox). Needs you is pinned as cards you can answer without opening the pane;
// everything else is grouped by state or by tmux session. Each group folds and switches
// between one-line rows and activity cards, and those choices are a per-screen preference,
// so they live in localStorage, not the URL. So does the one Sub-agents switch, which lists
// every pane's running sub-agents under its row in either density (Needs you cards only
// count them): seeing them is its own question, apart from how much of each pane's
// activity to show. Folded, the sidebar is a rail of live agents.
// Phones never draw this list (theirs is renderPhoneList in app.js), but their Needs-you rows
// borrow its answers and inline Reply: render.answers, so there is one of each.
import { headerPicker } from "/m/header-picker.js";
import { Composer, enterSubmits } from "/m/composer.js";
import { needsYou, isRunning, isRecent, markWorking, paneName, lastActivity, paneActivity, paneHeadline, paneMeta, activityLabel, activityClass, records, liveSubagents, subagentCount, since, age } from "/m/pane-model.js";
import { paneLinks } from "/pr-links.js";

const KEY = "tmuxrc-sidebar-list";
const prefs = { by: "state", rail: false, fold: {}, cards: {}, subagents: true };
try { const saved = JSON.parse(localStorage.getItem(KEY)); if (saved?.fold && saved.cards) Object.assign(prefs, saved); } catch {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch {} };

const alive = (p) => p.activity !== "idle";
const rank = (p) => needsYou(p) ? 0 : isRunning(p) ? 1 : isRecent(p) ? 2 : 3;
const STATES = [["run", "Working", isRunning], ["done", "Just finished", (p) => isRecent(p)], ["idle", "Idle", () => true]];
const FILTERS = [["all", "All"], ["running", "Running"], ["recent", "Recent"], ["attention", "Needs you"]];
const HOVER_MS = 350;

// Needs you first, then the groups; a group with nothing in it is not drawn.
function groups(subset, query) {
  const rest = subset.filter((p) => !needsYou(p));
  const list = prefs.by === "session"
    ? [...new Set(rest.map((p) => p.session))].map((s) => {
      const panes = rest.filter((p) => p.session === s);
      return { id: `session:${s}`, label: s || "Session", panes };
    })
    : STATES.map(([id, label, test]) => {
      const panes = rest.filter(test);
      panes.forEach((p) => rest.splice(rest.indexOf(p), 1)); // each pane lands in its first matching state
      return { id, label, panes };
    });
  list.unshift({ id: "need", label: "Needs you", panes: subset.filter(needsYou), alert: true });
  for (const g of list) {
    g.panes.sort((a, b) => rank(a) - rank(b) || lastActivity(b) - lastActivity(a));
    g.cards = prefs.cards[g.id] ?? g.id === "need";
    g.open = !!query || !prefs.fold[g.id]; // open unless the user folded it (only explicit folds are stored)
  }
  return list.filter((g) => g.panes.length);
}

export function setupSidebar(ctx) {
  const { licon, reconcile, text, renderItems } = ctx;
  const root = document.getElementById("side-list");
  let shown = [], replyTo = null;
  // Inline drafts, per pane like the footer's: kept until sent or cancelled.
  const drafts = new Map(), draft = () => drafts.get(replyTo);
  const repaint = ctx.repaint;
  // A control never moves under the pointer: whatever it folds, unfolds or reorders moves
  // around it, so a second click undoes the first. Every control re-renders through here,
  // which holds the last-clicked button's screen position (and its focus, which reconcile
  // drops when it moves the node). Folding near the end leaves too little list below to
  // scroll back by, so the shortfall is padded until the next control changes it.
  let anchor = null;
  root.addEventListener("click", (e) => { anchor = e.target.closest("button"); }, true);
  const rerender = () => {
    save();
    const el = anchor?.isConnected ? anchor : null, y = el?.getBoundingClientRect().top, focused = el === document.activeElement;
    root.style.paddingBottom = "";
    repaint();
    if (!el?.isConnected) return;
    const want = root.scrollTop + el.getBoundingClientRect().top - y;
    root.scrollTop = want;
    if (root.scrollTop < want - 1) { // pad a screen, scroll, then trim the padding to fit
      root.style.paddingBottom = `${root.clientHeight}px`;
      root.scrollTop = want;
      root.style.paddingBottom = `${2 * root.clientHeight + want - root.scrollHeight}px`;
    }
    if (focused) el.focus({ preventScroll: true });
  };
  const app = document.getElementById("app");
  const collapse = document.getElementById("collapse");
  collapse.onclick = () => { prefs.rail = !prefs.rail; rerender(); };

  // Group by, the URL's filter (wide has no tab bar to carry it), and the Sub-agents switch.
  const bar = document.createElement("div");
  bar.className = "sb-bar";
  bar.innerHTML = `<span>Group by</span><span class="seg"><button data-by="state">State</button><button data-by="session">Session</button></span>`;
  bar.querySelectorAll("[data-by]").forEach((b) => { b.onclick = () => { prefs.by = b.dataset.by; rerender(); }; });
  const pick = Object.assign(document.createElement("select"), { id: "side-filter" });
  pick.setAttribute("aria-label", "Filter panes");
  pick.append(...FILTERS.map(([v, l]) => new Option(l, v)));
  pick.onchange = () => ctx.setFilter(pick.value);
  const subs = Object.assign(document.createElement("button"), { className: "sb-switch", title: "List the running sub-agents of every pane outside Needs you under its row" });
  subs.setAttribute("role", "switch");
  subs.onclick = () => { prefs.subagents = !prefs.subagents; rerender(); };
  const show = Object.assign(document.createElement("div"), { className: "sb-show", textContent: "Show" });
  show.append(subs);
  bar.append(pick, show);
  const refreshPick = headerPicker(pick);

  function head(g) {
    const node = document.createElement("div");
    node.className = "sb-group";
    node.innerHTML = '<button class="sb-fold"></button><button class="sb-icon"></button>';
    node.firstChild.onclick = () => { prefs.fold[node._g.id] = node._g.open; rerender(); };
    node.lastChild.onclick = () => { prefs.cards[node._g.id] = !node._g.cards; rerender(); };
    return node;
  }
  function updateHead(node, g) {
    node._g = g;
    const fold = node.firstChild, density = node.lastChild, running = g.panes.filter(isRunning).length;
    fold.setAttribute("aria-expanded", String(g.open));
    ctx.html(fold, `${licon(g.open ? "chevronDown" : "chevron", 14)}${g.alert ? licon("alert", 13) : ""}<span class="l"></span><span class="c">${running && g.id !== "run" ? `<i></i>${running} · ` : ""}${g.panes.length}</span>`);
    text(fold.querySelector(".l"), g.label);
    density.setAttribute("aria-pressed", String(g.cards));
    density.title = density.ariaLabel = `${g.cards ? "Compact rows" : "Show the latest activity"} for ${g.label}`;
    ctx.html(density, licon("rows", 15));
  }

  // A row is one line; a card adds the latest activity and, for Needs you, the answers.
  // Either way the pane's button is .sb-open, and its running sub-agents are listed under it
  // while the switch is on; off, a count beside the age says they are there.
  function row(card) {
    const node = document.createElement("div"), open = document.createElement("button");
    node.className = card ? "sb-card" : "sb-row";
    open.className = "sb-open";
    open.innerHTML = '<span class="sb-logo"><img alt=""></span><span class="t"><b></b><span class="s"></span></span><span class="a"></span><span class="sb-n" role="img"></span>' + (card ? "<p></p>" : "");
    node.append(open, Object.assign(document.createElement("div"), { className: "sb-agents" }));
    if (card) addAnswers(node);
    open.onclick = () => ctx.navigate(node._p.pane_id);
    return node;
  }
  // A Needs-you card's answer buttons and its Reply composer, for the pane in `node._p`.
  function addAnswers(node) {
    node.insertAdjacentHTML("beforeend", `<div class="sb-replies"></div><form class="sb-compose" hidden><button type="button" class="sb-icon" aria-label="Cancel" title="Cancel">${licon("x", 15)}</button><button type="submit" class="sb-icon primary" aria-label="Send message" title="Send message">${licon("up", 15)}</button></form>`);
    const form = node.querySelector("form");
    // Pasted images are object URLs: release them with the draft, as pruneDrafts does.
    const done = (id) => { drafts.get(id)?.files.forEach((_, chip) => URL.revokeObjectURL(chip.src)); drafts.delete(id); if (replyTo === id) replyTo = null; repaint(); };
    form.querySelector("[type=button]").onclick = () => done(node._p.pane_id);
    enterSubmits(form, (target) => !!draft()?.editor.contains(target));
    form.onsubmit = async (event) => {
      event.preventDefault();
      const id = node._p.pane_id, value = drafts.get(id);
      if (!ctx.sending() && value?.segments().length && await ctx.compose(id, value)) done(id);
    };
  }
  function updateRow(node, { p, g, card }) {
    node._p = p;
    node.classList.toggle("need", needsYou(p));
    // On the card for its styling and on its button, which is what assistive tech lands on.
    for (const el of [node, node.firstChild]) {
      if (p.pane_id === ctx.active()) el.setAttribute("aria-current", "true"); else el.removeAttribute("aria-current");
    }
    const img = node.querySelector("img");
    img.alt = p.tool || "tmux";
    markWorking(img, p, ctx.logos);
    // By state the session is context; by session it is the group heading already.
    text(node.querySelector("b"), paneName(p));
    text(node.querySelector(".s"), prefs.by === "state" || g.id === "need" ? ` · ${p.session}` : "");
    text(node.querySelector(".a"), age(p));
    // Needs you is about the question and its answers, so its cards only count sub-agents.
    const agents = liveSubagents(p), badge = node.querySelector(".sb-n"), list = node.querySelector(".sb-agents"), lines = prefs.subagents && g.id !== "need";
    badge.hidden = !agents.length || lines; list.hidden = !agents.length || !lines;
    badge.title = badge.ariaLabel = `${agents.length} sub-agent${agents.length === 1 ? "" : "s"} working`;
    ctx.html(badge, `${licon("bot", 12)}${agents.length}`);
    renderItems(list, list.hidden ? [] : agents, true);
    if (card) {
      text(node.querySelector("p"), paneActivity(p) || "No recent activity");
      replies(node, p);
    }
  }

  // The same answers, the same send and the same "Answer sent" hold as the pane's own
  // question card; Reply opens a composer that posts to the pane's /compose like its footer.
  function replies(node, p) {
    const options = needsYou(p) ? ctx.answers(p.question) : [], busy = ctx.sending() || ctx.answered(p);
    const items = [...options, ...(needsYou(p) ? [{ reply: true }] : [])];
    reconcile(node.querySelector(".sb-replies"), items, (o) => o.reply ? "reply" : `${o.index}:${o.option}`, (o) => {
      const button = document.createElement("button");
      button.type = "button";
      button.onclick = () => o.reply ? openReply(node._p.pane_id, node._p.secret) : ctx.answer(node._p.pane_id, o.option, o.index);
      return button;
    }, (button, o) => { text(button, o.reply ? "Reply" : o.option); button.title = o.reply ? "" : o.option; button.disabled = busy; });
    const form = node.querySelector(".sb-compose"), typed = drafts.get(p.pane_id);
    // Text typed here before the prompt showed up is probably the password: drop it.
    if (p.secret && typed?.segments().length) typed.replace([]);
    form.hidden = replyTo !== p.pane_id || !!p.secret;
    if (form.hidden) return form.querySelector("#side-reply")?.remove();
    if (!form.contains(draft().editor)) form.prepend(draft().editor);
    form.querySelector("button[type=submit]").disabled = ctx.sending() || !draft().segments().length;
  }
  // At a password prompt Reply opens the pane, whose own composer is the password field.
  function openReply(id, secret) {
    if (secret && id !== ctx.active()) ctx.navigate(id);
    if (id === ctx.active()) { document.getElementById(secret ? "secret" : "reply").focus(); return; } // its own composer is on screen
    replyTo = id;
    if (!drafts.has(id)) drafts.set(id, new Composer(repaint, ctx.notice, { id: "side-reply", label: "Reply to this pane" }));
    repaint();
    draft().editor.focus();
  }
  // Folded to the rail: what is alive, Needs you first; idle panes are one count.
  function rail(subset) {
    const live = subset.filter(alive).sort((a, b) => rank(a) - rank(b));
    const idle = subset.length - live.length;
    reconcile(root, [...live, ...(idle ? [{ more: idle }] : [])], (p) => p.more ? "more" : `rail:${p.pane_id}`, (p) => {
      const b = document.createElement("button");
      b.className = p.more ? "sb-more" : "sb-rail";
      b.innerHTML = p.more ? "" : '<span class="sb-logo"><img alt=""></span>';
      b.onclick = () => p.more ? collapse.click() : ctx.navigate(b._p.pane_id);
      return b;
    }, (b, p) => {
      if (p.more) { text(b, `+${p.more}`); b.title = b.ariaLabel = `${p.more} idle panes: expand the sidebar`; return; }
      b._p = p;
      const agents = subagentCount(p);
      b.ariaLabel = `${p.session} / ${paneName(p)}${agents ? `, ${agents} sub-agent${agents === 1 ? "" : "s"} working` : ""}`;
      b.classList.toggle("need", needsYou(p));
      b.dataset.agents = agents || "";
      if (p.pane_id === ctx.active()) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
      markWorking(b.querySelector("img"), p, ctx.logos);
    });
  }

  // The hover card: what the pane overview would tell you, from the same /api/state record
  // and the same helpers, beside the sidebar after a short hover (or keyboard focus).
  // Mouse only: a tap is a navigation, and phones never render this list anyway.
  const card = Object.assign(document.createElement("div"), { id: "side-hover", hidden: true });
  card.setAttribute("role", "tooltip");
  card.innerHTML = '<div class="h"><span class="sb-logo"><img alt=""></span><span><b></b><small></small></span></div><small class="w"></small><span class="badge"></span><p class="x"></p><p class="q"></p><p class="q-cmd"></p><small class="m"></small><small class="k"></small><div class="sb-agents"></div><div class="l"></div>';
  document.body.append(card);
  let hoverOn = null, pending = null, hoverTimer = 0;
  const paneOf = (el) => (el.closest(".sb-row, .sb-card") || el)._p;
  function fill(el) {
    const p = paneOf(el), $c = (sel) => card.querySelector(sel), tasks = records(p.tasks);
    markWorking($c("img"), p, ctx.logos);
    text($c("b"), paneName(p));
    text($c(".h small"), [p.tool, p.model].filter(Boolean).join(" · "));
    text($c(".w"), [p.session, p.window_name, p.window_index !== "" && p.window_index != null ? `Window ${p.window_index}` : "", p.pane_id].filter(Boolean).join(" / "));
    $c(".badge").className = `badge ${activityClass(p)}`;
    text($c(".badge"), `${activityLabel(p)} · ${since(p)}`);
    text($c(".x"), paneHeadline(p) || "No recent activity");
    const asked = needsYou(p) && (p.question?.ask || p.question?.prompt);
    text($c(".q"), asked && asked !== paneHeadline(p) ? asked : "");
    text($c(".q-cmd"), (needsYou(p) && p.question?.context) || "");
    text($c(".m"), paneMeta(p));
    renderItems($c(".sb-agents"), records(p.subagents), true);
    text($c(".k"), tasks.length ? `Tasks ${tasks.filter((t) => t.done).length}/${tasks.length}` : "");
    reconcile($c(".l"), paneLinks(p).slice(0, 2), (l) => l.href, () => document.createElement("small"), (n, l) => text(n, l.detail ? `${l.text} (${l.detail})` : l.text));
  }
  function hover(el) {
    if (el === hoverOn || el === pending) return;
    unhover();
    pending = el;
    hoverTimer = setTimeout(() => {
      pending = null;
      if (!el.isConnected) return;
      hoverOn = el; fill(el); card.hidden = false;
      el.setAttribute("aria-describedby", card.id);
      const r = el.getBoundingClientRect(), h = card.offsetHeight;
      card.style.left = `${document.getElementById("sessions").getBoundingClientRect().right + 8}px`;
      // Below the row's top edge, or flipped up to end at its bottom near the screen's foot.
      card.style.top = `${Math.max(8, r.top + h > innerHeight - 8 ? r.bottom - h : r.top)}px`;
    }, HOVER_MS);
  }
  function unhover() {
    clearTimeout(hoverTimer);
    pending = null;
    hoverOn?.removeAttribute("aria-describedby");
    hoverOn = null; card.hidden = true;
  }
  // The pointer hovers a whole card (answers and composer too); the keyboard lands on its button.
  const target = (e, sel = ".sb-row, .sb-rail, .sb-card") => e.target.closest?.(sel);
  root.addEventListener("pointerover", (e) => { if (e.pointerType === "mouse" && target(e)) hover(target(e)); });
  root.addEventListener("pointerout", (e) => { const el = target(e); if (el && !el.contains(e.relatedTarget)) unhover(); });
  root.addEventListener("focusin", (e) => { const el = target(e, ".sb-rail, .sb-open"); if (el?.matches(":focus-visible")) hover(el); });
  root.addEventListener("focusout", unhover);
  root.addEventListener("scroll", unhover, { passive: true });
  root.addEventListener("click", unhover);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") unhover(); });
  addEventListener("resize", unhover); // a breakpoint crossing hides the sidebar but not this

  function render(subset, query, filter) {
    app.classList.toggle("rail", prefs.rail);
    collapse.title = collapse.ariaLabel = prefs.rail ? "Expand sidebar" : "Collapse sidebar";
    if (prefs.rail) rail(subset); else list(subset, query, filter);
    // Reconciling may have replaced the hovered row (a card that became a row, say).
    if (hoverOn) { if (hoverOn.isConnected) fill(hoverOn); else unhover(); }
  }
  function list(subset, query, filter) {
    shown = groups(subset, query);
    // A folded group still shows the open pane, so the selection never disappears.
    const of = (g) => [{ g }, ...g.panes.filter((p) => g.open || p.pane_id === ctx.active()).map((p) => ({ p, g, card: g.cards }))];
    // The controls head the list, above everything they fold or expand (Needs you included).
    const items = [{ bar: true }, ...shown.flatMap(of)];
    reconcile(root, items, (i) => i.bar ? "bar" : i.p ? `${i.card ? "c" : "r"}:${i.p.pane_id}` : `g:${i.g.id}`,
      (i) => i.bar ? bar : i.p ? row(i.card) : head(i.g),
      (node, i) => i.bar ? null : i.p ? updateRow(node, i) : updateHead(node, i.g));
    bar.querySelectorAll("[data-by]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.by === prefs.by)));
    if (pick.value !== filter) { pick.value = filter; refreshPick(); }
    subs.setAttribute("aria-checked", String(prefs.subagents));
    ctx.html(subs, `${licon("bot", 14)}Sub-agents <span class="n">${subset.reduce((n, p) => n + liveSubagents(p).length, 0)}</span><i></i>`);
  }
  render.drafts = drafts; // for the app's unsent-draft guard on reload
  // Both layouts' lists call this first: a Reply closes once its pane stops needing you.
  render.answers = { add: addAnswers, update: replies, prune: (subset) => {
    if (replyTo && (!drafts.has(replyTo) || !subset.some((p) => p.pane_id === replyTo && needsYou(p)))) replyTo = null;
  } };
  return render;
}
