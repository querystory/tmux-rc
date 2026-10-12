// The demo fleet in a browser, shared by scripts/screenshots.mjs and scripts/mock.mjs.
//
// startDemo(tree) boots scripts/demo_server.py from `tree` on a free port and a headless
// Chromium whose renders are reproducible: the clock starts at the fleet's NOW, motion is
// off, the pixel ratio is fixed, fonts are awaited and the chat socket is a canned stub.
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
export const STILL = `body { font-family: "Liberation Sans", sans-serif !important; }
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

// A canned Live Mode socket: a window named is offered with an Open button (open_pane), a
// "tell" or a "Resume..." waits on a consent card, anything else gets the same summary.
function stubChat(socket) {
  socket.send(JSON.stringify({ type: "status", status: "listening" }));
  socket.onMessage((raw) => {
    const message = JSON.parse(raw);
    if (message.action !== "text") return;
    socket.send(JSON.stringify({ type: "transcript", role: "user", text: message.text }));
    if (message.text.startsWith("Resume")) return socket.send(JSON.stringify({ type: "propose", id: "p1",
      text: "Resume checkout flake hunt", session: { tool: "codex", cwd: "~/src/example-org/storefront",
        last_active: "2026-06-10T09:12:00Z", id: "5f3a9c1e" } }));
    if (/^Tell/.test(message.text)) socket.send(JSON.stringify({ type: "propose", id: "p1", pane_id: "%9",
      text: 'Send to window 1 "e2e triage": From the user (via text): rerun it headed' }));
    else if (/window/.test(message.text)) {
      socket.send(JSON.stringify({ type: "open_pane", pane_id: "%9", label: 'window 1 "e2e triage"' })); // before the text, as live
      socket.send(JSON.stringify({ type: "transcript", role: "model", text: "Window 1 is **e2e triage**, rerunning checkout.spec with tracing on." }));
    } else socket.send(JSON.stringify({ type: "transcript", role: "model", text:
      "Four panes need you:\n\n- **api contract diff** asks whether to bump the public API to v3\n" +
      "- **flaky test hunter** wants you to pick a suite to quarantine\n" +
      "- **alert tuning** asks to silence the disk alert on build-02\n" +
      "- **dependency audit** wants approval for three breaking upgrades\n\n" +
      "Seven others are running, including the traced checkout.spec rerun in **e2e triage**." }));
    socket.send(JSON.stringify({ type: "turn_complete" }));
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
export const launch = () => chromium.launch({ args: ["--disable-lcd-text", "--font-render-hinting=none"] });

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
  return context.newPage();
}

// Long polls never let the network idle, so wait for the pixels instead: shoot until two
// frames in a row agree (charts and the live frame are still settling before that). Never
// hand back a frame that did not settle: that is how nondeterminism slips in unseen.
export async function settle(page, name, shoot = () => page.screenshot()) {
  let last, shot;
  for (let i = 0; i < 20 && !(shot && last?.equals(shot)); i++) {
    last = shot;
    await page.waitForTimeout(500);
    shot = await shoot();
  }
  if (!last?.equals(shot)) throw new Error(`${name}: still changing after 10s`);
  return shot;
}

export async function startDemo(tree = process.cwd()) {
  const python = (...args) => ["uv", ["run", "--directory", tree, "python", ...args]];
  const now = Number(execFileSync(...python("-c", "from scripts.demo_fleet import NOW; print(NOW)"), { encoding: "utf8" }));
  const port = await freePort();
  const server = spawn(...python("-m", "scripts.demo_server", String(port)), { stdio: "inherit" });
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
    // selector means a UI that renames its markup still renders instead of failing.
    async open(viewport, colorScheme, hash = "") {
      const page = await newPage(browser, viewport, colorScheme, now);
      await page.routeWebSocket(/\/api\/live-mode/, stubChat);
      const state = page.waitForResponse((r) => r.url().includes("/api/state"));
      await page.goto(`${base}/m/${hash}`);
      await (await page.addStyleTag({ content: STILL })).evaluate((el) => { el.id = "demo-still"; }); // a mock drops it
      await state;
      return page;
    },
  };
}
