// On a phone a dashboard card's page carries the account picker and pill in its head bar, and
// a page without a card (Scratch) keeps the pill in the find row, below its bar. Runs app.js's placePill() alone, against
// stand-in nodes.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/m/app.js", import.meta.url), "utf8");
const start = source.indexOf("function placePill()");
const block = source.slice(start, source.indexOf("\n}\n", start) + 3);

class Node {
  constructor(name) { this.name = name; this.kids = []; this.parentElement = null; }
  append(kid) { kid.parentElement?.kids.splice(kid.parentElement.kids.indexOf(kid), 1); kid.parentElement = this; this.kids.push(kid); }
  sibling(step) { return this.parentElement?.kids[this.parentElement.kids.indexOf(this) + step] ?? null; }
  get previousElementSibling() { return this.sibling(-1); }
  get nextElementSibling() { return this.sibling(1); }
  insert(kid, step) { kid.parentElement?.kids.splice(kid.parentElement.kids.indexOf(kid), 1); kid.parentElement = this.parentElement; this.parentElement.kids.splice(this.parentElement.kids.indexOf(this) + step, 0, kid); }
  before(kid) { this.insert(kid, 0); }
  after(kid) { this.insert(kid, 1); }
}

function place(page) {
  const root = new Node("root"), nodes = {};
  for (const id of ["dash-page", "find", "new-window", "heat-card", "picker", "pill"]) root.append(nodes[id] = new Node(id));
  const bar = new Node("landing-head"), scope = new Node("fg-scope");
  nodes["dash-page"].append(bar);
  nodes["dash-page"].querySelector = () => bar;
  const sandbox = { page, dashboard: true, WIDE: { matches: false }, CARDS: { heat: "heat-card", usage: "usage-panel", loops: "loops-card", graph: "graph-card" },
    $: (id) => nodes[id], renderSidebar: { picker: nodes.picker }, accounts: { pill: nodes.pill }, document: { querySelector: () => null },
    graphView: { querySelector: () => scope } };
  vm.createContext(sandbox);
  vm.runInContext(`${block}; placePill();`, sandbox);
  return { bar, scope, ...nodes };
}

test("the graph's page seats the picker and the pill in the view's own strip, which its full screen shows", () => {
  const { bar, scope, picker, pill } = place("graph");
  assert.equal(picker.parentElement, scope);
  assert.equal(pill.previousElementSibling, picker);
  assert.equal(bar.kids.length, 0);
});

test("a card's page puts the picker and the pill in its head bar", () => {
  for (const page of ["heat", "usage", "loops"]) {
    const { bar, picker, pill } = place(page);
    assert.equal(picker.parentElement, bar);
    assert.equal(pill.previousElementSibling, picker);
  }
});

test("a page without a card keeps the pill in the find row, out of its head", () => {
  const { bar, find, pill } = place("scratch");
  assert.equal(pill.previousElementSibling, find);
  assert.equal(bar.kids.length, 0);
});
