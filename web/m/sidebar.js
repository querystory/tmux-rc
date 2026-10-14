// The wide-screen sidebar's pane list (round 3 of docs' layout mock: I3's sidebar with
// I5's inbox). Needs you is pinned as cards you can answer without opening the pane;
// everything else is grouped by state or by tmux session. Each group folds and switches
// between one-line rows and activity cards, and those choices are a per-screen preference,
// so they live in localStorage, not the URL. So does the one Sub-agents switch, which lists
// every pane's running sub-agents under its row in either density (Needs you cards only
// count them): seeing them is its own question, apart from how much of each pane's
// activity to show. Folded, the sidebar is a rail: its actions and every listed pane as icons
// (docs/design/sidebar.md).
// Phones never draw this list (theirs is renderPhoneList in app.js), but their Needs-you rows
// borrow its answers and inline Reply: render.answers, so there is one of each.
import { dismissable } from "/m/header-picker.js";
import { Composer, enterSubmits } from "/m/composer.js";
import { needsYou, isRunning, isRecent, FILTERS, markWorking, paneName, newestFirst, paneActivity, paneHeadline, paneMeta, questionItems, since, age, markEnding, records, liveSubagents, subagentCount } from "/m/pane-model.js";
import { paneLinks } from "/pr-links.js";

const KEY = "tmuxrc-sidebar-list";
const prefs = { by: "state", rail: false, fold: {}, cards: {}, subagents: true };
try { const saved = JSON.parse(localStorage.getItem(KEY)); if (saved?.fold && saved.cards) Object.assign(prefs, saved); } catch {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch {} };

const rank = (p) => needsYou(p) ? 0 : isRunning(p) ? 1 : isRecent(p) ? 2 : 3;
export const SHOW = { attention: "Needs you", working: "Working", finished: "Just finished", idle: "Idle" }; // the sections, by filter name
const STATES = [["run", "working"], ["done", "finished"], ["idle", "idle"]].map(([id, f]) => [id, SHOW[f], (p) => FILTERS[f](p)]); // the filters' own sets (no index as nowMs)
export const SORTS = { updated: "Last activity", session: "Session order" }; // app.js keeps the choice
const HOVER_MS = 350;

// Needs you first, then the groups; a group with nothing in it is not drawn.
function groups(subset, query, sort) {
  // Session order is the order the watcher publishes, tmux's own: a stable sort that ties keeps it.
  const order = sort === "session" ? () => 0 : (a, b) => rank(a) - rank(b) || newestFirst(a, b);
  const rest = subset.filter((p) => !needsYou(p)).sort(order); // in session order, the sessions are too
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
    g.panes.sort(order);
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
  // Expanded, the whole sidebar above the usage strip scrolls (#sb-scroll); the rail, its list.
  let anchor = null;
  const wrap = document.getElementById("sb-scroll"), scroller = () => prefs.rail ? root : wrap;
  root.addEventListener("click", (e) => { anchor = e.target.closest("button"); }, true);
  const rerender = () => {
    save();
    const el = anchor?.isConnected ? anchor : null, y = el?.getBoundingClientRect().top, focused = el === document.activeElement;
    root.style.paddingBottom = "";
    repaint();
    if (!el?.isConnected) return;
    const sc = scroller(), want = sc.scrollTop + el.getBoundingClientRect().top - y;
    sc.scrollTop = want;
    if (sc.scrollTop < want - 1) { // pad a screen, scroll, then trim the padding to fit
      root.style.paddingBottom = `${sc.clientHeight}px`;
      sc.scrollTop = want;
      root.style.paddingBottom = `${parseFloat(root.style.paddingBottom) + sc.clientHeight + want - sc.scrollHeight}px`;
    }
    if (focused) el.focus({ preventScroll: true });
  };
  const app = document.getElementById("app");
  const collapse = document.getElementById("collapse");
  collapse.onclick = () => { prefs.rail = !prefs.rail; rerender(); };
  // The rail's search is the box's own icon: focusing the box (a click, or Tab) expands the sidebar.
  const search = document.querySelector("#sessions .search"), input = search.querySelector("input");
  search.addEventListener("click", () => input.focus());
  input.addEventListener("focus", () => { if (prefs.rail) { prefs.rail = false; rerender(); } });

  // The Sessions title's counts: the panes listed, plain text beside it, and at the right their
  // running sub-agents, a button with an eye (open while they are listed under their panes'
  // rows, slashed while not).
  const count = Object.assign(document.createElement("span"), { className: "sb-count" });
  const subs = Object.assign(document.createElement("button"), { className: "sb-subs", title: "List the running sub-agents of every pane outside Needs you under its row" });
  subs.onclick = () => { prefs.subagents = !prefs.subagents; rerender(); };
  document.querySelector("#sb-sess b").after(count, subs);
  // The URL's filters (wide has no tab bar to carry them) are a button beside the search box,
  // since both narrow the list. Its menu: State (a union; none is All), Account (the
  // accounts, app.js adds them), then Group by and Sort by, how you like to read the list:
  // per-browser preferences the button does not count. It stays open as you pick.
  const picker = document.createElement("details"), menu = document.createElement("div");
  picker.id = "side-filter-picker"; picker.className = "header-picker sb-only";
  picker.innerHTML = `<summary title="Filter" aria-label="Filter panes" data-n="">${licon("filter", 16)}</summary>`;
  menu.className = "header-picker-menu"; picker.append(menu);
  dismissable(picker);
  // A group of the menu: options are buttons whose aria-pressed draws the check (and, for the
  // states, a dot in the state's colour, as their chips have).
  const group = (label, opts, pick, rule = "<hr>", dot = "") => {
    const el = document.createElement("div");
    el.setAttribute("role", "group"); el.ariaLabel = label;
    el.innerHTML = `${rule}<div class="menu-sec">${label}</div>${Object.entries(opts).map(([v, l]) => `<button type="button" class="acct-opt" data-v="${v}"${dot && ` data-s="${v}"`}>${dot && `<i class="acct-dot${v === "all" ? " none" : ""}"></i>`}<span>${l}</span></button>`).join("")}`;
    el.onclick = (e) => { const b = e.target.closest("[data-v]"); if (b) pick(b.dataset.v); };
    el.mark = (on) => el.querySelectorAll("[data-v]").forEach((b) => b.setAttribute("aria-pressed", String(on(b.dataset.v))));
    return el;
  };
  let filters = [];
  const show = group("State", { all: "All", ...SHOW }, (v) => ctx.setFilter(v === "all" ? [] : filters.includes(v) ? filters.filter((f) => f !== v) : [...filters, v]), "", "dot"); // each state's dot in its colour
  const by = group("Group by", { state: "State", session: "Session" }, (v) => { prefs.by = v; rerender(); });
  const sortBy = group("Sort by", SORTS, ctx.setSort);
  // Clear all, at the foot: what the chips' Clear all does (app.js disables it with nothing on).
  const clearAll = Object.assign(document.createElement("button"), { type: "button", className: "acct-opt menu-clear", textContent: "Clear all" });
  clearAll.onclick = ctx.clearAll;
  menu.append(show);
  document.getElementById("find").append(picker);
  // The menu opens beside the list it filters, never over it: past the sidebar's edge, level
  // with the trigger, so the list narrows in view. With no room there it drops under the
  // trigger, nudged in where a narrow sidebar would clip its left edge.
  picker.addEventListener("toggle", () => {
    Object.assign(menu.style, { position: "", left: "", top: "", right: "", maxHeight: "" });
    if (!picker.open) return;
    const side = root.getBoundingClientRect(), top = picker.getBoundingClientRect().top;
    if (innerWidth - side.right >= menu.offsetWidth + 16) Object.assign(menu.style, { position: "fixed", right: "auto",
      left: `${side.right + 8}px`, top: `${Math.max(8, Math.min(top, innerHeight - menu.offsetHeight - 8))}px` });
    else {
      const over = side.left + 8 - menu.getBoundingClientRect().left;
      if (over > 0) menu.style.right = `${-over}px`;
    }
    menu.style.maxHeight = `${innerHeight - menu.getBoundingClientRect().top - 8}px`; // many accounts: it scrolls
  });

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
    ctx.html(fold, `${licon(g.open ? "chevronDown" : "chevron", 14)}${g.alert ? licon("alert", 13) : ""}<span class="l"></span><span class="c acct-badge">${running && g.id !== "run" ? `<i></i>${running} · ` : ""}${g.panes.length}</span>`);
    fold.lastChild.dataset.s = g.id; // its state's colour; set as data, since a session's id holds its name
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
    open.innerHTML = '<span class="sb-logo"><img alt=""></span><span class="t"><b></b><span class="s"></span><span class="sb-acct"></span></span><span class="a"></span><span class="sb-n" role="img"></span>' + (card ? '<p></p><p class="sb-items"></p>' : "");
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
      // Sent: close the Reply, unless more was typed while it went (compose clears as it sends).
      if (!ctx.sending() && value?.segments().length && await ctx.compose(id, value) && !value.segments().length) done(id);
    };
  }
  function updateRow(node, { p, g, card }) {
    node._p = p;
    markEnding(node, p);
    node.classList.toggle("need", ctx.asks(p));
    node.classList.toggle("outside", ctx.outside(p)); // needs you, shown though the filter leaves it out
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
    ctx.html(node.querySelector(".sb-acct"), ctx.badge(p.pane_id));
    text(node.querySelector(".a"), p.ending || age(p));
    // Needs you is about the question and its answers, so its cards only count sub-agents.
    const agents = liveSubagents(p), badge = node.querySelector(".sb-n"), list = node.querySelector(".sb-agents"), lines = prefs.subagents && g.id !== "need";
    badge.hidden = !agents.length || lines; list.hidden = !agents.length || !lines;
    badge.title = badge.ariaLabel = `${agents.length} sub-agent${agents.length === 1 ? "" : "s"} working`;
    ctx.html(badge, `${licon("bot", 12)}${agents.length}`);
    renderItems(list, list.hidden ? [] : agents, true);
    if (card) {
      text(node.querySelector("p"), paneActivity(p) || "No recent activity");
      text(node.querySelector(".sb-items"), needsYou(p) ? questionItems(p.question) : "");
      replies(node, p);
    }
  }

  // The same answers, the same send and the same pending hold as the pane's own question
  // card, its note standing in for Reply; Reply opens a composer that posts to the pane's
  // /compose like its footer.
  function replies(node, p) {
    const answered = ctx.answered(p), busy = ctx.sending() || !!answered;
    const options = needsYou(p) ? ctx.answers(p.question) : [];
    // A finished turn's offers ride every card, Needs you or not (app.js sendOffer).
    const items = [...options, ...ctx.offers(p).map((offer) => ({ offer })), ...(!needsYou(p) ? [] : answered ? [{ note: answered.note }] : [{ reply: true }])];
    reconcile(node.querySelector(".sb-replies"), items, (o) => o.note != null ? "note" : o.reply ? "reply" : o.offer ? `offer:${o.offer}` : `${o.index}:${o.option}`, (o) => {
      if (o.note != null) return Object.assign(document.createElement("span"), { className: "sb-note", role: "status" });
      const button = document.createElement("button");
      button.type = "button";
      button.onclick = () => o.reply ? openReply(node._p.pane_id, node._p.secret) : o.offer ? ctx.sendOffer(node._p.pane_id, o.offer) : ctx.answer(node._p.pane_id, o.option, o.index);
      return button;
    }, (el, o) => {
      if (o.note != null) return text(el, o.note);
      const label = o.reply ? "Reply" : o.offer || o.option;
      text(el, label); el.title = o.reply ? "" : label; el.disabled = busy;
      el.classList.toggle("picked", !o.reply && !o.offer && ctx.picked(p, o));
    });
    const form = node.querySelector(".sb-compose"); // a secret pane's draft: see app.js pruneDrafts
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
  // Folded to the rail: every pane the list shows, in its order, each with its state's dot
  // (and, with several accounts, its account's); hovering one shows its card.
  function rail(subset) {
    reconcile(root, groups(subset, "", ctx.sort()).flatMap((g) => g.panes), (p) => `rail:${p.pane_id}`, () => {
      const b = document.createElement("button");
      b.className = "sb-rail";
      b.innerHTML = '<span class="sb-logo"><img alt=""></span><span class="sb-acct"></span>';
      b.onclick = () => ctx.navigate(b._p.pane_id);
      return b;
    }, (b, p) => {
      b._p = p;
      markEnding(b, p);
      const agents = subagentCount(p);
      b.ariaLabel = `${p.session} / ${paneName(p)}${agents ? `, ${agents} sub-agent${agents === 1 ? "" : "s"} working` : ""}${ctx.outside(p) ? ", outside filter" : ""}`;
      b.classList.toggle("need", ctx.asks(p));
      b.dataset.state = ctx.asks(p) ? "need" : isRunning(p) ? "run" : "";
      b.classList.toggle("outside", ctx.outside(p));
      if (p.pane_id === ctx.active()) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
      markWorking(b.querySelector("img"), p, ctx.logos);
      ctx.html(b.lastChild, ctx.badge(p.pane_id));
    });
  }

  // The hover card: what the pane overview would tell you, from the same /api/state record
  // and the same helpers, beside the sidebar after a short hover (or keyboard focus).
  // Mouse only: a tap is a navigation, and phones never render this list anyway.
  const card = Object.assign(document.createElement("div"), { id: "side-hover", hidden: true });
  card.setAttribute("role", "tooltip");
  card.innerHTML = '<div class="h"><span class="sb-logo"><img alt=""></span><span><b></b><small></small></span></div><small class="w"></small><span class="sb-acct"></span><span class="badge"></span><p class="x"></p><p class="q"></p><p class="q-cmd"></p><small class="m"></small><small class="k"></small><div class="sb-agents"></div><div class="l"></div>';
  document.body.append(card);
  let hoverOn = null, pending = null, hoverTimer = 0;
  const paneOf = (el) => (el.closest(".sb-row, .sb-card") || el)._p;
  function fill(el) {
    const p = paneOf(el), $c = (sel) => card.querySelector(sel), tasks = records(p.tasks);
    markWorking($c("img"), p, ctx.logos);
    text($c("b"), paneName(p));
    text($c(".h small"), [p.tool, p.model].filter(Boolean).join(" · "));
    text($c(".w"), [p.session, p.window_name, p.window_index !== "" && p.window_index != null ? `Window ${p.window_index}` : "", p.pane_id].filter(Boolean).join(" / "));
    ctx.html($c(".sb-acct"), ctx.badge(p.pane_id));
    $c(".badge").className = `badge ${ctx.badgeOf(p).cls}`;
    text($c(".badge"), [ctx.badgeOf(p).label, since(p)].filter(Boolean).join(" · "));
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
  for (const el of [root, wrap]) el.addEventListener("scroll", unhover, { passive: true });
  // The Sessions block sticks at the top of the scroll; a hairline shows once it has.
  const sticky = document.getElementById("sb-sticky");
  wrap.addEventListener("scroll", () => sticky.classList.toggle("stuck", wrap.scrollTop > sticky.offsetTop - wrap.offsetTop - 1 && wrap.scrollTop > 0), { passive: true });
  root.addEventListener("click", unhover);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") unhover(); });
  addEventListener("resize", unhover); // a breakpoint crossing hides the sidebar but not this

  function render(subset, query, filter) {
    app.classList.toggle("rail", prefs.rail);
    collapse.title = collapse.ariaLabel = prefs.rail ? "Expand sidebar" : "Collapse sidebar";
    search.title = prefs.rail ? "Search" : "";
    filters = filter;
    show.mark((v) => v === "all" ? !filter.length : filter.includes(v));
    by.mark((v) => v === prefs.by);
    sortBy.mark((v) => v === ctx.sort());
    if (prefs.rail) rail(subset); else list(subset, query);
    // Reconciling may have replaced the hovered row (a card that became a row, say).
    if (hoverOn) { if (hoverOn.isConnected) fill(hoverOn); else unhover(); }
  }
  function list(subset, query) {
    shown = groups(subset, query, ctx.sort());
    // A folded group still shows the open pane, so the selection never disappears. A pane
    // with offers to tap is a card in any group, as Needs you is.
    const of = (g) => [{ g }, ...g.panes.filter((p) => g.open || p.pane_id === ctx.active()).map((p) => ({ p, g, card: g.cards || ctx.offers(p).length > 0 }))];
    reconcile(root, shown.flatMap(of), (i) => i.p ? `${i.card ? "c" : "r"}:${i.p.pane_id}` : `g:${i.g.id}`,
      (i) => i.p ? row(i.card) : head(i.g), (node, i) => i.p ? updateRow(node, i) : updateHead(node, i.g));
    const n = subset.reduce((n, p) => n + liveSubagents(p).length, 0);
    subs.setAttribute("aria-pressed", String(prefs.subagents));
    subs.ariaLabel = `${n} sub-agents: ${prefs.subagents ? "hide" : "list"} them under their panes`;
    text(count, subset.length);
    ctx.html(subs, `${n} sub-agent${n === 1 ? "" : "s"} ${licon(prefs.subagents ? "eye" : "eyeOff", 13)}`);
  }
  render.picker = picker; // the filter menu: app.js adds the account filter, then these
  render.views = [by, sortBy, document.createElement("hr"), clearAll];
  render.clearAll = clearAll;
  render.drafts = drafts; // for the app's unsent-draft guard on reload
  // Both layouts' lists call this first: a Reply closes once its pane stops needing you.
  render.answers = { add: addAnswers, update: replies, prune: (subset) => {
    if (replyTo && (!drafts.has(replyTo) || !subset.some((p) => p.pane_id === replyTo && needsYou(p)))) replyTo = null;
  } };
  return render;
}
