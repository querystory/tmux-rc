// Deterministic screenshots of /m against the demo fleet (scripts/demo_fleet.py).
//
//   node scripts/screenshots.mjs <out-dir> [tree]     (or: make screenshots)
//
// Boots scripts/demo_server.py from `tree` (default: this checkout) on a free port, then
// shoots SHOTS below. Same tree, same machine => same bytes: the browser clock starts at
// the fleet's NOW, motion is off, the viewport and pixel ratio are fixed, fonts are awaited,
// and the chat socket is a canned stub. Playwright resolves from $PLAYWRIGHT_DIR (the
// Makefile installs it there) so the repo's own package.json stays browser-free.
import { spawn, execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import path from "node:path";

const { chromium } = createRequire(path.join(process.env.PLAYWRIGHT_DIR || ".", "/"))("playwright");
const [out = ".screenshots", tree = process.cwd()] = process.argv.slice(2);
const WIDE = { width: 1440, height: 900 }, PHONE = { width: 390, height: 844 };

// name, viewport, colour scheme, URL hash, and an optional step before the shot.
const SHOTS = [
  ["hero", WIDE, "light", "#pane=%259"],
  ["wide-pane-dark", WIDE, "dark", "#pane=%259"],
  ["wide-needs-you", WIDE, "light", "#pane=%254"],
  ["wide-dashboard", WIDE, "light", "#view=dashboard"],
  ["wide-dashboard-dark", WIDE, "dark", "#view=dashboard"],
  ["wide-chat", WIDE, "light", "#pane=%259", openChat],
  ["mobile-list", PHONE, "light", ""],
  ["mobile-list-dark", PHONE, "dark", ""],
  ["mobile-pane", PHONE, "light", "#pane=%259"],
  ["mobile-needs-you", PHONE, "light", "#pane=%254"],
  ["mobile-menu", PHONE, "dark", "#pane=%2540"],
  ["mobile-terminal", PHONE, "light", "#pane=%259&view=terminal"],
];

// Motion off, and the UI font pinned to what Linux already renders for the app's stack:
// resolving -apple-system / "Segoe UI" (absent here) races, and the loser shifts inline
// icons and timestamps by a pixel from run to run.
const STILL = `body { font-family: "Liberation Sans", sans-serif !important; }
*, *::before, *::after { animation: none !important; transition: none !important;
  caret-color: transparent !important; scroll-behavior: auto !important; scrollbar-width: none !important; }`;

async function openChat(page) {
  await page.click("#chat");
  await page.waitForFunction(() => document.getElementById("voice-status")?.textContent === "Connected");
  await page.fill("#chat-input", "Which panes need me?");
  await page.press("#chat-input", "Enter");
  await page.waitForFunction(() => document.querySelectorAll("#voice-log .voice-entry").length >= 2);
}

// A canned Live Mode socket: answers any typed message with the same summary.
function stubChat(socket) {
  socket.send(JSON.stringify({ type: "status", status: "listening" }));
  socket.onMessage((raw) => {
    const message = JSON.parse(raw);
    if (message.action !== "text") return;
    socket.send(JSON.stringify({ type: "transcript", role: "user", text: message.text }));
    socket.send(JSON.stringify({ type: "transcript", role: "model", text:
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

const python = (...args) => ["uv", ["run", "--directory", tree, "python", ...args]];
const now = Number(execFileSync(...python("-c", "from scripts.demo_fleet import NOW; print(NOW)"), { encoding: "utf8" }));
const port = await freePort();
const server = spawn(...python("-m", "scripts.demo_server", String(port)), { stdio: "inherit" });
const base = `http://127.0.0.1:${port}`;
mkdirSync(out, { recursive: true });
let browser;
try {
  await waitUp(`${base}/api/version`, server);
  browser = await chromium.launch();
  for (const [name, viewport, colorScheme, hash, step] of SHOTS) {
    const context = await browser.newContext({ viewport, colorScheme, deviceScaleFactor: 2,
      reducedMotion: "reduce", locale: "en-US", timezoneId: "UTC", serviceWorkers: "block",
      isMobile: viewport === PHONE, hasTouch: viewport === PHONE });
    // Start the clock at the fleet's NOW and let it run: a clock frozen solid (setFixedTime)
    // starves the app's own Date-based throttles and makes layout racy between runs.
    await context.clock.setSystemTime(now * 1000);
    const page = await context.newPage();
    await page.routeWebSocket(/\/api\/live-mode/, stubChat);
    await page.goto(`${base}/m/${hash}`);
    await page.addStyleTag({ content: STILL });
    await page.waitForSelector(".pane-row", { state: "attached" });
    if (step) await step(page);
    await page.evaluate(() => document.fonts.ready);
    // Long polls never let the network idle, so wait for the pixels instead: shoot until two
    // frames in a row agree (charts and the live frame are still settling before that).
    let last, shot;
    for (let i = 0; i < 20 && !(shot && last?.equals(shot)); i++) {
      last = shot;
      await page.waitForTimeout(500);
      shot = await page.screenshot();
    }
    writeFileSync(path.join(out, `${name}.png`), shot);
    await context.close();
    console.log(`shot ${name}`);
  }
} finally {
  await browser?.close();
  server.kill();
}
