import assert from "node:assert/strict";
import { test } from "node:test";
import { renderUsage, renderStrip, staleUsage, shownUsage, HIGH_PCT } from "../web/m/usage.js";

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

test("countdowns and the trend run from the real clock unless told otherwise", () => {
  const now = Date.now(), H = 3600e3, el = {};
  const win = { window: "5h", pct: 40, projected: 80, start: now - 2 * H, resets_at: now + 3 * H, samples: [[now - H, 20]] };
  renderUsage(el, [{ provider: "claude", windows: [win] }], { wide: true });
  assert.match(el.innerHTML, /resets 3h0m</);
  // The held point sits at now, 40% of the way across the 120-wide trend, not back at its start.
  assert.match(el.innerHTML, /<polyline class="proj" points="48\.0,/);
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

test("a window not yet opened (no reset) draws a bar, not a trend", () => {
  const el = {};
  renderUsage(el, [{ provider: "claude", windows: [{ window: "7d Fable", pct: 0, resets_at: null, samples: [] }] }], { wide: true });
  assert.doesNotMatch(el.innerHTML, /usage-spark|Invalid|resets/);
});

test("each meter is named by its account and window", () => {
  const el = {};
  renderUsage(el, [{ provider: "claude", acct: { name: "Work" }, windows: [{ window: "5h", pct: 3, resets_at: null, samples: [] }] }]);
  assert.match(el.innerHTML, /role="meter" aria-label="Claude Work 5h"/);
});

test("the session list's strip draws a ring card per window, the account by its chip", () => {
  const now = Date.now(), H = 3600e3, el = {};
  const win = { window: "7d", pct: 40, projected: 80, start: now - 2 * H, resets_at: now + 3 * H, samples: [] };
  renderStrip(el, [{ provider: "claude", label: "Work", chip: '<span class="acct-badge">Work</span>', windows: [win] }], { now });
  assert.match(el.innerHTML, /class="usage-card " [^>]*style="--p:40"><i class="usage-ring">/);
  assert.match(el.innerHTML, /<small>· resets 3h0m<\/small>/);
  assert.match(el.innerHTML, /<span><span class="acct-badge">Work<\/span> · 7d<\/span>/);
  renderStrip(el, [{ provider: "claude", windows: [{ ...win, limit_at: now + H }] }], { now });
  assert.match(el.innerHTML, /<small class="out">· full in 1h0m<\/small>/);
  assert.match(el.innerHTML, /alt="" width="12" height="12">Claude · 7d/); // one account: the provider, by its logo
});

test("a kept reading's age is said once for the strip, by the oldest", () => {
  const now = Date.now();
  assert.equal(staleUsage([{ as_of: now - 6 * 60e3 }, {}, { as_of: now - 20 * 60e3 }], now), '<small class="usage-stale">as of 20m ago</small>');
  assert.equal(staleUsage([{}], now), "");
});
