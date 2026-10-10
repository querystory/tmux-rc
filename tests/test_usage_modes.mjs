import assert from "node:assert/strict";
import { test } from "node:test";
import { shownUsage, HIGH_PCT } from "../web/m/usage.js";

const w = (window, pct, limit_at = null) => ({ window, pct, limit_at });
const ACCOUNTS = [
  { provider: "claude", windows: [w("5h", 16), w("7d", HIGH_PCT), w("7d Fable", 52, 1)] },
  { provider: "codex", windows: [w("7d", 40)] },
  { provider: "claude", label: "b", error: "unavailable", windows: [] },
];
const names = (accounts) => accounts.map((a) => `${a.provider}:${a.windows.map((x) => x.window)}`);

test("on shows every meter, off none", () => {
  assert.equal(shownUsage(ACCOUNTS, "on"), ACCOUNTS);
  assert.deepEqual(shownUsage(ACCOUNTS, "off"), []);
});

test("auto keeps meters filling before the reset or at the threshold, and drops empty or unreadable accounts", () => {
  assert.deepEqual(names(shownUsage(ACCOUNTS, "auto")), ["claude:7d,7d Fable"]);
});

test("auto with nothing worth showing hides the strip", () => {
  assert.deepEqual(shownUsage([{ provider: "codex", windows: [w("7d", HIGH_PCT - 1)] }], "auto"), []);
});
