// The demo fleet in a browser, shared by scripts/screenshots.mjs and scripts/mock.mjs.
//
// startDemo(tree, env) boots scripts/demo_server.py from `tree` on a free port, `env` added
// to its environment, and a headless Chromium whose renders are reproducible: the clock
// starts at the fleet's NOW, motion is off, the pixel ratio is fixed, fonts are awaited and
// the chat socket is a canned stub.
// Playwright resolves from $PLAYWRIGHT_DIR (default: the Makefile's install dir) so the
// repo's own package.json stays browser-free.
import { spawn, execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import { homedir } from "node:os";
import path from "node:path";

const playwright = process.env.PLAYWRIGHT_DIR || path.join(homedir(), ".cache/tmux-rc/playwright");
export const { chromium } = createRequire(path.join(playwright, "/"))("playwright");
export const WIDE = { width: 1440, height: 900 }, PHONE = { width: 390, height: 844 };

// Motion off, and the UI font pinned to what Linux already renders for the app's stack:
// resolving -apple-system / "Segoe UI" (absent here) races, and the loser shifts inline
// icons and timestamps by a pixel from run to run.
export const STILL = `/* demo-still: a mock snapshot drops this */ body { font-family: "Liberation Sans", sans-serif !important; }
*, *::before, *::after { animation: none !important; transition: none !important;
  caret-color: transparent !important; scroll-behavior: auto !important; scrollbar-width: none !important; }`;

// Type `ask` into the chat and wait for the canned reply (and `until`, a frame after it).
export function chat(ask, until) {
  return async (page) => {
  await page.click("#chat");
  await page.waitForFunction(() => document.getElementById("voice-status")?.textContent === "Connected");
  await page.fill("#chat-input", ask);
  await page.press("#chat-input", "Enter");
  await page.waitForFunction(() => document.querySelectorAll("#voice-log .voice-entry").length >= 2);
  if (until) await page.waitForSelector(until); // a frame after the transcripts, e.g. open_pane
  };
}

// Frames a beat apart, as live. Sent in one burst, rows lay out in one frame or several at
// random, and the text of one lands a pixel higher or lower.
export function pace(socket, frames) {
  frames.forEach((frame, i) => setTimeout(() => socket.send(JSON.stringify(frame)), i * 150));
}

// A canned Live Mode socket: a window named is offered with an Open button (open_pane), a
// "tell" or a "Resume..." waits on a consent card, anything else gets the same summary.
function stubChat(socket) {
  socket.send(JSON.stringify({ type: "status", status: "listening" }));
  socket.onMessage((raw) => {
    const { action, text } = JSON.parse(raw);
    if (action !== "text") return;
    const done = { type: "turn_complete" };
    pace(socket, [{ type: "transcript", role: "user", text }, ...text.startsWith("Resume") ? [{ type: "propose", id: "p1",
      text: "Resume checkout flake hunt", session: { tool: "codex", cwd: "~/src/example-org/storefront",
        last_active: "2026-06-10T09:12:00Z", id: "5f3a9c1e" } }]
      : /^Tell/.test(text) ? [{ type: "propose", id: "p1", pane_id: "%9",
        text: "Send to e2e triage · claude in ci: From the user (via text): rerun it headed" }, done]
      : /window/.test(text) ? [{ type: "open_pane", pane_id: "%9", label: "e2e triage · claude in ci" }, // before the text, as live
        { type: "transcript", role: "model", text: "**e2e triage** is rerunning checkout.spec with tracing on." }, done]
      : [{ type: "transcript", role: "model", text:
        "Two panes need you:\n\n- **refunds** asks whether to merge PRs #413 and #414\n" +
        "- **preview deploy** hands you `make test` to run before it merges\n\n" +
        "Others are running, including the traced checkout.spec rerun in **e2e triage**." }, done]]);
  });
}

const freePort = () => new Promise((resolve) => {
  const probe = createServer().listen(0, "127.0.0.1", () => {
    const { port } = probe.address();
    probe.close(() => resolve(port));
  });
});

async function waitUp(url, server) {
  for (let i = 0; i < 300 && server.exitCode === null; i++) {
    try { if ((await fetch(url)).ok) return; } catch { /* not listening yet */ }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`demo server did not come up at ${url}`);
}

// Font rendering settled by flags, not by whatever fontconfig answers: on a fresh CI runner
// one shot came out with three text baselines (and an icon aligned to one) a pixel lower
// than every other render of the same page. Unhinted metrics and grayscale antialiasing
// leave nothing for that answer to change.
// The fake media flags give Live Mode's voice a synthetic microphone, with no prompt.
export const launch = () => chromium.launch({ args: ["--disable-lcd-text", "--font-render-hinting=none",
  "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"] });

// A new context per page, so nothing (storage, routes, the clock) leaks between shots. A
// viewport under 700px wide is a phone: mobile, with touch.
export async function newPage(browser, viewport, colorScheme, now, options) {
  const phone = viewport.width < 700;
  const context = await browser.newContext({ viewport, colorScheme, deviceScaleFactor: 2,
    reducedMotion: "reduce", locale: "en-US", timezoneId: "UTC", serviceWorkers: "block",
    isMobile: phone, hasTouch: phone, ...options });
  // Start the clock at the fleet's NOW and let it run: a clock frozen solid (setFixedTime)
  // starves the app's own Date-based throttles and makes layout racy between runs.
  if (now) await context.clock.setSystemTime(now * 1000);
  context.setDefaultTimeout(10000);
  // A count of fetches started and ended, NaN while one is out, leaving out the long polls that
  // never end: settle() wants it unchanged across a frame. A slow fetch (the dashboard's
  // history) otherwise lands after two frames already agreed.
  const page = await context.newPage(), fetching = new Set();
  let events = 0;
  const track = (add) => (r) => { if (["fetch", "xhr"].includes(r.resourceType()) && !/\/api\/state|\/live\b/.test(r.url())) { events++; fetching[add ? "add" : "delete"](r); } };
  page.on("request", track(true));
  for (const end of ["requestfinished", "requestfailed"]) page.on(end, track(false));
  page.network = () => fetching.size ? NaN : events;
  return page;
}

// Long polls never let the network idle, so wait for the pixels instead: shoot until two
// frames in a row agree with no other fetch out or done between them (charts and the live
// frame are still settling before that). Never hand back a frame that did not settle: that
// is how nondeterminism slips in unseen.
export async function settle(page, name, shoot = () => page.screenshot()) {
  let shot, still = false;
  for (let i = 0; i < 20 && !still; i++) {
    const last = shot, network = page.network?.();
    await page.waitForTimeout(500);
    shot = await shoot();
    still = !!last?.equals(shot) && network === page.network?.(); // NaN: a fetch was out
  }
  if (!still) throw new Error(`${name}: still changing after 10s`);
  return shot;
}

export async function startDemo(tree = process.cwd(), env = {}) {
  const python = (...args) => ["uv", ["run", "--directory", tree, "python", ...args]];
  const now = Number(execFileSync(...python("-c", "from scripts.demo_fleet import NOW; print(NOW)"), { encoding: "utf8" }));
  const port = await freePort();
  const server = spawn(...python("-m", "scripts.demo_server", String(port)), { stdio: "inherit", env: { ...process.env, ...env } });
  const base = `http://127.0.0.1:${port}`;
  let browser;
  const close = async () => { await browser?.close(); server.kill(); };
  try {
    await waitUp(`${base}/api/version`, server);
    browser = await launch();
  } catch (e) { await close(); throw e; }
  return {
    base, close,
    // /m at `hash`, once its first state has arrived. Waiting on the API rather than a
    // selector means a UI that renames its markup still renders instead of failing. `socket`
    // stands in for Live Mode, and `prepare` adds routes before the page loads.
    async open(viewport, colorScheme, hash = "", socket = stubChat, prepare) {
      const page = await newPage(browser, viewport, colorScheme, now);
      await page.routeWebSocket(/\/api\/live-mode/, socket);
      // History a beat late, as over a network. Served at once, its pages land in one frame
      // or several at random, and the heatmap view's grid paints a pixel higher or lower.
      await page.route("**/api/history*", async (route) => {
        await new Promise((r) => setTimeout(r, 200));
        await route.continue().catch(() => {}); // the page closed meanwhile
      });
      await prepare?.(page);
      const state = page.waitForResponse((r) => r.url().includes("/api/state"));
      await page.goto(`${base}/m/${hash}`);
      await page.addStyleTag({ content: STILL });
      await state;
      return page;
    },
  };
}
