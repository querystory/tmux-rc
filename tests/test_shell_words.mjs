import assert from "node:assert/strict";
import { test } from "node:test";
import { shellWords } from "../web/m/accounts.js";

// The sign-in line is checked as you type with the server's own reading (shlex.split), so
// the sheet neither refuses a line the server takes nor passes one it refuses.
test("the sign-in line splits into words as shlex.split does", () => {
  for (const [line, words] of [
    ['CODEX_HOME="~/.codex-work" codex login', ["CODEX_HOME=~/.codex-work", "codex", "login"]],
    ["codex login -c 'a=b c'", ["codex", "login", "-c", "a=b c"]],
    ["x'y z'w a\\ b", ["xy zw", "a b"]],
    ['"a\\"b" "a\\b" \'a\\b\' ""', ['a"b', "a\\b", "a\\b", ""]],
    ["  ", []],
  ]) assert.deepEqual(shellWords(line), words, line);
  for (const open of ["codex 'login", '"codex', "codex\\"]) assert.equal(shellWords(open), null, open);
});
