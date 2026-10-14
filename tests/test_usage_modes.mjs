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

test("countdowns and the trend run from the real clock unless told otherwise", () => {
  const now = Date.now(), H = 3600e3, el = {};
  const win = { window: "5h", pct: 40, projected: 80, start: now - 2 * H, resets_at: now + 3 * H, samples: [[now - H, 20]] };
  renderUsage(el, [{ provider: "claude", windows: [win] }], { wide: true, detail: true });
  assert.match(el.innerHTML, /resets 3h0m, /);
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

test("the usage view keeps the past window being read across a refresh that adds another", () => {
  const now = Date.now(), H = 3600e3, el = { addEventListener() {} };
  const ended = (resets_at, pct) => ({ window: "5h", pct, start: resets_at - 5 * H, resets_at, samples: [], limit_at: null });
  const meter = (past) => [{ provider: "claude", dirs: ["~/.claude"], windows: [{ window: "5h", pct: 3, past, start: now - H, resets_at: now + 4 * H, samples: [] }] }];
  const draw = (past) => { renderUsage(el, meter(past), { wide: true, detail: true, now, icon: () => "" }); return el.innerHTML; };
  draw([ended(now - 11 * H, 74), ended(now - H, 50)]);
  el._back[Object.keys(el._list)[0]] = now - 11 * H; // stepped back two windows
  assert.match(draw([ended(now - 11 * H, 74), ended(now - H, 50)]), /ended at 74%/);
  assert.match(draw([ended(now - 11 * H, 74), ended(now - 6 * H, 50), ended(now - H, 20)]), /ended at 74%/);
  assert.match(draw([ended(now - 11 * H + 60e3, 74), ended(now - H, 50)]), /ended at 74%/); // its reset re-read a minute on
});

test("a window not yet opened (no reset) is not drawn as an ended one", () => {
  const el = {};
  renderUsage(el, [{ provider: "claude", windows: [{ window: "7d Fable", pct: 0, resets_at: null, samples: [] }] }], { wide: true, detail: true });
  assert.doesNotMatch(el.innerHTML, /ended|Invalid/);
});
