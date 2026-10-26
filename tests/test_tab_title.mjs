import assert from "node:assert/strict";
import { test } from "node:test";
import { tabTitle } from "../web/m/pane-model.js";
import { parseHash } from "../web/m/url-state.js";

const panes = [{ pane_id: "%7", title: "deploy fix", session: "work", window_index: 1 }];
const title = (hash, count = 0, host = "kestrel", wide = false) => {
  const s = parseHash(hash);
  return tabTitle({ ...s, dashboard: s.dashboard || (wide && !s.pane) }, panes, count, host);
};

test("each view: the count, the subject, then the host", () => {
  assert.equal(title("#pane=%257", 2), "(2) deploy fix · kestrel");
  assert.equal(title("#pane=%257"), "deploy fix · kestrel");
  assert.equal(title("#pane=%259"), "Loading pane · kestrel");
  assert.equal(title(""), "Sessions · kestrel");
  assert.equal(title("", 0, "kestrel", true), "Dashboard · kestrel"); // a wide screen's bare URL is the dashboard
  assert.equal(title("#filter=attention", 1), "(1) Needs you · kestrel");
  assert.equal(title("#view=dashboard", 3), "(3) Dashboard · kestrel");
  assert.equal(title("#view=graph"), "Graph · kestrel");
  assert.equal(title("#view=scratch"), "Scratch · kestrel");
  assert.equal(title("#view=usage"), "Plan usage · kestrel");
  assert.equal(title("#view=loops"), "Open loops · kestrel");
  assert.equal(title("#view=heat"), "Running by hour · kestrel");
  assert.equal(title("#view=restore"), "Restore · kestrel");
  assert.equal(title("#view=graph", 0, ""), "Graph"); // before the first state answers
});
