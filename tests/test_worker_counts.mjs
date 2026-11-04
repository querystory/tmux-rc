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

test("pane-bound workers retain harness attribution without counting the pane twice", () => {
  const panes = [{pane_id: "%2", session: "garden", tool: "shell", activity: "running"}];
  const workers = [{pane_id: "%2", session: "garden", tool: "garden-runner", activity: "running"}];
  for (const tools of [[], ["garden-runner"], ["shell"], ["shell", "garden-runner"]]) {
    const selected = rows => rows.filter(row => !tools.length || tools.includes(row.tool));
    assert.deepEqual(liveCounts(selected(panes), selected(workers)), [0, 1, 0, 0, 0, 0, 0]);
    assert.deepEqual(liveCounts(selected(panes), selected(workers), "panes"),
      tools.length === 1 && tools[0] === "garden-runner" ? [0, 0, 0, 0, 0, 0, 0] : [0, 1, 0, 0, 0, 0, 0]);
  }
});

test("worker scope follows its controller session while binding deduplicates the fleet", () => {
  const panes = [{pane_id: "%2", session: "orchard", tool: "shell", activity: "running"}];
  const workers = [{pane_id: "%2", session: "garden", tool: "garden-runner", activity: "running"}];
  assert.deepEqual(liveCounts(panes, workers), [0, 1, 0, 0, 0, 0, 0]);
  for (const session of ["garden", "orchard"]) {
    const selected = rows => rows.filter(row => row.session === session);
    assert.deepEqual(liveCounts(selected(panes), selected(workers)), [0, 1, 0, 0, 0, 0, 0]);
  }
});

test("bound worker attention uses the reported state only when its pane is outside scope", () => {
  const panes = [{pane_id: "%2", tool: "shell", activity: "idle"}];
  const workers = [{pane_id: "%2", tool: "garden-runner", activity: "waiting", waiting_on: "user"}];
  assert.deepEqual(liveCounts(panes, workers), [0, 0, 1, 0, 0, 0, 0]);
  assert.deepEqual(liveCounts([], workers), [1, 0, 0, 0, 0, 0, 0]);
  assert.deepEqual(liveCounts([], [{...workers[0], activity: "done"}]), [0, 0, 0, 0, 0, 0, 0]);
});
