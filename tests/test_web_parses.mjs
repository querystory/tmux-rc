// Every script the daemon serves must parse as an ES module. Most of web/ is never
// imported by a node test, so a syntax error there would otherwise pass `make test`
// and only show up as a phone app that fails to boot.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { globSync, readFileSync } from "node:fs";
import { test } from "node:test";

const web = new URL("../web/", import.meta.url).pathname;

test("every web/ script parses as an ES module", () => {
  const files = globSync("**/*.{js,mjs}", { cwd: web });
  assert.ok(files.includes("m/app.js"), `no scripts found under ${web}`);
  for (const f of files) {
    const r = spawnSync(process.execPath, ["--input-type=module", "--check"],
      { input: readFileSync(web + f), encoding: "utf8" });
    assert.equal(r.status, 0, `web/${f}\n${r.stderr}`);
  }
});
