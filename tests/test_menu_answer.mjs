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
  // Claude Code's "❯ 1. Yes / 2. No" box ignores a bare y/n: Yes/No rows get digits too,
  // whatever cursor fields the classifier attached to the menu.
  const proceed = { ...menu("Yes", "No"), selected: 0, keymap: { next: "Down", prev: "Up", select: "Enter" } };
  assert.deepEqual(answerBody(proceed, "Yes", 0), digit("1"));
  assert.deepEqual(answerBody(proceed, "No", 1), digit("2"));
  // Two options that are not yes/no still get their digit, not the typed label.
  assert.deepEqual(answerBody(menu("Retry", "Abort"), "Abort", 1), digit("2"));
  // Row 10 has no one-key shortcut: Codex commits the "1" of "10" as row 1.
  const long = menu(...Array.from({ length: 12 }, (_, i) => `Model ${i + 1}`));
  assert.deepEqual(answerBody(long, "Model 9", 8), digit("9"));
  assert.equal(answerBody(long, "Model 10", 9), null);
});

test("a typed reply keeps its Enter", () => {
  assert.deepEqual(answerBody({ answer_style: "text", options: ["Ship it"] }, "Ship it", 0),
    { keys: "Ship it", enter: true, literal: true });
});
