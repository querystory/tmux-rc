// The wide Layout picker's choice is global: it must survive a pane switch and a reload.
// Runs app.js's layout-state block alone, against stub storage and media queries.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/m/app.js", import.meta.url), "utf8");
const block = source.slice(source.indexOf("const WIDE = "), source.indexOf("const reviewing = "));

function boot(saved, wide = true) {
  const sandbox = {
    view: "summary",
    matchMedia: () => ({ matches: wide }),
    localStorage: { getItem: () => saved },
    document: { getElementById: () => ({ getBoundingClientRect: () => ({ width: 1600, height: 900 }) }) },
  };
  vm.createContext(sandbox);
  vm.runInContext(`${block}; this.layout = effectiveLayout; this.opens = () => defaultView();`, sandbox);
  return sandbox;
}

test("a saved Terminal choice opens every pane on the terminal, in the focus layout", () => {
  const app = boot("terminal");
  assert.equal(app.layout(), "focus");
  assert.equal(app.opens(), "terminal");
});

test("Overview, including its older 'focus' spelling, opens panes on the overview", () => {
  for (const saved of ["summary", "focus"]) {
    const app = boot(saved);
    assert.equal(app.layout(), "focus");
    assert.equal(app.opens(), "summary");
  }
});

test("splits keep their layout, and phones keep their own tabs", () => {
  assert.equal(boot("stack").layout(), "stack");
  assert.equal(boot("side").opens(), "summary");
  assert.equal(boot("terminal", false).opens(), "summary");
  assert.equal(boot("terminal", false).layout(), "focus");
});
