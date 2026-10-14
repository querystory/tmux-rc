import assert from "node:assert/strict";
import { test } from "node:test";
import { shellWords, withWay } from "../web/m/accounts.js";

// The sign-in line is checked as you type with the server's own reading (shlex.split), so
// the sheet neither refuses a line the server takes nor passes one it refuses.
test("the sign-in line splits into words as shlex.split does", () => {
  for (const [line, words] of [
    ['CODEX_HOME="~/.codex-work" codex login', ["CODEX_HOME=~/.codex-work", "codex", "login"]],
    ["codex login -c 'a=b c'", ["codex", "login", "-c", "a=b c"]],
    ["x'y z'w a\\ b", ["xy zw", "a b"]],
    ['"a\\"b" "a\\b" \'a\\b\' "" "\\$x"', ['a"b', "a\\b", "a\\b", "", "\\$x"]],
    ["  ", []],
    ["codex\u00a0login", ["codex\u00a0login"]], // shlex splits on ASCII whitespace only
  ]) assert.deepEqual(shellWords(line), words, line);
  for (const open of ["codex 'login", '"codex', "codex\\"]) assert.equal(shellWords(open), null, open);
});

test("picking a way toggles only a bare --device-auth word", () => {
  assert.equal(withWay("codex login --device-auth -c 'a  b'", false), "codex login -c 'a  b'");
  assert.equal(withWay("codex login -c 'x --device-auth y'", true), "codex login -c 'x --device-auth y' --device-auth");
  assert.equal(withWay("codex login -c 'x --device-auth y'", false), "codex login -c 'x --device-auth y'");
  assert.equal(withWay("codex login --device-auth", true), "codex login --device-auth");
  assert.equal(withWay('codex login "--device-auth" -v', false), "codex login -v"); // the same word, quoted
});
