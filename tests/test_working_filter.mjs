import assert from "node:assert/strict";
import { test } from "node:test";
import { matchesFilter, activityLabel, isRunning } from "../web/m/pane-model.js";

const now = Date.now();
const idle = { pane_id: "%3", tool: "claude", activity: "idle", state_since: now / 1000 - 3600 };

test("a pane idle at its prompt while its subagents work counts as Working", () => {
  for (const pane of [{ ...idle, subagents: [{ label: "Port the converter", state: "running" }] },
                      { ...idle, subagents: [{ label: "Summarize", state: "compacting" }] },
                      { ...idle, agents: 2 }]) {  // omp's own count, with no roster
    assert.equal(matchesFilter(pane, ["working"], now), true);
    assert.equal(matchesFilter(pane, ["idle"], now), false);
    assert.equal(activityLabel(pane), "Working");
    assert.equal(isRunning(pane), true);
  }
});

test("finished or waiting subagents leave an idle pane idle", () => {
  const pane = { ...idle, agents: 0, subagents: [{ label: "a", state: "done" }, { label: "b", state: "waiting" }] };
  assert.equal(matchesFilter(pane, ["working"], now), false);
  assert.equal(activityLabel(pane), "Idle");
});

test("a question still needs you, whatever its subagents are doing", () => {
  const pane = { ...idle, activity: "waiting", waiting_on: "user", agents: 1 };
  assert.equal(matchesFilter(pane, ["working"], now), false);
  assert.equal(activityLabel(pane), "Needs you");
});
