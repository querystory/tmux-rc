// Exercise the real Activity observer: leaving through the top must not fold a tall
// chart and clamp the dashboard's scroll position back to its slot.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/m/atlas.js", import.meta.url), "utf8");
const start = source.indexOf("  root._slot ||= ");
const end = source.indexOf("  root._slot.observe(sentinel);", start);
assert.ok(start >= 0 && end > start, "Activity observer moved");

test("Activity unfolds at its slot and stays full when a short screen scrolls past it", () => {
  const scroller = {}, sentinel = {}, folds = [];
  let callback, options, observed;
  const root = { closest: selector => { assert.equal(selector, "#landing"); return scroller; } };
  const sandbox = { root, sentinel, compact: on => folds.push(on), IntersectionObserver: class {
    constructor(fn, opts) { callback = fn; options = opts; }
    observe(node) { observed = node; }
  } };
  vm.runInNewContext(source.slice(start, end) + "root._slot.observe(sentinel);", sandbox);
  assert.equal(options.root, scroller);
  assert.equal(observed, sentinel);
  const entry = (top, isIntersecting) => callback([{ boundingClientRect: { top }, rootBounds: { top: 60, bottom: 340 }, isIntersecting }]);
  entry(600, false); // Before reaching Activity: the bottom strip.
  entry(340, true); // Its slot arrives: full chart.
  entry(100, true);
  entry(20, false); // Above the scroller, still inside the browser viewport.
  entry(-150, false); // The full chart is taller than the visible dashboard.
  entry(600, false); // Scroll back up: the strip returns.
  callback([{ boundingClientRect: { top: 0 }, rootBounds: { top: 0, bottom: 0 }, isIntersecting: false }]); // Hidden dashboard: fold before returning to its top.
  entry(340, true); // A visible zero-height sentinel touching the edge is reached.
  assert.deepEqual(folds, [true, false, false, false, false, true, true, false]);
});
