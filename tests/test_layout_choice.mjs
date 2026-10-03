// The wide Layout picker's choice is global: it must survive a pane switch and a reload.
// Runs app.js's layout-state block and the picker's change handler alone, against stub
// storage, media queries and navigation.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/m/app.js", import.meta.url), "utf8");
const slice = (from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const block = slice("const WIDE = ", "const reviewing = ");
const picker = slice('$("review-layout").onchange', "reviewDivider.onpointerdown");

function boot(storage, wide = true) {
  const elements = { detail: { getBoundingClientRect: () => ({ width: 1600, height: 900 }) }, "review-layout": {} };
  const sandbox = {
    view: "summary", active: "%1", navigations: [],
    matchMedia: () => ({ matches: wide }),
    localStorage: { getItem: (key) => storage[key] ?? null, setItem: (key, value) => { storage[key] = value; } },
    document: { getElementById: (id) => elements[id] },
    $: (id) => elements[id],
  };
  sandbox.navigate = (id, next) => sandbox.navigations.push([id, next]);
  vm.createContext(sandbox);
  vm.runInContext(`${block}; ${picker}; this.layout = effectiveLayout; this.opens = () => defaultView();`, sandbox);
  sandbox.pick = (value) => elements["review-layout"].onchange({ target: { value } });
  return sandbox;
}
const saved = (value) => ({ "tmuxrc-review-layout": value });

test("picking Terminal is saved, and every pane opened after a reload lands on it", () => {
  const storage = {};
  boot(storage).pick("terminal");
  assert.equal(storage["tmuxrc-review-layout"], "terminal");
  const app = boot(storage);
  assert.equal(app.layout(), "focus");
  assert.equal(app.opens(), "terminal");
});

test("route() opens a pane URL without a view on the chosen tab", () => {
  assert.match(source, /parseHash\(location\.hash, defaultView\(\)\)/);
});

test("Overview, including its older 'focus' spelling, opens panes on the overview", () => {
  for (const value of ["summary", "focus"]) {
    const app = boot(saved(value));
    assert.equal(app.layout(), "focus");
    assert.equal(app.opens(), "summary");
  }
});

test("splits keep their layout, and phones keep their own tabs", () => {
  assert.equal(boot(saved("stack")).layout(), "stack");
  assert.equal(boot(saved("side")).opens(), "summary");
  const phone = {};
  boot(phone, false).pick("terminal");
  assert.deepEqual(phone, {});
  assert.equal(boot(saved("terminal"), false).opens(), "summary");
  assert.equal(boot(saved("terminal"), false).layout(), "focus");
});
