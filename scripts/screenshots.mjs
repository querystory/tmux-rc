// Deterministic screenshots of /m against the demo fleet (scripts/demo_fleet.py).
//
//   node scripts/screenshots.mjs <out-dir> [tree]     (or: make screenshots)
//
// Boots scripts/demo_server.py from `tree` (default: this checkout) on a free port, then
// shoots SHOTS below. Same tree, same machine => same bytes: the browser clock starts at
// the fleet's NOW, motion is off, the viewport and pixel ratio are fixed, fonts are awaited,
// and the chat socket is a canned stub. Playwright resolves from $PLAYWRIGHT_DIR (default:
// the Makefile's install dir) so the repo's own package.json stays browser-free.
import { spawn, execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { homedir } from "node:os";
import path from "node:path";

const playwright = process.env.PLAYWRIGHT_DIR || path.join(homedir(), ".cache/tmux-rc/playwright");
const { chromium } = createRequire(path.join(playwright, "/"))("playwright");
const [out = ".screenshots", tree = process.cwd()] = process.argv.slice(2);
const WIDE = { width: 1440, height: 900 }, PHONE = { width: 390, height: 844 };

// name, viewport, colour scheme, URL hash, and an optional step before the shot.
const SHOTS = [
  ["hero", WIDE, "light", "#pane=%259"],
  ["wide-pane-dark", WIDE, "dark", "#pane=%259"],
  ["wide-needs-you", WIDE, "light", "#pane=%254"],
  ["wide-dashboard", WIDE, "light", "#view=dashboard"],
  ["wide-dashboard-dark", WIDE, "dark", "#view=dashboard"],
  ["wide-chat", WIDE, "light", "#pane=%259", chat("Which panes need me?")],
  ["wide-subagents", WIDE, "dark", "#pane=%259", showSubagents],
  ["mobile-list", PHONE, "light", ""],
  ["mobile-list-dark", PHONE, "dark", ""],
  ["mobile-pane", PHONE, "light", "#pane=%259"],
  ["mobile-needs-you", PHONE, "light", "#pane=%254"],
  ["mobile-menu", PHONE, "dark", "#pane=%2540"],
  ["mobile-terminal", PHONE, "light", "#pane=%259&view=terminal"],
  ["mobile-chat-open", PHONE, "light", "", chat("Let's go back to window 1")],
];

// Motion off, and the UI font pinned to what Linux already renders for the app's stack:
// resolving -apple-system / "Segoe UI" (absent here) races, and the loser shifts inline
// icons and timestamps by a pixel from run to run.
const STILL = `body { font-family: "Liberation Sans", sans-serif !important; }
*, *::before, *::after { animation: none !important; transition: none !important;
  caret-color: transparent !important; scroll-behavior: auto !important; scrollbar-width: none !important; }`;

// The Sub-agents switch is on by default; show its lines under activity cards too.
async function showSubagents(page) {
  await page.locator(".sb-group", { hasText: "Working" }).locator(".sb-icon").click();
  await page.locator(".sb-card", { hasText: "terraform plan review" }).scrollIntoViewIfNeeded();
}

function chat(ask) { // hoisted: SHOTS above calls it
  return async (page) => {
  await page.click("#chat");
  await page.waitForFunction(() => document.getElementById("voice-status")?.textContent === "Connected");
  await page.fill("#chat-input", ask);
  await page.press("#chat-input", "Enter");
  await page.waitForFunction(() => document.querySelectorAll("#voice-log .voice-entry").length >= 2);
  };
}

// A canned Live Mode socket: a window named is offered with an Open button (open_pane),
// anything else gets the same summary.
function stubChat(socket) {
  socket.send(JSON.stringify({ type: "status", status: "listening" }));
  socket.onMessage((raw) => {
    const message = JSON.parse(raw);
    if (message.action !== "text") return;
    socket.send(JSON.stringify({ type: "transcript", role: "user", text: message.text }));
    if (/window/.test(message.text)) {
      socket.send(JSON.stringify({ type: "transcript", role: "model", text: "Window 1 is **e2e triage**, rerunning checkout.spec with tracing on." }));
      socket.send(JSON.stringify({ type: "open_pane", pane_id: "%9", label: 'window 1 "e2e triage"' }));
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

const python = (...args) => ["uv", ["run", "--directory", tree, "python", ...args]];
const now = Number(execFileSync(...python("-c", "from scripts.demo_fleet import NOW; print(NOW)"), { encoding: "utf8" }));
const port = await freePort();
const server = spawn(...python("-m", "scripts.demo_server", String(port)), { stdio: "inherit" });
const base = `http://127.0.0.1:${port}`;
mkdirSync(out, { recursive: true });
let browser;
try {
  await waitUp(`${base}/api/version`, server);
  // Font rendering settled by flags, not by whatever fontconfig answers: on a fresh CI runner
  // one shot came out with three text baselines (and an icon aligned to one) a pixel lower
  // than every other render of the same page. Unhinted metrics and grayscale antialiasing
  // leave nothing for that answer to change.
  browser = await chromium.launch({ args: ["--disable-lcd-text", "--font-render-hinting=none"] });
  for (const [name, viewport, colorScheme, hash, step] of SHOTS) {
    const context = await browser.newContext({ viewport, colorScheme, deviceScaleFactor: 2,
      reducedMotion: "reduce", locale: "en-US", timezoneId: "UTC", serviceWorkers: "block",
      isMobile: viewport === PHONE, hasTouch: viewport === PHONE });
    // Start the clock at the fleet's NOW and let it run: a clock frozen solid (setFixedTime)
    // starves the app's own Date-based throttles and makes layout racy between runs.
    await context.clock.setSystemTime(now * 1000);
    context.setDefaultTimeout(10000);
    const page = await context.newPage();
    await page.routeWebSocket(/\/api\/live-mode/, stubChat);
    // Wait on the API rather than a selector, so a UI that renames its markup still gets
    // shot and the diff shows the change instead of the run failing.
    const state = page.waitForResponse((r) => r.url().includes("/api/state"));
    await page.goto(`${base}/m/${hash}`);
    await page.addStyleTag({ content: STILL });
    await state;
    // A step written against one UI may not fit another; shoot what is there either way.
    await step?.(page).catch((e) => console.warn(`${name}: step failed, shooting as is: ${e.message}`));
    await page.evaluate(() => document.fonts.ready);
    // Long polls never let the network idle, so wait for the pixels instead: shoot until two
    // frames in a row agree (charts and the live frame are still settling before that).
    let last, shot;
    for (let i = 0; i < 20 && !(shot && last?.equals(shot)); i++) {
      last = shot;
      await page.waitForTimeout(500);
      shot = await page.screenshot();
    }
    // Never write a frame that did not settle: that is how nondeterminism slips in unseen.
    if (!last?.equals(shot)) throw new Error(`${name}: still changing after 10s`);
    writeFileSync(path.join(out, `${name}.png`), shot);
    await context.close();
    console.log(`shot ${name}`);
  }
} finally {
  await browser?.close();
  server.kill();
}
