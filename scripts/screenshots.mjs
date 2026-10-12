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
  ["wide-heatmap", WIDE, "light", "#view=dashboard", heatCard],
  ["wide-heatmap-view", WIDE, "dark", "#view=heat", heatCell],
  ["wide-usage-detail", WIDE, "dark", "#view=usage"],
  ["wide-chat", WIDE, "light", "#pane=%259", chat("Which panes need me?")],
  ["wide-subagents", WIDE, "dark", "#pane=%259", showSubagents],
  ["wide-open-loops", WIDE, "dark", "#view=loops", showLoops],
  ["wide-sessions", WIDE, "light", "#view=dashboard", reach(".atlas-multiples")],
  ["mobile-open-loops", PHONE, "light", "#view=loops", showLoops],
  ["mobile-loops-card", PHONE, "dark", "#view=dashboard", reach("#loops-card .loop-count")],
  ["mobile-sessions", PHONE, "dark", "#view=dashboard", reach(".atlas-multiples")],
  ["wide-activity", WIDE, "light", "#view=dashboard", reach(".atlas-pulse", true)],
  ["mobile-activity", PHONE, "dark", "#view=dashboard", reach(".atlas-pulse", true)],
  ["mobile-list", PHONE, "light", ""],
  ["mobile-list-dark", PHONE, "dark", ""],
  ["mobile-more-menu", PHONE, "light", "", (page) => page.click("#more-button")],
  ["mobile-usage", PHONE, "light", "", (page) => page.click("#usage")],
  ["mobile-usage-detail", PHONE, "dark", "#view=usage"],
  ["mobile-heatmap", PHONE, "light", "#view=dashboard", heatCard],
  ["mobile-pane", PHONE, "light", "#pane=%259"],
  ["mobile-needs-you", PHONE, "light", "#pane=%254"],
  ["mobile-question-items", PHONE, "light", "#pane=%2546"],
  ["mobile-menu", PHONE, "dark", "#pane=%2540"],
  ["mobile-terminal", PHONE, "light", "#pane=%259&view=terminal"],
  ["mobile-password", PHONE, "dark", "#pane=%2529&view=terminal"],
  ["mobile-pane-menu", PHONE, "dark", "#pane=%259", (page) => page.click("#pane-menu-button")],
  ["mobile-expunge", PHONE, "light", "#pane=%259", confirmExpunge],
  ["mobile-shell-menu", PHONE, "dark", "#pane=%256", (page) => page.click("#pane-menu-button")],
  ["mobile-chat-open", PHONE, "light", "", chat("Let's go back to window 1", "#voice-log .voice-open button")],
  ["mobile-chat-consent", PHONE, "light", "", chat("Tell e2e triage to rerun it headed", "#voice-log .propose .open")],
  ["mobile-chat-resume", PHONE, "light", "", chat("Resume the checkout session")],
  ["mobile-chat-minimized", PHONE, "light", "", async (page) => { await chat("Which panes need me?")(page); await page.click("#voice-close"); }],
  ["mobile-no-tmux", PHONE, "light", "", noTmux],
  ["wide-new-session", WIDE, "light", "", async (page) => { await noTmux(page); await page.click(".start-session:not([hidden])"); }],
];

// A host after a reboot, with no tmux server at all. The demo fleet always has panes, so
// its state is emptied on the way to the page.
async function noTmux(page) {
  // No timeout: the fetch may be a 25s long poll. A failure (the context closing under a
  // held poll) is dropped, not thrown — an unhandled one would end the whole run.
  await page.route(/\/api\/state/, async (route) => {
    try {
      const state = await (await route.fetch({ timeout: 0 })).json();
      await route.fulfill({ json: { ...state, panes: [], tmux_running: false } });
    } catch { /* page gone */ }
  });
  await page.reload();
  await page.waitForSelector(".start-session:not([hidden])");
}

// Motion off, and the UI font pinned to what Linux already renders for the app's stack:
// resolving -apple-system / "Segoe UI" (absent here) races, and the loser shifts inline
// icons and timestamps by a pixel from run to run.
const STILL = `body { font-family: "Liberation Sans", sans-serif !important; }
*, *::before, *::after { animation: none !important; transition: none !important;
  caret-color: transparent !important; scroll-behavior: auto !important; scrollbar-width: none !important; }`;

// Bring a dashboard node to the top once it, and the Open loops card above it (which arrives
// after the page), have drawn; a base that predates either shoots the page as it is. `end`
// scrolls to the page's foot instead (Activity, which grows from its strip there).
function reach(selector, end = false) {
  return async (page) => {
    for (const node of ["#loops-card .loop-count", selector]) await page.locator(node).first().waitFor({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(500); // the panels above settle their height first
    await page.locator(selector).first().evaluate((el, end) => {
      const pane = document.getElementById("landing"); // scroll the dashboard, never the page
      pane.scrollTop = end ? pane.scrollHeight : pane.scrollTop + el.getBoundingClientRect().top - pane.getBoundingClientRect().top - 16;
    }, end).catch(() => {});
    if (!end) return;
    // The strip grows into the chart, which lengthens the page: follow the foot until it holds.
    for (let last = -1, i = 0; i < 10; i++) {
      await page.waitForTimeout(300);
      const top = await page.evaluate(() => { const pane = document.getElementById("landing"); pane.scrollTop = pane.scrollHeight; return pane.scrollTop; });
      if (top === last) break;
      last = top;
    }
  };
}
// The Open loops page, once its lanes arrive.
async function showLoops(page) {
  await page.locator("#landing-loops summary").first().waitFor({ timeout: 5000 }).catch(() => {});
}

// The Sub-agents toggle is on by default; show its lines under activity cards too.
async function showSubagents(page) {
  await page.locator(".sb-group", { hasText: "Working" }).locator(".sb-icon").click();
  await page.locator(".sb-card", { hasText: "terraform plan review" }).scrollIntoViewIfNeeded();
}

// The heatmap card at the dashboard's foot, once the page above it has its height.
async function heatCard(page) {
  // The word cloud above lays out at random, and at the page's foot it is in frame.
  await page.addStyleTag({ content: ".atlas-cloud { visibility: hidden; }" });
  await page.waitForSelector(".atlas-heat-card .heat-grid");
  await page.waitForTimeout(500); // the panels above settle their height first
  await page.locator(".atlas-heat-card").scrollIntoViewIfNeeded();
}
// Older days page in while the foot is near; a shot waits until the day count holds still.
async function heatSettled(page) {
  let days;
  for (let i = 0; i < 20; i++) {
    await page.waitForSelector("#dash-page .heat-foot:not([aria-busy])");
    await page.waitForTimeout(400);
    const now = await page.locator("#dash-page .heat-day").count();
    if (now === days) return;
    days = now;
  }
}
// The heatmap's own view, one cell's tip open.
async function heatCell(page) {
  await heatSettled(page);
  await page.locator("#dash-page .heat-grid .heat-cell").nth(24 + 14).click(); // yesterday, 14:00
}

async function confirmExpunge(page) {
  await page.click("#pane-menu-button");
  await page.click("#expunge-pane");
  await page.waitForSelector("#expunge-dialog[open]");
}

function chat(ask, until) { // hoisted: SHOTS above calls it
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
