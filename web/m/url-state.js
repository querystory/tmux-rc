// Navigation state <-> location.hash, with no DOM so `node --test` can drive it.
//
// The hash is the whole addressable state: `#pane=%25N&view=terminal&filter=attention&acct=claude-work&sort=session`
// or, with no pane, `#view=dashboard`, or one of PAGES: a dashboard card's own view (`#view=heat`, `#view=loops`). A hash (not a path) because the daemon serves /m and
// nothing under it, and because the installed PWA's start_url stays the plain list. Unknown
// fields (push's `from=push`) are ignored on read and dropped on write; `compose=1` is
// read-only, set by push links to focus the reply box.
//
// Also reads the old desktop UI's `#/pane/<id>` and `#/list/<filter>` so links saved from it
// keep working.

const FILTERS = ["attention", "running", "recent"];
export const PAGES = ["heat", "usage", "loops"]; // the dashboard's cards that open their own view

// `tab` is the view a pane opens on when the hash names none: the wide Layout picker's choice.
export function parseHash(hash, tab = "summary") {
  const raw = String(hash || "").replace(/^#/, "");
  const legacy = /^\/(pane|list)\/(.+)$/.exec(raw);
  let params;
  if (legacy) {
    let value = legacy[2];
    try { value = decodeURIComponent(value); } catch { /* keep the raw text; it just won't match a pane */ }
    params = new URLSearchParams(legacy[1] === "pane" ? { pane: value } : { filter: value === "waiting" ? "attention" : value });
  } else params = new URLSearchParams(raw);
  const pane = params.get("pane") || null;
  return {
    pane,
    view: ["summary", "terminal"].includes(params.get("view")) ? params.get("view") : tab,
    dashboard: !pane && (params.get("view") === "dashboard" || PAGES.includes(params.get("view"))),
    page: !pane && PAGES.includes(params.get("view")) ? params.get("view") : null,
    filter: FILTERS.includes(params.get("filter")) ? params.get("filter") : "all",
    acct: params.get("acct") || null, // the account filter: a name accounts.js resolves
    sort: params.get("sort") === "session" ? "session" : "updated",
    compose: params.get("compose") === "1",
  };
}

// Defaults are omitted so the bare list is the bare URL.
export function formatHash({ pane = null, view = "summary", dashboard = false, page = null, filter = "all", acct = null, sort = "updated" }) {
  const params = new URLSearchParams();
  if (filter !== "all") params.set("filter", filter);
  if (acct) params.set("acct", acct);
  if (sort !== "updated") params.set("sort", sort);
  if (pane) { params.set("pane", pane); if (view === "terminal") params.set("view", "terminal"); }
  else if (dashboard) params.set("view", page || "dashboard");
  return params.toString();
}

// Back should undo a move to another screen (list, dashboard, a card's view or a different pane) and
// nothing smaller: tab, filter (state or account) and sort changes replace, or Back would step through every
// tap instead of leaving the pane.
export function historyMode(fromHash, toHash) {
  const from = parseHash(fromHash), to = parseHash(toHash);
  return from.pane !== to.pane || from.dashboard !== to.dashboard || from.page !== to.page ? "push" : "replace";
}
