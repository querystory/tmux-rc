import assert from "node:assert/strict";
import { test } from "node:test";
import { sendIntent } from "../web/m/pane-model.js";

const panes = [{ pane_id: "%1", tool: "claude" }, { pane_id: "%2", tool: null }, { pane_id: "%3", tool: "codex" }];
const ask = (text, pane = "%1", intent = "send") => sendIntent({ intent, pane, text }, panes);

test("a line to an agent pane is asked, trimmed", () => {
  assert.deepEqual(ask(" merge #12 "), { pane: panes[0], text: "merge #12" });
  assert.equal(ask("merge #12", "%3").pane, panes[2]);
  assert.equal(ask("é".repeat(500)).text.length, 500);
});

test("anything else is dropped", () => {
  assert.equal(ask("merge #12", "%2"), null); // a plain shell would run it
  assert.equal(ask("merge #12", "%9"), null); // not in the feed
  assert.equal(ask("merge #12", "%1", "keys"), null);
  for (const bad of ["merge\n#12", "rm -rf /\r", "\u001b[2J", "ok‮", "a\u0000", "é".repeat(501), "  ", 12, null, undefined])
    assert.equal(ask(bad), null, JSON.stringify(bad));
  assert.equal(sendIntent(undefined, panes), null);
});
