import assert from "node:assert/strict";
import { test } from "node:test";
import { needsYou, tabIconKey } from "../web/m/pane-model.js";
import { parseHash } from "../web/m/url-state.js";

const panes = [{ pane_id: "%7", activity: "running" }, { pane_id: "%8", activity: "waiting" }, { pane_id: "%9", activity: "idle" }];
const key = (hash, away = false, list = panes) => tabIconKey(parseHash(hash), list, needsYou, away);

test("each surface's glyph, and the state of its own subject", () => {
  assert.equal(key(""), "home:needs"); // the home shows a dot while anything needs you
  assert.equal(key("", false, [panes[0]]), "home:");
  assert.equal(key("#filter=attention"), "home:needs");
  assert.equal(key("#pane=%258"), "terminal:needs");
  assert.equal(key("#pane=%257"), "terminal:working");
  assert.equal(key("#pane=%259"), "terminal:");
  assert.equal(key("#pane=%259", true), "terminal:done"); // finished while the tab was hidden
  assert.equal(key("#pane=%2599"), "terminal:"); // still loading
  assert.equal(key("#view=dashboard"), "dashboard:"); // the fleet's views: the title's count covers it
  assert.equal(key("#view=heat"), "activity:");
  assert.equal(key("#view=usage"), "gauge:");
  assert.equal(key("#view=loops"), "listTodo:");
  assert.equal(key("#view=graph"), "network:");
  assert.equal(key("#view=restore"), "history:");
  assert.equal(key("#view=scratch"), "files:");
});
