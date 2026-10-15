import assert from "node:assert/strict";
import { test } from "node:test";
import { renderUsage, renderStrip, staleUsage, byUrgency, shownUsage, moreUsage, HIGH_PCT } from "../web/m/usage.js";

const w = (window, pct, limit_at = null) => ({ window, pct, limit_at });
const ACCOUNTS = [
  { provider: "claude", windows: [w("5h", 16), w("7d", HIGH_PCT), w("7d Fable", 52, 1)] },
  { provider: "codex", windows: [w("7d", 40)] },
  { provider: "claude", label: "b", error: "unavailable", windows: [] },
];
const name = (a) => `${a.label ?? a.provider} ${a.windows[0]?.window ?? ""}`;

test("every meter in one order, most urgent first, whatever its account", () => {
  const list = byUrgency([
    { provider: "claude", label: "Work", windows: [{ ...w("5h", 20), projected: 30 }, { ...w("7d", 60), projected: 95 }] },
    { provider: "codex", label: "Spare", windows: [w("7d", 40, 5), w("5h", 100)] },
    { provider: "claude", label: "Gone", error: "unavailable", windows: [] },
  ]);
  assert.deepEqual(list.map(name), ["Spare 5h", "Spare 7d", "Work 7d", "Work 5h", "Gone "]);
});

test("on shows every meter, off none, neither folds", () => {
  const list = byUrgency(ACCOUNTS);
  assert.deepEqual(shownUsage(list, "on"), { shown: list, quiet: [] });
  assert.deepEqual(shownUsage(list, "off"), { shown: [], quiet: [] });
});

test("auto shows meters filling before the reset or at the threshold, folding the rest into +N", () => {
  const { shown, quiet } = shownUsage(byUrgency(ACCOUNTS), "auto");
  assert.deepEqual(shown.map(name), ["claude 7d Fable", "claude 7d"]);
  assert.deepEqual(quiet.map(name), ["codex 7d", "claude 5h", "b "]);
  assert.match(moreUsage(quiet), /title="Quiet: Codex 7d 40%, Claude 5h 16%, Claude b">\+3</);
  assert.equal(moreUsage([]), "");
});

test("auto with nothing worth a glance still shows the most urgent meter", () => {
  const { shown, quiet } = shownUsage(byUrgency([{ provider: "codex", windows: [w("7d", HIGH_PCT - 1), w("5h", 10)] }]), "auto");
  assert.deepEqual([shown.map(name), quiet.map(name)], [["codex 7d"], ["codex 5h"]]);
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
