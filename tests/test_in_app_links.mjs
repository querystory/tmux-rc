// The list's rows and the sidebar's places are links into the app: a plain click routes in
// place (and keeps its history mode), a modified or middle click is the browser's, a new tab on
// the same view. Runs app.js's hashFor/inApp/paneLink and the row builders on a stub DOM.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";
import { formatHash, PAGES } from "../web/m/url-state.js";

const read = (file) => readFileSync(new URL(`../web/m/${file}`, import.meta.url), "utf8");
const from = (source, start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const app = read("app.js"), side = read("sidebar.js");
const block = from(app, "const hashFor = ", "function navigate(") + from(app, "function makeRow(", "function updateRow(")
  + from(side, "  function row(card) {", "  // A long question") + from(side, "  function rail(subset, query) {", "  // The hover card");

class Element {
  constructor(tagName) { this.tagName = tagName; this.children = []; this.classList = { toggle() {} }; this.dataset = {}; }
  append(...nodes) { this.children.push(...nodes); }
  after() {}
  setAttribute() {}
  removeAttribute() {}
  querySelector() { return new Element("img"); }
  get firstChild() { return this.children[0]; }
  get lastChild() { return this.children.at(-1); }
}
function harness({ filter = [], q = "" } = {}) {
  const navigated = [], made = [];
  const ctx = { sort: () => "updated", outside: () => false, asks: () => false, active: () => null, logos: {}, html() {}, badge: () => "" };
  const sandbox = { formatHash, PAGES, filter, scope: { tools: [] }, accounts: { key: () => [] }, $: () => ({ value: q }), navigate: (id) => navigated.push(id),
    licon: () => "", document: { createElement: (tag) => new Element(tag) }, ctx, addAnswers() {}, root: {}, groups: () => [{ panes: [{ pane_id: "%7" }] }],
    reconcile: (root, items, key, make, update) => items.forEach((p) => { const node = make(); update(node, p); made.push(node); }),
    markEnding() {}, subagentCount: () => 0, sessionOf: () => "", paneName: () => "", isRunning: () => false, markWorking() {} };
  vm.createContext(sandbox);
  vm.runInContext(`${block}; ctx.paneLink = paneLink; this.paneLink = paneLink; this.makeRow = makeRow; this.row = row; this.rail = rail;`, sandbox);
  return { sandbox, navigated, made };
}
const click = (a, options = {}) => { const e = { button: 0, prevented: false, preventDefault() { this.prevented = true; }, ...options }; a.onclick(e); return e.prevented; };

test("a pane's link is the hash navigate would write, filters and search included", () => {
  const { sandbox } = harness({ filter: ["working"], q: "api" });
  const a = new Element("a");
  sandbox.paneLink(a, "%1");
  assert.equal(a.href, "#filter=working&q=api&pane=%251");
  assert.equal(a.href.slice(1), formatHash({ pane: "%1", filter: ["working"], q: "api" }));
});

test("a plain click routes in place; a modified or middle click is left to the browser", () => {
  const { sandbox, navigated } = harness();
  const a = new Element("a");
  sandbox.paneLink(a, "%1");
  assert.equal(click(a), true);
  assert.deepEqual(navigated, ["%1"]);
  for (const options of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) assert.equal(click(a, options), false, JSON.stringify(options));
  assert.deepEqual(navigated, ["%1"]);
});

test("the phone row, the sidebar row and card, and the rail icon are anchors to their pane", () => {
  const { sandbox, made } = harness();
  assert.equal(sandbox.makeRow({ pane_id: "%1" }).tagName, "a");
  for (const card of [false, true]) assert.equal(sandbox.row(card).firstChild.tagName, "a");
  sandbox.rail([], "");
  assert.equal(made[0].tagName, "a");
  assert.equal(made[0].href, "#pane=%257");
});
