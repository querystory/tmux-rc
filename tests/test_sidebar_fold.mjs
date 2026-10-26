// A section's chevron folds it, with or without a search: a search opens every section,
// but a fold tapped during it holds until the search changes. Runs sidebar.js's groups()
// alone, with every pane in Needs you.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/m/sidebar.js", import.meta.url), "utf8");
const start = source.indexOf("const searched = ");
const block = source.slice(start, source.indexOf("\n}\n", start) + 3);

function sidebar() {
  const sandbox = { prefs: { fold: {}, cards: {} }, STATES: [], needsYou: () => true, rank: () => 0, newestFirst: () => 0, bySession: () => false };
  vm.createContext(sandbox);
  vm.runInContext(`${block}; this.groups = groups;`, sandbox);
  const need = (query) => sandbox.groups([{ pane_id: "%1" }], query, "updated")[0];
  const tap = (query) => { const g = need(query); g.folds[g.id] = g.open; };
  return { need, tap };
}

test("the chevron folds and unfolds a section without a search", () => {
  const { need, tap } = sidebar();
  tap("");
  assert.equal(need("").open, false);
  tap("");
  assert.equal(need("").open, true);
});

test("a search opens a folded section, and a tap during it folds it again until the search changes", () => {
  const { need, tap } = sidebar();
  tap("");
  assert.equal(need("tmu").open, true);
  tap("tmu");
  assert.equal(need("tmu").open, false);
  assert.equal(need("tmux").open, true);
  assert.equal(need("").open, false); // the fold made without a search is still there
});
