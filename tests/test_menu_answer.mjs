import assert from "node:assert/strict";
import { test } from "node:test";
import { answerBody } from "../web/cursor-pick.js";

const menu = (...options) => ({ answer_style: "menu", options });
const digit = (keys) => ({ keys, enter: false, literal: true });

test("a menu tap sends only its shortcut, never Enter", () => {
  // Codex commits on the digit: an Enter would confirm the reasoning menu that follows.
  const models = menu("GPT-6.1-Sol (current)", "GPT-6-Astra", "GPT-6-Sol", "GPT-6-Luna");
  assert.deepEqual(answerBody(models, "GPT-6-Astra", 1), digit("2"));
  const approval = menu("Yes", "Yes, and don't ask again", "No, and tell Codex what to do");
  assert.deepEqual(answerBody(approval, "Yes", 0), digit("1"));
  assert.deepEqual(answerBody(menu("Yes", "No"), "No", 1), digit("n"));
  // Two options that are not yes/no still get their digit, not the typed label.
  assert.deepEqual(answerBody(menu("Retry", "Abort"), "Abort", 1), digit("2"));
  assert.deepEqual(answerBody(menu("Yes", "Abort"), "Yes", 0), digit("1"));
  assert.deepEqual(answerBody(menu("Retry", "No"), "No", 1), digit("2"));
  assert.deepEqual(answerBody(menu("Yes", null), "Yes", 0), digit("1"));
});

test("a typed reply keeps its Enter", () => {
  assert.deepEqual(answerBody({ answer_style: "text", options: ["Ship it"] }, "Ship it", 0),
    { keys: "Ship it", enter: true, literal: true });
});

test("non-yes/no menus retain their original option indices", () => {
  const question = menu("Yes", "Other", "No");
  assert.deepEqual(answerBody(question, "No", 2), digit("3"));
  assert.deepEqual(answerBody(menu("YES", "NO"), "YES", 0), digit("y"));
});

test("explicit displayed numbers override position; multi-digit shortcuts are refused", () => {
  const numbered = menu("0. Dismiss", "1. Retry");
  assert.deepEqual(answerBody(numbered, "0. Dismiss", 0), digit("0"));
  assert.deepEqual(answerBody(numbered, "1. Retry", 1), digit("1"));
  assert.equal(answerBody(menu("10. Dangerous"), "10. Dangerous", 0), null);
  assert.equal(answerBody(menu(...Array(10).fill("Choice")), "Choice", 9), null);
});
