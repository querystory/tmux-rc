import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

// The inline <head> script of web/m/index.html: the theme pick and the boot watchdog.
const html = readFileSync(new URL("../web/m/index.html", import.meta.url), "utf8");
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];

class Script {}
class Link {}

// Boots the watchdog with `last` as the previous start's failure, fails a module load, then
// answers each /api/version probe from `probes` (true: up, false: down). Returns the probe
// delays and whether the page reloaded.
async function boot(probes, last = null) {
  const timers = [], listeners = {}, store = new Map(last ? [["tmuxrc-boot-failure", last]] : []);
  let reloads = 0;
  const el = { content: "", classList: { toggle() {}, add() {}, contains: () => false } };
  const context = {
    HTMLScriptElement: Script, HTMLLinkElement: Link,
    document: { documentElement: el, querySelector: () => el, hidden: false, addEventListener() {} },
    localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) },
    matchMedia: () => ({ matches: false }),
    navigator: { onLine: true },
    location: { reload: () => reloads++ },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    setTimeout: (fn, ms) => timers.push({ fn, ms }),
    fetch: async () => {
      if (!probes.shift()) throw new TypeError("Load failed");
      return { ok: true };
    },
  };
  vm.runInNewContext(source, context);
  timers.shift(); // the 10s not-booted timer
  listeners.error({ target: Object.assign(new Script(), { src: "/v/1/m/app.js" }) });
  const delays = [];
  while (timers.length) {
    const { fn, ms } = timers.shift();
    delays.push(ms);
    fn();
    await new Promise((r) => setImmediate(r));
  }
  return { delays, reloads };
}

test("a boot that failed mid-restart reloads once the daemon answers", async () => {
  assert.deepEqual(await boot([false, false, true]), { delays: [2000, 4000, 8000], reloads: 1 });
});

test("a daemon down past the backoff leaves the Reload button", async () => {
  assert.deepEqual(await boot([false, false, false, false, true]), { delays: [2000, 4000, 8000, 16000], reloads: 0 });
});

test("a second failure with the daemon up is a real error: no loop", async () => {
  assert.deepEqual(await boot([true], "failed to load /v/1/m/app.js"), { delays: [2000], reloads: 0 });
});

test("a second failure that saw the daemon down still heals", async () => {
  assert.deepEqual(await boot([false, true], "failed to load /v/1/m/app.js"), { delays: [2000, 4000], reloads: 1 });
});
