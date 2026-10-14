// Navigation state <-> location.hash, with no DOM so `node --test` can drive it.
//
// The hash is the whole addressable state: `#pane=%25N&view=terminal&filter=working,attention&acct=claude-work&q=api`
// or, with no pane, `#view=dashboard`, or one of PAGES: a dashboard card's own view (`#view=heat`, `#view=loops`). A hash (not a path) because the daemon serves /m and
// nothing under it, and because the installed PWA's start_url stays the plain list. Unknown
// fields (push's `from=push`) are ignored on read and dropped on write; `compose=1` is
// read-only, set by push links to focus the reply box. `filter` and `acct` are lists (a pane
// shows if it matches any state and any account; empty is all), comma-separated, so an old
// single-value link reads as a list of one. The sort is a per-browser preference, not here.
//
// Also reads the old desktop UI's `#/pane/<id>` and `#/list/<filter>` so links saved from it
// keep working.

const FILTERS = ["attention", "working", "finished", "idle"]; // the list's sections, in its order
const OLD = { running: "working", recent: "finished", waiting: "attention" }; // earlier links' names
export const PAGES = ["heat", "usage", "loops"]; // the dashboard's cards that open their own view

// `tab` is the view a pane opens on when the hash names none: the wide Layout picker's choice.
export function parseHash(hash, tab = "summary") {
  const raw = String(hash || "").replace(/^#/, "");
  const legacy = /^\/(pane|list)\/(.+)$/.exec(raw);
  let params, lists = raw;
  if (legacy) {
    let value = legacy[2];
    try { value = decodeURIComponent(value); } catch { /* keep the raw text; it just won't match a pane */ }
    params = new URLSearchParams(legacy[1] === "pane" ? { pane: value } : { filter: value });
    lists = params.toString();
  } else params = new URLSearchParams(raw);
  const pane = params.get("pane") || null;
  return {
    pane,
    view: ["summary", "terminal"].includes(params.get("view")) ? params.get("view") : tab,
    dashboard: !pane && (params.get("view") === "dashboard" || PAGES.includes(params.get("view"))),
    page: !pane && PAGES.includes(params.get("view")) ? params.get("view") : null,
    filter: FILTERS.filter((f) => list(lists, "filter").map((v) => OLD[v] || v).includes(f)),
    acct: list(lists, "acct"), // the account filter: names accounts.js resolves
    q: params.get("q")?.trim() || "", // the list's search box
    compose: params.get("compose") === "1",
  };
}

// Defaults are omitted so the bare list is the bare URL.
export function formatHash({ pane = null, view = "summary", dashboard = false, page = null, filter = [], acct = [], q = "" }) {
  const params = new URLSearchParams();
  if (q.trim()) params.set("q", q.trim());
  if (pane) { params.set("pane", pane); if (view === "terminal") params.set("view", "terminal"); }
  else if (dashboard) params.set("view", page || "dashboard");
  const lists = Object.entries({ filter, acct }).filter(([, v]) => v.length).map(([k, v]) => `${k}=${v.map(encodeURIComponent).join(",")}`);
  return [...lists, params.toString()].filter(Boolean).join("&");
}
// A list is read from the raw hash: a literal comma divides it, and each item is encoded on
// its own, so an account dir with a comma is one item, as is an old single-value link's
// (URLSearchParams wrote acct=team%2Cwest, and + for a space).
const decode = (s) => { try { return decodeURIComponent(s.replace(/\+/g, " ")); } catch { return s; } };
const list = (raw, name) => [...new Set((new RegExp(`(?:^|&)${name}=([^&]*)`).exec(raw)?.[1] || "").split(",").filter(Boolean).map(decode))];

// Back should undo a move to another screen (list, dashboard, a card's view or a different pane) and
// nothing smaller: tab and filter (state, account or search) changes replace, or Back would step through every
// tap instead of leaving the pane.
export function historyMode(fromHash, toHash) {
  const from = parseHash(fromHash), to = parseHash(toHash);
  return from.pane !== to.pane || from.dashboard !== to.dashboard || from.page !== to.page ? "push" : "replace";
}
