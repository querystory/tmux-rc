// Renders the vendored ECharts server-side so the ticks asserted are the ones it really
// places, for each history window at phone and desktop plot widths.
process.env.TZ = "America/Los_Angeles";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";
import { timeAxis, timeLabel } from "../web/m/time-axis.js";

const echarts = createRequire(import.meta.url)("../web/m/vendor/echarts.min.js");
const HOUR = 3600e3, END = Date.parse("2026-09-30T02:24:00-07:00");
const PHONE = 286, DESKTOP = 1044;

function labels(span, width, zoom = [0, 100]) {
  const chart = echarts.init(null, null, { renderer: "svg", ssr: true, width: width + 56, height: 240 });
  const step = span / 288, data = [];
  for (let t = END - span; t <= END; t += step) data.push([t, 1]);
  chart.setOption({ grid: { left: 42, right: 14 }, yAxis: { show: false },
    dataZoom: [{ type: "inside", start: zoom[0], end: zoom[1] }],
    xAxis: timeAxis(chart, width), series: [{ type: "bar", data }] });
  const svg = chart.renderToSVGString();
  chart.dispose();
  return [...svg.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1]).sort();
}

const cases = {
  "1h": [HOUR, ["1:30", "1:40", "1:50", "2 AM", "2:10", "2:20"],
    ["1:25", "1:30", "1:35", "1:40", "1:45", "1:50", "1:55", "2 AM", "2:05", "2:10", "2:15", "2:20"]],
  "24h, crossing midnight": [24 * HOUR, ["12 PM", "4 AM", "4 PM", "8 AM", "8 PM", "Wed 30"],
    ["10 AM", "10 PM", "12 PM", "2 AM", "2 PM", "4 AM", "4 PM", "6 AM", "6 PM", "8 AM", "8 PM", "Wed 30"]],
  "7d": [7 * 24 * HOUR, ["Fri 25", "Sun 27", "Tue 29"],
    ["Fri 25", "Mon 28", "Sat 26", "Sun 27", "Thu 24", "Tue 29", "Wed 30"]],
  "all": [120 * 24 * HOUR, ["Aug", "Jul", "Sep"], null],
};
for (const [name, [span, phone, desktop]] of Object.entries(cases)) {
  test(`${name}: round ticks, date only where the day changes`, () => {
    assert.deepEqual(labels(span, PHONE), phone);
    if (desktop) assert.deepEqual(labels(span, DESKTOP), desktop);
  });
}

test("all history on a desktop reads as months and days, never times", () => {
  const all = labels(120 * 24 * HOUR, DESKTOP);
  assert.ok(all.length >= 8, all);
  for (const label of all) assert.match(label, /^[A-Z][a-z]{2}( \d+)?$/);
});

test("zooming re-ticks to the visible span, keeping the midnight marker", () => {
  assert.deepEqual(labels(24 * HOUR, PHONE, [85, 95]), ["1 AM", "11 PM", "Wed 30"]);
  const desktop = labels(24 * HOUR, DESKTOP, [85, 95]);
  assert.ok(desktop.includes("Wed 30") && desktop.includes("11:30"), desktop);
});

test("labels adapt to the unit and the span", () => {
  const noon = Date.parse("2026-09-29T12:00:00-07:00");
  assert.equal(timeLabel(noon + 15 * 6e4, "minute", HOUR), "12:15");
  assert.equal(timeLabel(noon, "hour", HOUR), "12 PM");
  assert.equal(timeLabel(Date.parse("2026-09-30T00:00:00-07:00"), "day", 24 * HOUR), "Wed 30");
  assert.equal(timeLabel(Date.parse("2026-09-30T00:00:00-07:00"), "day", 90 * 24 * HOUR), "Sep 30");
  assert.equal(timeLabel(Date.parse("2026-10-01T00:00:00-07:00"), "month", 90 * 24 * HOUR), "Oct");
  assert.equal(timeLabel(Date.parse("2027-01-01T00:00:00-08:00"), "year", 400 * 24 * HOUR), "2027");
});
