import assert from "node:assert/strict";
import { test } from "node:test";
import { formatHash, historyMode, parseHash } from "../web/m/url-state.js";

test("every addressable state round-trips", () => {
  for (const state of [
    { pane: "%12", view: "terminal", filter: "attention", sort: "session" },
    { pane: "%3", view: "summary", filter: "running" },
    { dashboard: true, filter: "recent" },
    {},
  ]) {
    const hash = formatHash(state);
    assert.equal(formatHash(parseHash(hash)), hash);
  }
  assert.equal(formatHash({ pane: "%12", view: "terminal" }), "pane=%2512&view=terminal");
  assert.equal(formatHash({}), "");
  assert.equal(parseHash("#pane=%2512").pane, "%12");
});

test("unknown fields and bad values are ignored", () => {
  const state = parseHash("#from=push&pane=%254&filter=bogus&sort=weird&view=nope&x=1");
  assert.deepEqual(state, { pane: "%4", view: "summary", dashboard: false, filter: "all", sort: "updated", compose: false });
  assert.equal(parseHash("#pane=%254&compose=1").compose, true);
  assert.equal(formatHash(parseHash("#pane=%254&from=push&compose=1")), "pane=%254");
  assert.equal(parseHash("").pane, null);
  assert.equal(parseHash(undefined).filter, "all");
});

test("a pane hash without a view opens on the chosen tab; an explicit one wins", () => {
  assert.equal(parseHash("#pane=%251", "terminal").view, "terminal");
  assert.equal(parseHash("#pane=%251&view=terminal").view, "terminal");
  assert.equal(parseHash("#pane=%251").view, "summary");
});

test("a dashboard view only exists without a pane", () => {
  assert.equal(parseHash("#view=dashboard").dashboard, true);
  assert.equal(parseHash("#pane=%251&view=dashboard").dashboard, false);
  assert.equal(formatHash({ pane: "%1", dashboard: true }), "pane=%251");
});

test("old desktop hashes still land", () => {
  assert.equal(parseHash("#/pane/%251").pane, "%1");
  assert.equal(parseHash("#/pane/%").pane, "%");
  assert.equal(parseHash("#/list/waiting").filter, "attention");
  assert.equal(parseHash("#/list/running").filter, "running");
  assert.equal(parseHash("#/list/nonsense").filter, "all");
});

test("history: screens push, in-place changes replace", () => {
  assert.equal(historyMode("", "pane=%251"), "push");
  assert.equal(historyMode("pane=%251", "pane=%252"), "push");
  assert.equal(historyMode("pane=%251", ""), "push");
  assert.equal(historyMode("", "view=dashboard"), "push");
  assert.equal(historyMode("pane=%251", "pane=%251&view=terminal"), "replace");
  assert.equal(historyMode("", "filter=running"), "replace");
  assert.equal(historyMode("filter=running", "filter=running&sort=session"), "replace");
  assert.equal(historyMode("#/pane/%251", "pane=%251"), "replace");
});

test("stillOnPane understands old-format hashes", async () => {
  const { stillOnPane } = await import("../web/m/pane-model.js");
  assert.equal(stillOnPane("#/pane/%251", "%1"), true);
  assert.equal(stillOnPane("#pane=%251", "%1"), true);
  assert.equal(stillOnPane("#pane=%252", "%1"), false);
  assert.equal(stillOnPane("", "%1"), false);
});
