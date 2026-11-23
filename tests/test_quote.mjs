import assert from "node:assert/strict";
import { test } from "node:test";
import { quoteOf } from "../web/m/composer.js";

test("a selection is quoted as one line, ready for the remark after it", () => {
  assert.equal(quoteOf("  Ship on\n\tFriday.  "), '"Ship on Friday." ');
});

test("nothing selected quotes nothing", () => {
  for (const none of ["", " \n ", undefined, 7]) assert.equal(quoteOf(none), "");
});

test("no control or invisible character reaches the terminal", () => {
  assert.equal(quoteOf("rm\u001b[2J -rf\u200b\u0007 x"), '"rm [2J -rf x" ');
});

test("a long selection is cut, on whole characters", () => {
  assert.equal(quoteOf("ab \u{1F600}\u{1F600} cd", 4), '"ab \u{1F600}…" ');
  assert.equal(quoteOf("x".repeat(400)).length, 280 + 4);
});
