// The fleet chart (atlas-charts.js) draws only what changed: ECharts redraws the whole chart
// on setOption and again on resize, and the dashboard holds one per session, so a surface
// shown again or a poll that left the history alone must cost no render at all.
import assert from "node:assert/strict";
import { test } from "node:test";

const calls = { setOption: 0, resize: 0 };
const node = () => ({ clientWidth: 300, clientHeight: 120, dataset: {}, style: {}, setAttribute() {}, replaceChildren() {}, addEventListener() {} });
const theme = { light: false };
globalThis.window = globalThis;
globalThis.document = { createElement: node, documentElement: { classList: { contains: () => theme.light } } };
globalThis.ResizeObserver = globalThis.MutationObserver = class { observe() {} disconnect() {} };
globalThis.addEventListener = () => {};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "#000" });
globalThis.echarts = { init: () => ({ setOption() { calls.setOption++; }, resize() { calls.resize++; }, on() {}, dispatchAction() {}, dispose() {}, getDom() {} }) };
const { fleetChart } = await import("../web/m/atlas-charts.js");

const states = ["Idle", "Running", "Needs you", "Done", "Compacting", "External"];
const rows = () => [{ t: 0, n: [1, 2, 0, 0, 0, 0], ma: null }, { t: 60000, n: [0, 3, 0, 0, 0, 0], ma: 2.5 }];
const data = (r, extra) => ({ rows: r, states, step: 60000, unit: "Panes", goal: 3, average: "1d", zoomKey: "k", ...extra });
const painted = () => new Promise((resolve) => setTimeout(resolve)); // paint awaits the library

test("the same rows again draw nothing; a new size alone resizes; new rows or a theme draw again", async () => {
  const chart = fleetChart(true), r = rows();
  chart.update(data(r)); await painted();
  assert.deepEqual(calls, { setOption: 1, resize: 0 }, "born at its size: one render, no resize");
  chart.update(data(r)); await painted();
  assert.deepEqual(calls, { setOption: 1, resize: 0 }, "a poll with the rows it drew");
  chart.el.clientWidth = 360;
  chart.update(data(r)); await painted();
  assert.deepEqual(calls, { setOption: 1, resize: 1 }, "a new size is a resize, not a rebuild");
  chart.update(data(rows())); await painted();
  assert.deepEqual(calls, { setOption: 2, resize: 1 }, "new rows draw again, and the size held");
  theme.light = true;
  chart.update(data(rows())); await painted(); // the same call a theme change makes
  assert.deepEqual(calls, { setOption: 3, resize: 1 });
  chart.compact = true; // the dashboard's pinned strip: axes go, so it is another drawing
  chart.update(data(rows())); await painted();
  assert.deepEqual(calls, { setOption: 4, resize: 1 });
});

test("a hidden chart draws nothing and takes no size", async () => {
  const chart = fleetChart(false), before = { ...calls };
  chart.el.clientWidth = 0;
  chart.update(data(rows())); await painted();
  assert.deepEqual(calls, before);
});
