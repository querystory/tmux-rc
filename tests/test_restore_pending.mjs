import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// restore.js imports by its served path; point that at the file and load it as a module.
const model = new URL("../web/m/pane-model.js", import.meta.url).href;
const source = readFileSync(new URL("../web/m/restore.js", import.meta.url), "utf8").replace("/m/pane-model.js", model);

// Just enough DOM for render(): it counts the card's redraws.
let draws = 0;
const node = () => ({ dataset: {}, classList: { add() {} }, setAttribute() {}, addEventListener(t, fn) { this[t] = fn; },
  querySelectorAll: () => [], querySelector: () => null, contains: () => false,
  set innerHTML(_) { draws++; } });
globalThis.document = { createElement: node, activeElement: null };
const { setupRestore } = await import(`data:text/javascript,${encodeURIComponent(source)}`);

const offer = { server: "s1", died_at: 0, sessions: [{ name: "demo", windows: [{ id: "@1", index: 0 }] }],
  windows: { "@1": { name: "w", panes: [{ index: 0, default: true, cwd: "/tmp", tool: "shell" }] } } };

test("a refresh during the restore POST neither redraws nor closes the view", async () => {
  const answers = [];
  const request = (url, init) => new Promise(resolve => answers.push({ url, post: !!init, resolve }));
  let closes = 0;
  const view = setupRestore({ mount: node(), request, logos: {}, licon: () => "", notice() {}, running: () => true,
    open() {}, opened() {}, closed: () => closes++ });
  const first = view.refresh();
  answers.shift().resolve({ candidate: offer });
  await first;

  const early = view.refresh(); // in flight when the tap lands
  const button = { dataset: { act: "restore" }, classList: { add() {} } };
  const tap = view.card.click({ target: { closest: () => button } });
  const [get, post] = answers.splice(0);
  assert.equal(post.post, true);
  draws = 0;
  get.resolve({ candidate: null }); // the server already spent the offer
  await early;
  assert.deepEqual({ draws, closes }, { draws: 0, closes: 0 }); // the tap discarded it
  view.refresh();
  assert.deepEqual({ draws, closes, asked: answers.length }, { draws: 0, closes: 0, asked: 0 });

  post.resolve({});
  await new Promise(r => setImmediate(r));
  answers.shift().resolve({ candidate: null });
  await tap;
  assert.equal(closes, 1); // the refresh after the POST reconciles
});
