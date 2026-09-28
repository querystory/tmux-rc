import assert from "node:assert/strict";
import { test } from "node:test";
import { matchesSearch, matchesFilter } from "../web/m/pane-model.js";

const pane = {
  pane_id: "%7", session: "work", title: "Address review", activity: "idle",
  window_index: "2", tool: "codex", prs: [{ repo: "querystory/qs-app", number: 4955 }],
};

test("sidebar finds accumulated PRs after the number disappears from visible text", () => {
  for (const query of ["4955", "#4955", "PR 4955", "PR #4955", "qs-app#4955",
    "qs-app 4955", "qs-app #4955", "qs-app pr 4955", "QUERYSTORY/QS-APP#4955",
    "querystory/qs-app PR #4955", "  qs-app   PR 4955  ",
    "https://github.com/querystory/qs-app/pull/4955"]) {
    assert.equal(matchesSearch(pane, query), true, query);
  }
  assert.equal(matchesSearch(pane, "4956"), false);
  assert.equal(matchesSearch({ ...pane, prs: [] }, "4955"), false);
});

test("all sessions for a PR remain searchable, including idle panes in All", () => {
  const other = { ...pane, pane_id: "%8", prs: [...pane.prs, { repo: "org/other", number: 42 }] };
  assert.deepEqual([pane, other].filter((p) => matchesFilter(p, "all") && matchesSearch(p, "4955")), [pane, other]);
  assert.equal(matchesSearch(other, "other#42"), true);
  assert.equal(matchesSearch(pane, "other#42"), false);
});

test("existing text searches and panes without PR metadata still work", () => {
  for (const prs of [undefined, null, [], "invalid", [null, {}, { repo: 1, number: 2 }]]) {
    for (const query of ["", "address review", "work", "%7", "codex", "Window 2"]) {
      assert.equal(matchesSearch({ ...pane, prs }, query), true, query);
    }
    assert.equal(matchesSearch({ ...pane, prs }, "4955"), false);
  }
});
