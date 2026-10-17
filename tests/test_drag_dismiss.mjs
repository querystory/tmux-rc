import assert from "node:assert/strict";
import { test } from "node:test";
import { dismisses, FLICK } from "../web/m/drag-dismiss.js";

test("a slow drag closes only past a third of the sheet", () => {
  assert.equal(dismisses(99, 300, 0), false);
  assert.equal(dismisses(101, 300, 0), true);
});

test("a flick toward closing closes from anywhere", () => {
  assert.equal(dismisses(20, 300, FLICK + 0.1), true);
});

test("a flick back cancels a drag past the threshold", () => {
  assert.equal(dismisses(200, 300, -(FLICK + 0.1)), false);
});
