import assert from "node:assert/strict";
import { test } from "node:test";
import { overscroll, overscrollState, RESIST_PX, NOTCH_PX, IDLE_MS } from "../web/m/overscroll.js";

test("reaching the top only stretches until the resistance is spent", () => {
  const s = overscrollState();
  assert.equal(overscroll(s, -(RESIST_PX - 1), 0), 0);
  assert.equal(s.pull, RESIST_PX - 1);
  // Only the excess past the resistance scrolls the app.
  assert.equal(overscroll(s, -(1 + 2 * NOTCH_PX), 10), 2);
  assert.equal(s.net, 2);
});

test("a finger's small steps add up past the resistance", () => {
  const s = overscrollState();
  let sent = 0;
  for (let t = 0; t < 30; t++) sent += overscroll(s, -15, t * 30);
  assert.equal(sent, Math.trunc((30 * 15 - RESIST_PX) / NOTCH_PX));
});

test("a pause springs the resistance back", () => {
  const s = overscrollState();
  overscroll(s, -(RESIST_PX - 1), 0);
  assert.equal(overscroll(s, -NOTCH_PX, IDLE_MS + 1), 0);
  assert.equal(s.pull, NOTCH_PX);
});

test("once the app is scrolled up, wheel notches go straight through, even after a pause", () => {
  const s = overscrollState();
  overscroll(s, -(RESIST_PX + NOTCH_PX), 0);
  assert.equal(overscroll(s, -NOTCH_PX / 2, IDLE_MS * 4), 0); // fractions accumulate
  assert.equal(overscroll(s, -NOTCH_PX / 2, IDLE_MS * 4 + 10), 1);
  assert.equal(s.net, 2);
});

test("scrolling down returns the app home, never past it, and re-arms the resistance", () => {
  const s = overscrollState();
  overscroll(s, -(RESIST_PX + 3 * NOTCH_PX), 0);
  assert.equal(overscroll(s, 10 * NOTCH_PX, 10), -3);
  assert.deepEqual([s.net, s.pull, s.rem], [0, 0, 0]);
  assert.equal(overscroll(s, -NOTCH_PX, 20), 0);
});

test("scrolling down at the bottom with the app home sends nothing and drops a half pull", () => {
  const s = overscrollState();
  assert.equal(overscroll(s, 500, 0), 0);
  overscroll(s, -RESIST_PX / 2, 10);
  assert.equal(overscroll(s, 1, 20), 0);
  assert.equal(s.pull, 0);
});
