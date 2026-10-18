import assert from "node:assert/strict";
import { test } from "node:test";
import { formatHash, historyMode, parseHash, scratchUrl } from "../web/m/url-state.js";

test("every addressable state round-trips", () => {
  for (const state of [
    { pane: "%12", view: "terminal", filter: ["attention"] },
    { pane: "%12", filter: ["working"], acct: ["claude-work"] },
    { filter: ["attention", "working", "finished", "idle"], acct: ["claude-work", "claude-personal"] },
    { dashboard: true, acct: ["side-project"] },
    { pane: "%12", acct: ["claude-work"], q: "api fix & more" },
    { pane: "%3", view: "summary", filter: ["working"] },
    { dashboard: true, filter: ["finished"] },
    { dashboard: true, page: "usage" },
    { dashboard: true, page: "loops" },
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
  const state = parseHash("#from=push&pane=%254&filter=bogus&view=nope&x=1");
  assert.deepEqual(state, { pane: "%4", view: "summary", dashboard: false, page: null, filter: [], acct: [], agent: [], q: "", compose: false, open: null });
  assert.equal(parseHash("#pane=%254&compose=1").compose, true);
  assert.equal(formatHash(parseHash("#pane=%254&from=push&compose=1")), "pane=%254");
  // A "Ready" push names the scratch page to open (app.js checks it with scratchUrl), once.
  assert.equal(parseHash("#view=scratch&open=%2Fscratch%2Fwip%2F").open, "/scratch/wip/");
  assert.equal(formatHash(parseHash("#view=scratch&open=%2Fscratch%2Fwip%2F")), "view=scratch");
  assert.equal(parseHash("").pane, null);
  assert.deepEqual(parseHash(undefined).filter, []);
});

test("the filters are comma lists: a union within each, old single values still parse", () => {
  assert.equal(formatHash({ filter: ["working", "finished"], acct: ["claude-work", "claude-personal"] }), "filter=working,finished&acct=claude-work,claude-personal");
  assert.deepEqual(parseHash("#filter=finished,working&acct=claude-work,claude-personal"), { ...parseHash(""), filter: ["working", "finished"], acct: ["claude-work", "claude-personal"] });
  assert.deepEqual(parseHash("#filter=working,bogus,working,").filter, ["working"]);
  assert.deepEqual(parseHash("#filter=attention&acct=work"), { ...parseHash(""), filter: ["attention"], acct: ["work"] });
  assert.deepEqual(parseHash("#acct=work,work").acct, ["work"]);
  // A key with its own comma (or %) is one item, however the list is joined.
  const odd = { acct: ["team,west", "100%", "/x/.claude"] };
  assert.deepEqual(parseHash(formatHash(odd)).acct, odd.acct);
  assert.equal(formatHash({ acct: ["a", "b"] }), "acct=a,b");
  // An old single-value link, as URLSearchParams wrote it: its escaped comma is not a divider.
  assert.deepEqual(parseHash("#acct=team%2Cwest").acct, ["team,west"]);
  assert.deepEqual(parseHash("#acct=my+work&filter=running").acct, ["my work"]);
});

test("old filter names still land: running is Working, recent is Just finished", () => {
  assert.deepEqual(parseHash("#filter=running").filter, ["working"]);
  assert.deepEqual(parseHash("#filter=recent").filter, ["finished"]);
  assert.deepEqual(parseHash("#filter=recent,running,attention").filter, ["attention", "working", "finished"]);
  assert.equal(formatHash(parseHash("#filter=running,recent")), "filter=working,finished");
});

test("a pane hash without a view opens on the chosen tab; an explicit one wins", () => {
  assert.equal(parseHash("#pane=%251", "terminal").view, "terminal");
  assert.equal(parseHash("#pane=%251&view=terminal").view, "terminal");
  assert.equal(parseHash("#pane=%251&view=summary", "terminal").view, "summary");
  assert.equal(parseHash("#pane=%251").view, "summary");
});

test("a dashboard view only exists without a pane", () => {
  assert.equal(parseHash("#view=dashboard").dashboard, true);
  assert.equal(parseHash("#pane=%251&view=dashboard").dashboard, false);
  assert.equal(formatHash({ pane: "%1", dashboard: true }), "pane=%251");
  // A card's own view is a page of the dashboard, and a screen of its own for Back.
  assert.equal(parseHash("#view=heat").dashboard, true);
  assert.equal(parseHash("#view=usage").page, "usage");
  assert.equal(parseHash("#pane=%251&view=heat").page, null);
  assert.equal(historyMode("view=dashboard", "view=heat"), "push");
  assert.equal(historyMode("view=heat", "view=loops"), "push");
});

test("old desktop hashes still land", () => {
  assert.equal(parseHash("#/pane/%251").pane, "%1");
  assert.equal(parseHash("#/pane/%").pane, "%");
  assert.deepEqual(parseHash("#/list/waiting").filter, ["attention"]);
  assert.deepEqual(parseHash("#/list/running").filter, ["working"]);
  assert.deepEqual(parseHash("#/list/nonsense").filter, []);
});

test("the account filter rides along: in the URL only when set, and a change replaces", () => {
  assert.equal(formatHash({ filter: ["working"], acct: ["claude-work"] }), "filter=working&acct=claude-work");
  assert.equal(formatHash({ acct: [] }), "");
  assert.deepEqual(parseHash("#acct=work").acct, ["work"]);
  assert.deepEqual(parseHash("#acct=").acct, []);
  assert.equal(historyMode("filter=running", "filter=running&acct=claude-work"), "replace");
  assert.equal(historyMode("pane=%251&acct=work", "pane=%251"), "replace");
});

test("the agent filter rides along: a list, in the URL only when set, and a change replaces", () => {
  assert.equal(formatHash({ filter: ["working"], agent: ["claude", "codex"] }), "filter=working&agent=claude,codex");
  assert.equal(formatHash({ agent: [] }), "");
  assert.deepEqual(parseHash("#agent=codex,codex,shell").agent, ["codex", "shell"]);
  assert.equal(historyMode("view=dashboard", "view=dashboard&agent=claude"), "replace");
});

test("the search rides along too: trimmed, absent when blank, and a change replaces", () => {
  assert.equal(formatHash({ acct: ["claude-work"], q: " api " }), "acct=claude-work&q=api");
  assert.equal(formatHash({ q: "   " }), "");
  assert.equal(parseHash("#q=a%26b%20c").q, "a&b c");
  assert.equal(parseHash("#q=%20").q, "");
  assert.equal(historyMode("q=ap", "q=api"), "replace");
  assert.equal(historyMode("pane=%251&q=api", "pane=%251"), "replace");
});

test("history: screens push, in-place changes replace", () => {
  assert.equal(historyMode("", "pane=%251"), "push");
  assert.equal(historyMode("pane=%251", "pane=%252"), "push");
  assert.equal(historyMode("pane=%251", ""), "push");
  assert.equal(historyMode("", "view=dashboard"), "push");
  assert.equal(historyMode("pane=%251", "pane=%251&view=terminal"), "replace");
  assert.equal(historyMode("", "filter=running"), "replace");
  assert.equal(historyMode("filter=working", "filter=working,finished"), "replace");
  assert.equal(historyMode("#/pane/%251", "pane=%251"), "replace");
});

test("stillOnPane understands old-format hashes", async () => {
  const { stillOnPane } = await import("../web/m/pane-model.js");
  assert.equal(stillOnPane("#/pane/%251", "%1"), true);
  assert.equal(stillOnPane("#pane=%251", "%1"), true);
  assert.equal(stillOnPane("#pane=%252", "%1"), false);
  assert.equal(stillOnPane("", "%1"), false);
});

test("the gallery opens only a Scratch page on the app's own origin", () => {
  const base = "https://rc.example/m/#view=scratch";
  assert.equal(scratchUrl("https://rc.example/scratch/demo/", base), "https://rc.example/scratch/demo/");
  assert.equal(scratchUrl("/scratch/a/b.html?x=1#y", base), "https://rc.example/scratch/a/b.html?x=1#y");
  assert.equal(scratchUrl("/scratch/a/%2e./b/", base), "https://rc.example/scratch/b/");
  for (const href of ["https://evil.example/scratch/x/", "//evil.example/scratch/", "javascript:alert(1)", "data:text/html,x",
    "/m/", "/api/panes", "/scratch", "/scratch/../m/", "/scratch/%2e%2e/api/x", "/scratch\\..\\m/", "/scratch/..%2fm/", "/scratch/..%5Cm/",
    "/docs/mocks/x/", "", undefined, null, 1, { href: "/scratch/x/" }, "http://[bad"])
    assert.equal(scratchUrl(href, base), null, String(href));
});
