import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/sw.js", import.meta.url), "utf8");

// Runs sw.js against fake windows and taps a notification for `url`: what each window saw.
async function tap(url, windows) {
  const listeners = {}, opened = [];
  const clients = {
    claim: async () => {},
    matchAll: async () => windows,
    openWindow: async (target) => { opened.push(target); },
  };
  const self = { location: { origin: "https://host" }, addEventListener: (type, fn) => { listeners[type] = fn; } };
  vm.runInNewContext(source, { self, clients, caches: {}, URL });
  let done;
  listeners.notificationclick({
    action: "",
    notification: { close() {}, data: { url } },
    waitUntil: (promise) => { done = promise; },
  });
  await done;
  return opened;
}

const fakeWindow = (url, { navigate = async () => {} } = {}) => {
  const w = { url, posted: [], navigated: [], focused: 0 };
  w.postMessage = (message) => w.posted.push(structuredClone(message)); // into this realm, as a real post
  w.navigate = async (target) => { w.navigated.push(target); return navigate(target); };
  w.focus = async () => { w.focused += 1; };
  return w;
};

test("an open app is told which pane to open and focused, never navigated", async () => {
  const app = fakeWindow("https://host/m#pane=%251");
  const opened = await tap("/m#pane=%257&from=push", [fakeWindow("https://host/docs/"), app]);
  assert.deepEqual(app.posted, [{ open: "https://host/m#pane=%257&from=push" }]);
  assert.deepEqual(app.navigated, []);
  assert.equal(app.focused, 1);
  assert.deepEqual(opened, []);
});

test("a window that is not the app is navigated; a failure falls back to a new window", async () => {
  const docs = fakeWindow("https://host/docs/", { navigate: async () => { throw new TypeError("no"); } });
  const opened = await tap("/m#pane=%257", [docs]);
  assert.deepEqual(docs.navigated, ["https://host/m#pane=%257"]);
  assert.deepEqual(opened, ["https://host/m#pane=%257"]);
});

test("a cold start opens the app at the pane", async () => {
  assert.deepEqual(await tap("/m#pane=%257", []), ["https://host/m#pane=%257"]);
});
