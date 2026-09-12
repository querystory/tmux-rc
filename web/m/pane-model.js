// Pure pane predicates for the mobile UI: no DOM, no clock of their own (time is injected),
// so tests/test_mobile_pane_model.py can run them under node against fixture panes.

// Same fold rule as the desktop dock (web/app.js PARKED_IDLE_SECS): an idle pane older
// than this is parked, so it drops out of "Recent".
export const PARKED_IDLE_SECS = 600;
const ACTIVITIES = ["running", "waiting", "idle", "compacting", "unknown"];
const actOf = (pane) => ACTIVITIES.includes(pane.activity) ? pane.activity : "unknown";
export const needsYou = (pane) => pane.activity === "waiting" && pane.waiting_on !== "external";
export const activityLabel = (pane) => needsYou(pane) ? "Needs you" : ({ running: "Running", waiting: "Working", idle: "Idle", compacting: "Compacting", unknown: "Unknown" }[actOf(pane)]);
// "waiting" on something external (a tool, a subagent) is shown as running: the pane is
// busy, it just isn't our turn.
export const activityClass = (pane) => pane.activity === "waiting" && !needsYou(pane) ? "running" : actOf(pane);
export const isRunning = (pane) => ["running", "compacting"].includes(activityClass(pane));
export function isRecent(pane, nowMs = Date.now()) {
  const since = pane.state_since == null ? NaN : Number(pane.state_since);
  const idle = Number.isFinite(since) ? Math.max(0, nowMs / 1000 - since) : pane.idle_seconds || 0;
  return pane.activity !== "idle" || idle < PARKED_IDLE_SECS;
}
export const FILTERS = { all: () => true, running: isRunning, recent: (pane, nowMs) => isRecent(pane, nowMs), attention: needsYou };
export function matchesFilter(pane, filter, nowMs = Date.now()) {
  const predicate = Object.prototype.hasOwnProperty.call(FILTERS, filter) ? FILTERS[filter] : FILTERS.all;
  return predicate(pane, nowMs);
}
// Is the URL still on pane `id`? Nothing may pull the user out of a pane on the strength of
// the app's own `active`: navigate() only assigns location.hash, and the hashchange that
// updates `active` is queued behind it, so for one task `active` still names the pane the
// user just LEFT. Both involuntary exits fire inside exactly that window — a state poll that
// no longer lists the old pane, and the old pane's live stream taking its 404 — and each
// would replace the URL of the pane the user just tapped and dump them back on the list. The
// hash changed the instant they tapped, so it, not `active`, is the authority on where they
// want to be.
export const stillOnPane = (hash, id) => !!id && new URLSearchParams(String(hash).replace(/^#/, "")).get("pane") === id;

// Sort key for "Sort by updated": the parser's timestamp when it has one, else the moment
// the pane's state last changed, never later than when an idle pane went idle.
export function lastActivity(pane) {
  if (Number.isFinite(pane.last_activity_at)) return pane.last_activity_at;
  const changed = (Number(pane.updated_at) || 0) - (Number(pane.idle_seconds) || 0);
  const since = Number(pane.state_since);
  return pane.activity === "idle" && since > 0 ? Math.min(changed, since) : changed;
}
