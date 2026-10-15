import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

// The inline <head> script of web/m/index.html: the theme pick and the boot watchdog.
const html = readFileSync(new URL("../web/m/index.html", import.meta.url), "utf8");
const source = html.split("<script>")[1].split("</script>")[0];

class Script {}
class Link {}

// Boots the watchdog with `last` as the previous start's failure, fails to load `failed`,
// then answers each /api/version probe from `probes` (true: up, false: down, "stall": no
// answer until its deadline, which passes before the next probe); app.js boots
// before the probes when `appBoots`. Returns the probe delays and whether the page reloaded.
async function boot(probes, { last = null, failed = new Script(), appBoots = false, storage = true } = {}) {
  const timers = [], deadlines = [], listeners = {}, store = new Map(last ? [["tmuxrc-boot-failure", last]] : []);
  let reloads = 0;
  const classes = new Set();
  const el = { content: "", classList: { toggle() {}, add: (c) => classes.add(c) },
    matches: () => classes.has("booted") && !classes.has("unstyled") }; // the one selector asked
  const context = {
    HTMLScriptElement: Script, HTMLLinkElement: Link,
    document: { documentElement: el, querySelector: () => el, hidden: false, addEventListener() {} },
    localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => { if (!storage) throw new Error("blocked"); store.set(k, v); }, removeItem: (k) => store.delete(k) },
    matchMedia: () => ({ matches: false }),
    navigator: { onLine: true },
    location: { reload: () => reloads++ },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    setTimeout: (fn, ms) => timers.push({ fn, ms }),
    AbortSignal: { timeout: () => { const c = new AbortController(); deadlines.push(() => c.abort()); return c.signal; } },
    fetch: async (url, { signal }) => {
      const up = probes.shift();
      if (up === "stall") await new Promise((_, reject) => signal?.addEventListener("abort", reject));
      if (!up) throw new TypeError("Load failed");
      return { ok: true };
    },
  };
  vm.runInNewContext(source, context);
  timers.shift(); // the 10s not-booted timer
  listeners.error({ target: Object.assign(failed, { src: "/v/1/m/app.js", href: "/m/style.css" }) });
  if (appBoots) classes.add("booted");
  const delays = [];
  while (timers.length) {
    const { fn, ms } = timers.shift();
    delays.push(ms);
    fn();
    await new Promise((r) => setImmediate(r));
    deadlines.splice(0).forEach((abort) => abort());
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
  assert.deepEqual(await boot([true], { last: "failed to load /v/1/m/app.js" }), { delays: [2000], reloads: 0 });
});

test("a second failure that saw the daemon down still heals", async () => {
  assert.deepEqual(await boot([false, true], { last: "failed to load /v/1/m/app.js" }), { delays: [2000, 4000], reloads: 1 });
});

test("a page that booted late is left alone", async () => {
  assert.deepEqual(await boot([true], { appBoots: true }), { delays: [2000], reloads: 0 });
});

test("a page that booted without its stylesheet still heals", async () => {
  const failed = Object.assign(new Link(), { rel: "stylesheet" });
  assert.deepEqual(await boot([true], { failed, appBoots: true }), { delays: [2000], reloads: 1 });
});

test("a failure that can't be recorded reloads only after seeing the daemon down", async () => {
  assert.deepEqual(await boot([true], { storage: false }), { delays: [2000], reloads: 0 });
  assert.deepEqual(await boot([false, true], { storage: false }), { delays: [2000, 4000], reloads: 1 });
});

test("a probe stalled in the tunnel gives way to the next", async () => {
  assert.deepEqual(await boot(["stall", true]), { delays: [2000, 4000], reloads: 1 });
});
