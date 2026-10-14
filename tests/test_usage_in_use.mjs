import assert from "node:assert/strict";
import { test } from "node:test";
import { inUse, inlineUsage, IN_USE_SECS } from "../web/m/usage.js";

const NOW = 1_800_000_000_000, ago = (s) => NOW / 1000 - s;
const pane = (activity, idleFor = 0) => ({ activity, state_since: ago(idleFor) });
const [work, personal, codex, demo] = ["Work", "Personal", "Codex", "Demo"].map((name) => ({ name }));
const PANES = new Map([
  [work, [pane("running"), pane("waiting"), pane("idle", 99999)]],
  [personal, [pane("idle", IN_USE_SECS - 60)]], // quiet, but within the hour
  [codex, [pane("running"), pane("running"), pane("running")]],
  [demo, [pane("idle", IN_USE_SECS + 60)]],
]);
const panesOf = (a) => PANES.get(a) || [];
const meter = (acct, window) => ({ acct, windows: [{ window }] });
const names = (list) => list.map((m) => `${m.acct.name} ${m.windows[0].window}`);

test("in use: busy or active in the hour, most busy panes first, each account's meters kept in order", () => {
  const meters = [meter(demo, "7d"), meter(work, "7d"), meter(personal, "5h"), meter(work, "5h"), meter(codex, "7d")];
  const { shown, idle } = inUse(meters, panesOf, NOW);
  assert.deepEqual(names(shown), ["Codex 7d", "Work 7d", "Work 5h", "Personal 5h"]);
  assert.deepEqual(idle, [demo]);
});

test("ties keep the urgency order they came in", () => {
  const meters = [meter(personal, "7d"), meter(demo, "7d"), meter(work, "5h")];
  assert.deepEqual(names(inUse(meters, () => [pane("running")], NOW).shown), ["Personal 7d", "Demo 7d", "Work 5h"]);
});

test("nothing in use shows every account, with no +N", () => {
  const meters = [meter(demo, "7d"), meter({ name: "Spare" }, "5h")];
  assert.deepEqual(inUse(meters, panesOf, NOW), { shown: meters, idle: [] });
});

test("the header chip's 5h and 7d, coloured as the strip colours them", () => {
  const row = { windows: [{ window: "5h", pct: 15.4 }, { window: "7d", pct: 89, projected: 95 }, { window: "7d Fable", pct: 99, limit_at: 1 }] };
  assert.equal(inlineUsage(row), ' · 5h <b class="">15%</b> · 7d <b class="warn">89%</b>');
  assert.equal(inlineUsage(undefined), "");
});
