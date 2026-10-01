import assert from "node:assert/strict";
import { test } from "node:test";
import { Composer, bindAttach } from "../web/m/composer.js";

const png = (name) => ({ name, type: "image/png", size: 10 });
// A composer with the real attachMany but a stub attach, so no DOM is needed.
const fakeComposer = (max) => {
  const c = { files: new Map(), max, errors: [], error: (m) => c.errors.push(m), saveCaret() {},
    attach(file) { c.files.set(file.name, file); } };
  c.attachMany = Composer.prototype.attachMany;
  return c;
};
const pick = (composer, files) => {
  const input = { files, value: "x", click() {} };
  const button = {};
  bindAttach(button, input, () => composer);
  button.onclick(); input.onchange();
  return input;
};

test("a multi-select attaches every file in order and resets the input", () => {
  const c = fakeComposer(Infinity);
  const input = pick(c, [png("a"), png("b"), png("c")]);
  assert.deepEqual([...c.files.keys()], ["a", "b", "c"]);
  assert.equal(input.value, "");
  pick(c, [png("a")]); // the same file again still attaches
  assert.deepEqual([...c.files.keys()], ["a", "b", "c"]);
});

test("an over-limit selection attaches what fits and reports the rest", () => {
  const c = fakeComposer(2);
  pick(c, [png("a"), png("b"), png("c")]);
  assert.deepEqual([...c.files.keys()], ["a", "b"]);
  assert.equal(c.errors.length, 1);
});
