import assert from "node:assert/strict";
import { test } from "node:test";
import { matchesSearch, matchesFilter } from "../web/m/pane-model.js";
import { paneLinks } from "../web/pr-links.js";

test("PR titles are primary, with the repository and number beneath", () => {
  const [link] = paneLinks({ prs: [{ repo: "querystory/tmux-rc", number: 245,
    title: "Track PR associations" }] });
  assert.equal(link.text, "Track PR associations");
  assert.equal(link.detail, "querystory/tmux-rc#245");
  assert.equal(link.href, "https://github.com/querystory/tmux-rc/pull/245");
});

const pane = {
  pane_id: "%7", session: "work", title: "Address review", activity: "idle",
  window_index: "2", tool: "codex", prs: [{ repo: "querystory/qs-app", number: 4955 }],
};

test("transient link metadata cannot spoof the destination host", () => {
  assert.deepEqual(paneLinks({ links: [{ href: "https://evil.example/", text: "Preview",
    detail: "github.com", extra: "untrusted" }] }),
  [{ href: "https://evil.example/", text: "Preview" }]);
});

test("tracked PRs stay tappable without a current-frame link and preserve other links", () => {
  const pr = { href: "https://github.com/querystory/qs-app/pull/4955", text: "querystory/qs-app#4955" };
  assert.deepEqual(paneLinks(pane), [pr]);
  const preview = { href: "https://example.com/preview", text: "Preview" };
  assert.deepEqual(paneLinks({ ...pane, prs: [...pane.prs, ...pane.prs], links: [
    { href: pr.href + "/", text: "Open PR" }, preview,
  ] }), [pr, preview]);
  const other = { repo: "other/qs-app", number: 4955 };
  assert.equal(paneLinks({ prs: [...pane.prs, other] }).length, 2);
});

test("PR destinations reject malformed metadata and unsafe links", () => {
  assert.deepEqual(paneLinks(null), []);
  assert.deepEqual(paneLinks({ prs: "invalid", links: "invalid" }), []);
  assert.deepEqual(paneLinks({ prs: [null, {}, { repo: "../bad", number: 1 },
    { repo: "evil.test/@foo/bar", number: 1 }, { repo: "a/b", number: -1 },
    { repo: "a/b", number: "1" }, { repo: "a/b", number: Number.MAX_SAFE_INTEGER + 1 }],
    links: [null, {}, { href: "javascript:alert(1)" }] }), []);
});

test("transient links keep their own cap without hiding tracked PRs", () => {
  const prs = Array.from({ length: 8 }, (_, i) => ({ repo: "org/repo", number: i + 1 }));
  const links = [null, { href: "javascript:bad" },
    { href: "https://github.com/org/repo/pull/1" },
    ...Array.from({ length: 20 }, (_, i) => ({ href: `https://example.com/${i}` }))];
  const result = paneLinks({ prs, links });
  assert.equal(result.length, 11);
  assert.equal(result[7].href, "https://github.com/org/repo/pull/8");
  assert.equal(result[10].href, "https://example.com/2");
});

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
