import assert from "node:assert/strict";
import { test } from "node:test";
import { keptRows, renderCaptureLines } from "../web/terminal.js";

const kept = (text) => keptRows(renderCaptureLines(text, { color: true }));

test("a run of blank rows shows as its first one, wherever it is", () => {
  assert.deepEqual(kept("\n\n\n\nprompt"), [0, 4]);
  assert.deepEqual(kept("head\n\n\n\nprompt"), [0, 1, 4]);
  assert.deepEqual(kept("a\nb\n\nc"), [0, 1, 2, 3]);
});

test("an empty row is blank whatever markup it carries; a row of spaces is drawn", () => {
  assert.deepEqual(kept("\x1b[31mred\n\n\n\x1b[0mend"), [0, 1, 3]);
  assert.deepEqual(kept("\x1b[2m\n\n\x1b[0mend"), [0, 2]);
  assert.deepEqual(kept("\x1b[41m  \n  \x1b[0m\n\x1b[4m \n \x1b[0m"), [0, 1, 2, 3]);
});
