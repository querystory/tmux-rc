import assert from "node:assert/strict";
import { test } from "node:test";

globalThis.matchMedia = () => ({ matches: false, addEventListener() {} }); // atlas.js reads it on import
const { heatWeek } = await import("../web/m/atlas.js");

const DAY = 86400000, today = new Date().setHours(0, 0, 0, 0);
// heatDays' shape, newest first: [day, cells] with a cell per recorded hour (sparse) holding
// its running-hours and the hours recorded, and each day's total. A number is a whole hour
// at that many running; [sum, seen] a part-recorded one.
function data(days, newest = today) {
  const cell = (v) => v == null ? undefined : Array.isArray(v) ? { sum: v[0], seen: v[1] } : { sum: v, seen: 1 };
  const all = days.map((cells, i) => [newest - i * DAY, cells.map(cell)]);
  return { all, totals: all.map(([, cells]) => cells.reduce((t, c) => t + (c ? c.sum : 0), 0)) };
}
const flat = (v, hours = 24) => Array(hours).fill(v);
const weeks = (...values) => values.flatMap((v) => Array.from({ length: 7 }, () => (Array.isArray(v) ? v : flat(v))));

test("peak hour averages each hour over the days that recorded it", () => {
  const busyAt3 = flat(1);
  busyAt3[3] = 9;
  const gapAt3 = flat(1);
  gapAt3[3] = null; // not recorded: left out, not counted as idle
  assert.equal(heatWeek(data([busyAt3, gapAt3, flat(1)])).peak, 3);
});

test("a part-recorded hour averages over the time seen, not the whole hour", () => {
  const day = flat(10);
  day[2] = [5, 0.25]; // 20 running for the quarter hour recorded
  const w = heatWeek(data([day]));
  assert.equal(w.peak, 2);
  assert.equal(w.avg, (230 + 5) / 23.25);
});

test("an hour never recorded is never the peak; nothing recorded leaves no peak or average", () => {
  const idleAt2 = Array(24);
  idleAt2[14] = 0;
  assert.equal(heatWeek(data([idleAt2])).peak, 14);
  const w = heatWeek(data([Array(24)]));
  assert.deepEqual([w.peak, w.avg, w.change], [-1, null, null]);
});

test("the busiest day is an index into the week", () => {
  assert.equal(heatWeek(data([flat(2, 12), flat(4), flat(1)])).busy, 1);
});

test("the change compares average running over 7 full days to the 7 before, leaving today out", () => {
  assert.equal(heatWeek(data([flat(100), ...weeks(2, 1)])).change, 1);
  assert.equal(heatWeek(data(weeks(3, 1), today - DAY)).change, 2); // no today: the newest day counts
});

test("gaps and a mid-day history start do not read as less running", () => {
  const half = [...Array(12), ...flat(10, 12)];
  assert.equal(heatWeek(data([flat(1), ...weeks(10, half)])).change, 0);
  assert.equal(heatWeek(data([flat(1), ...weeks(10)])).change, null); // no week before yet
});
