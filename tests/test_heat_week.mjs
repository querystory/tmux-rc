import assert from "node:assert/strict";
import { test } from "node:test";

globalThis.matchMedia = () => ({ matches: false, addEventListener() {} }); // atlas.js reads it on import
const { heatWeek } = await import("../web/m/atlas.js");

const DAY = 86400000, today = new Date().setHours(0, 0, 0, 0);
// heatDays' shape, newest first: [day, cells] with a cell per recorded hour (sparse) holding
// its running-hours and the hours recorded, each day's total, and when history starts. A
// number is a whole hour at that many running; [sum, seen] a part-recorded one.
function data(days, newest = today, first = 0) {
  const cell = (v) => v == null ? undefined : Array.isArray(v) ? { sum: v[0], seen: v[1] } : { sum: v, seen: 1 };
  const all = days.map((cells, i) => [newest - i * DAY, cells.map(cell)]);
  return { all, first, totals: all.map(([, cells]) => cells.reduce((t, c) => t + (c ? c.sum : 0), 0)) };
}
const flat = (v, hours = 24) => Array(hours).fill(v);

test("peak hour averages each hour over the days that recorded it", () => {
  const busyAt3 = flat(1);
  busyAt3[3] = 9;
  const gapAt3 = flat(1);
  gapAt3[3] = null; // not recorded: left out, not counted as idle
  const w = heatWeek(data([busyAt3, gapAt3, flat(1)]));
  assert.equal(w.peak, 3);
  assert.equal(w.peakAvg, 5);
});

test("a part-recorded hour averages over the time seen, not the whole hour", () => {
  const day = flat(10);
  day[2] = [5, 0.25]; // 20 running for the quarter hour recorded
  const w = heatWeek(data([day]));
  assert.equal(w.peak, 2);
  assert.equal(w.peakAvg, 20);
  assert.equal(w.avg, (230 + 5) / 23.25);
});

test("average running counts only recorded hours, and the busiest day is an index into the week", () => {
  const w = heatWeek(data([flat(2, 12), flat(4), flat(1)]));
  assert.equal(w.avg, (2 * 12 + 4 * 24 + 24) / 60);
  assert.equal(w.busy, 1);
});

test("the week compares 7 full days to the 7 before, leaving today out", () => {
  const days = [flat(100), ...Array.from({ length: 7 }, () => flat(2)), ...Array.from({ length: 7 }, () => flat(1))];
  const w = heatWeek(data(days));
  assert.equal(w.now, 7 * 48);
  assert.equal(w.before, 7 * 24);
});

test("without today, the week starts at the newest day; short history has no comparison", () => {
  const days = Array.from({ length: 14 }, (_, i) => flat(i < 7 ? 3 : 1));
  assert.deepEqual([heatWeek(data(days, today - DAY)).now, heatWeek(data(days, today - DAY)).before], [7 * 72, 7 * 24]);
  assert.equal(heatWeek(data(days)).before, null); // today plus 13: one full day short
  const fifteen = [flat(1), ...days], oldest = today - 14 * DAY;
  assert.equal(heatWeek(data(fifteen, today, oldest + DAY / 2)).before, null); // history began mid-day on the oldest
  assert.equal(heatWeek(data(fifteen, today, oldest)).before, 7 * 24);
  assert.equal(heatWeek(data([])).busy, -1);
});
