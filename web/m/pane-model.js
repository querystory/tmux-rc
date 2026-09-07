// Pure pane predicates for the mobile UI: no DOM, no clock of their own (time is injected),
// so tests/test_mobile_pane_model.py can run them under node against fixture panes.

// Same fold rule as the desktop dock (web/app.js PARKED_IDLE_SECS): an idle pane older
// than this is parked, so it drops out of "Recent".
export const PARKED_IDLE_SECS = 600;
export const needsYou = (pane) => pane.activity === "waiting" && pane.waiting_on !== "external";
export const activityLabel = (pane) => needsYou(pane) ? "Needs you" : ({ running: "Running", waiting: "Working", idle: "Idle", compacting: "Compacting", unknown: "Unknown" }[pane.activity] || "Unknown");
// "waiting" on something external (a tool, a subagent) is shown as running: the pane is
// busy, it just isn't our turn.
export const activityClass = (pane) => pane.activity === "waiting" && !needsYou(pane) ? "running" : pane.activity;
export const isRunning = (pane) => ["running", "compacting"].includes(activityClass(pane));
export function isRecent(pane, nowMs = Date.now()) {
  const since = pane.state_since == null ? NaN : Number(pane.state_since);
  const idle = Number.isFinite(since) ? Math.max(0, nowMs / 1000 - since) : pane.idle_seconds || 0;
  return pane.activity !== "idle" || idle < PARKED_IDLE_SECS;
}
export const FILTERS = { all: () => true, running: isRunning, recent: (pane, nowMs) => isRecent(pane, nowMs), attention: needsYou };
export function matchesFilter(pane, filter, nowMs = Date.now()) { return (FILTERS[filter] || FILTERS.all)(pane, nowMs); }
// Sort key for "Sort by updated": the parser's timestamp when it has one, else the moment
// the pane's state last changed, never later than when an idle pane went idle.
export function lastActivity(pane) {
  if (Number.isFinite(pane.last_activity_at)) return pane.last_activity_at;
  const changed = (Number(pane.updated_at) || 0) - (Number(pane.idle_seconds) || 0);
  const since = Number(pane.state_since);
  return pane.activity === "idle" && since > 0 ? Math.min(changed, since) : changed;
}
