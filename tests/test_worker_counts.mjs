import assert from "node:assert/strict";
import { test } from "node:test";
import { liveCounts, stateOf } from "../web/m/fleet-counts.js";

test("pane population stays identical with reported workers", () => {
  const panes = [{ tool: "shell", activity: "idle" }];
  const workers = [{ tool: "codex", activity: "running" }];
  assert.deepEqual(liveCounts(panes, workers, "panes"), [0, 0, 1, 0, 0, 0, 0]);
  assert.deepEqual(liveCounts(panes, workers), [0, 1, 1, 0, 0, 0, 0]);
});

test("visible workers and completed workers do not add a background count", () => {
  const panes = [{ pane_id: "%1", activity: "running", tool: "codex" }];
  const workers = [{ pane_id: "%1", activity: "running" }, { activity: "done" },
    { activity: "waiting", waiting_on: "user" }, { activity: "waiting", waiting_on: "external" }];
  assert.deepEqual(liveCounts(panes, workers), [1, 1, 0, 0, 0, 1, 0]);
});

test("ordinary parsed rosters keep their existing counts", () => {
  const panes = [{ activity: "idle", tool: "omp", agents: 2,
    subagents: [{ state: "running" }, { state: "done" }] }];
  assert.deepEqual(liveCounts(panes), [0, 2, 1, 0, 0, 0, 0]);
});

test("all reported lifecycle states use the historical category order", () => {
  const workers = ["running", "idle", "unknown", "compacting", "quota_blocked"].map(activity => ({ activity }));
  assert.deepEqual(liveCounts([], workers), [0, 1, 1, 1, 1, 0, 1]);
  assert.equal(stateOf({ activity: "unexpected" }), 3);
});


test("custom harness workers count without a built-in tool registration", () => {
  const workers = [{tool: "garden-runner", activity: "running"},
    {tool: "garden-runner", activity: "waiting", waiting_on: "user"}];
  assert.deepEqual(liveCounts([], workers), [1, 1, 0, 0, 0, 0, 0]);
  assert.deepEqual(liveCounts([], workers, "panes"), [0, 0, 0, 0, 0, 0, 0]);
});
