import assert from "node:assert/strict";
import { test } from "node:test";
import { besideHeld, renderCaptureLines } from "../web/terminal.js";

// A URL a TUI wrapped with its own newline, cut at a chunk edge: each side, rendered against
// the other, links the whole href, as one capture of the same rows does.
test("a hard-wrapped URL across a chunk edge stays one link", () => {
  const above = "a".repeat(42) + " https://ex.com/very/long/pa";
  const below = "th/continues/here/" + "x".repeat(41) + "\nshort\n" + "z".repeat(71);
  const one = renderCaptureLines(`${above}\n${below}`, { color: true });
  assert.match(one[1], /href="https:\/\/ex.com\/very\/long\/path\/continues/);
  const gap = besideHeld(below, above, true, false);
  assert.deepEqual(gap.lines, one);
  assert.equal(gap.raw, `${above}\n${below}`);
  const up = besideHeld(above, below, false, false);
  assert.deepEqual(up.lines, one.slice(0, 2));
  assert.equal(up.raw, `${above}\n${below.split("\n")[0]}`);
});
