import assert from "node:assert/strict";
import { test } from "node:test";
import { renderUsage, shownUsage, HIGH_PCT } from "../web/m/usage.js";

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

test("countdowns and the even-pace mark run from the real clock unless told otherwise", () => {
  const now = Date.now(), H = 3600e3, el = {};
  const win = { window: "5h", pct: 40, projected: 80, start: now - 2 * H, resets_at: now + 3 * H, samples: [[now - H, 20]] };
  renderUsage(el, [{ provider: "claude", windows: [win] }]);
  assert.match(el.innerHTML, /resets 3h0m</);
  // Two hours of five gone: the mark sits 40% of the way along, not back at the start.
  assert.match(el.innerHTML, /class="pace" style="left:40\.0%"/);
});

test("a reading kept through a failed read says how old it is", () => {
  const now = Date.now(), el = {};
  const win = { window: "5h", pct: 40, start: now - 3600e3, resets_at: now + 3600e3, samples: [] };
  renderUsage(el, [{ provider: "claude", as_of: now - 6 * 60e3, windows: [win] }], { now });
  assert.match(el.innerHTML, /<small>as of 6m ago<\/small>/);
  renderUsage(el, [{ provider: "claude", windows: [win] }], { now });
  assert.doesNotMatch(el.innerHTML, /as of/);
});

test("a reading from before its window reset counts nothing down to zero", () => {
  const now = Date.now(), el = {};
  renderUsage(el, [{ provider: "claude", windows: [{ window: "5h", pct: 90, start: now - 6 * 3600e3, resets_at: now - 60e3, limit_at: now - 120e3, samples: [] }] }], { now });
  assert.doesNotMatch(el.innerHTML, /resets|full in/);
});

test("a window not yet opened (no reset) draws a bar with no pace or reset", () => {
  const el = {};
  renderUsage(el, [{ provider: "claude", windows: [{ window: "7d Fable", pct: 0, resets_at: null, samples: [] }] }]);
  assert.match(el.innerHTML, /usage-bar/);
  assert.doesNotMatch(el.innerHTML, /pace|NaN|Invalid|resets/);
});

test("each meter is named by its account and window", () => {
  const el = {};
  renderUsage(el, [{ provider: "claude", acct: { name: "Work" }, windows: [{ window: "5h", pct: 3, resets_at: null, samples: [] }] }]);
  assert.match(el.innerHTML, /role="meter" aria-label="Claude Work 5h"/);
});
