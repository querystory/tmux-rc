// Navigation state <-> location.hash, with no DOM so `node --test` can drive it.
//
// The hash is the whole addressable state: `#pane=%25N&view=terminal&filter=attention&sort=session`
// or, with no pane, `#view=dashboard`. A hash (not a path) because the daemon serves /m and
// nothing under it, and because the installed PWA's start_url stays the plain list. Unknown
// fields (push's `from=push`) are ignored on read and dropped on write; `compose=1` is
// read-only, set by push links to focus the reply box.
//
// Also reads the old desktop UI's `#/pane/<id>` and `#/list/<filter>` so links saved from it
// keep working.

const FILTERS = ["attention", "running", "recent"];

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
    dashboard: !pane && params.get("view") === "dashboard",
    filter: FILTERS.includes(params.get("filter")) ? params.get("filter") : "all",
    sort: params.get("sort") === "session" ? "session" : "updated",
    compose: params.get("compose") === "1",
  };
}

// Defaults are omitted so the bare list is the bare URL.
export function formatHash({ pane = null, view = "summary", dashboard = false, filter = "all", sort = "updated" }) {
  const params = new URLSearchParams();
  if (filter !== "all") params.set("filter", filter);
  if (sort !== "updated") params.set("sort", sort);
  if (pane) { params.set("pane", pane); if (view === "terminal") params.set("view", "terminal"); }
  else if (dashboard) params.set("view", "dashboard");
  return params.toString();
}

// Back should undo a move to another screen (list, dashboard or a different pane) and
// nothing smaller: tab, filter and sort changes replace, or Back would step through every
// tap instead of leaving the pane.
export function historyMode(fromHash, toHash) {
  const from = parseHash(fromHash), to = parseHash(toHash);
  return from.pane !== to.pane || from.dashboard !== to.dashboard ? "push" : "replace";
}
