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
const WIDE = { width: 1440, height: 900 }, HD = { width: 1920, height: 1080 }, PHONE = { width: 390, height: 844 };

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
  ["wide-usage-past", WIDE, "light", "#view=usage", stepBack],
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
  ["mobile-usage-past", PHONE, "light", "#view=usage", stepBack],
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

// The same fleet with a second Claude account (TMUXRC_DEMO_ACCOUNTS=2, demo_fleet.PERSONAL):
// badges, the strip by urgency, grouped Plan usage, the picker, the filter and Accounts….
// Everything above is the one-account state, which must look as it did before accounts.
const TWO_ACCOUNTS = [
  ["accounts-wide-dashboard", WIDE, "light", "#view=dashboard", two()],
  ["accounts-hd-dashboard", HD, "dark", "#view=dashboard", two()],
  ["accounts-wide-pane", WIDE, "light", "#pane=%2513", two()],
  ["accounts-wide-new-window", WIDE, "dark", "", two(async (page) => { await page.click("#new-window"); await page.click('.acct-pick [data-dir="~/.claude-personal"]'); })],
  ["accounts-mobile-list", PHONE, "light", "", two()],
  ["accounts-mobile-list-dark", PHONE, "dark", "", two()],
  ["accounts-mobile-menu", PHONE, "light", "", two((page) => page.click("#more-button"))],
  ["accounts-mobile-filtered", PHONE, "dark", "", two(async (page) => { await page.click("#more-button"); await page.click('#acct-filter [data-dir="~/.claude-personal"]'); })],
  ["accounts-mobile-dashboard", PHONE, "light", "#view=dashboard", two((page) => page.locator("#usage-panel").scrollIntoViewIfNeeded())],
  ["accounts-mobile-pane", PHONE, "light", "#pane=%2513", two()],
  ["accounts-mobile-pane-menu", PHONE, "dark", "#pane=%2513", moveMenu()],
  ["accounts-mobile-move", PHONE, "light", "#pane=%2513", moveMenu("#move-pane"), "#move-dialog"],
  ["accounts-mobile-moved", PHONE, "light", "#pane=%2513", moveMenu("#move-pane", "Exit the old pane"), "#move-dialog"],
  ["accounts-mobile-new-window", PHONE, "light", "", two(async (page) => { await page.click("#new-window"); await page.waitForSelector(".acct-pick"); })],
  ["accounts-mobile-consent", PHONE, "light", "", two(chat("Tell e2e triage to rerun it headed", "#voice-log .propose .acct-badge"))],
  ["accounts-mobile-accounts", PHONE, "dark", "", two(async (page) => { await page.click("#more-button"); await page.click("#accounts-open"); })],
  ["accounts-mobile-add", PHONE, "light", "", two(async (page) => { await page.click("#more-button"); await page.click("#accounts-open"); await page.click("#acct-dialog [data-add]"); await page.fill("#acct-name", "Side project"); })],
  // Adding "Demo", step by step, each cropped to its sheet (docs/design/agent-accounts.md).
  // Last, and in this order: opening the sign-in window changes the demo server's state.
  ["accounts-add-1-new-window", PHONE, "light", "", two(async (page) => { await page.click("#new-window"); await page.waitForSelector(".acct-pick"); }), "#launch-dialog"],
  ["accounts-add-2-name", PHONE, "light", "", two(addFlow("name")), "#acct-dialog"],
  ["accounts-add-3-share", PHONE, "light", "", two(addFlow("share")), "#acct-dialog"],
  ["accounts-add-4-sign-in", PHONE, "light", "", two(addFlow("login")), "#acct-dialog"],
  ["accounts-add-5-waiting", PHONE, "light", "", two(addFlow("wait")), "#acct-dialog"],
  ["accounts-add-5-sign-in-pane", PHONE, "dark", "#pane=%2590&view=terminal", two()],
  ["accounts-add-6-done", PHONE, "light", "", two(addFlow("done")), "#acct-dialog"],
  ["accounts-add-7-list", PHONE, "light", "", two((page) => page.fill("#search", "sign in"))],
];
// The add flow up to `until`: name, share, login (its step before the window opens), wait
// (the window open, signing in) or done.
function addFlow(until) {
  return async (page) => {
    await page.click("#more-button"); await page.click("#accounts-open");
    const added = page.locator('#acct-dialog [data-remove="~/.claude-demo"]'); // by an earlier shot
    if (await added.count()) { // remove it, and wait for the list to come back without it
      await added.click(); await page.click("#acct-dialog [data-remove-ok]");
      await page.locator("#acct-dialog [data-remove-ok]").waitFor({ state: "detached" });
    }
    await page.click("#acct-dialog [data-add]");
    await page.fill("#acct-name", "Demo");
    if (until === "name") return;
    await page.click('#acct-dialog [data-goto="share"]');
    if (until === "share") return;
    await page.click('#acct-dialog [data-create="link"]'); await page.waitForSelector("#acct-session");
    if (until === "login") return;
    await page.click("#acct-dialog [data-login]"); await page.waitForSelector(".acct-wait");
    if (until === "wait") return;
    await page.waitForSelector("#acct-make-default", { timeout: 20000 });
  };
}
// The pane's "…" menu once the pane's account has loaded (the list's badges are hidden
// behind the pane on a phone, so two() can't wait on those), then `item` in it. A base
// from before accounts, or before the item, is shot as it is.
function moveMenu(item, done) {
  return async (page) => {
    if (!await page.locator('link[href="/m/accounts.js"]').count() || (item && !await page.locator(item).count())) return;
    await page.locator("#pane-location .acct-badge").waitFor();
    await page.click("#pane-menu-button");
    if (item) { await page.click(item); await page.waitForSelector("#move-dialog[open]"); }
    if (done) { await page.click("#move-choices button"); await page.getByText(done).waitFor(); } // the demo's move succeeds
  };
}

// Once the accounts have arrived (after the panes), the step. A base from before accounts
// (no accounts.js preload) is shot as it is; on any other, a missing badge fails the shot.
function two(step) {
  return async (page) => {
    if (!await page.locator('link[href="/m/accounts.js"]').count()) return;
    // Attached, not visible: a phone's dashboard has its badged rows hidden.
    await page.locator(".acct-badge").first().waitFor({ state: "attached" })
      .catch((e) => { throw Object.assign(new Error(`no account badge: ${e.message}`), { loud: true }); });
    await step?.(page);
  };
}

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

// The usage view's history: the 5h meter two windows back (it hit the limit), the 7d one.
async function stepBack(page) {
  for (const n of [0, 0, 1]) await page.locator('.usage-nav [data-step="1"]').nth(n).click();
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
// One server per account state: the demo fleet's accounts are fixed when it starts.
const servers = [];
async function serve(env = {}) {
  const port = await freePort();
  const server = spawn(...python("-m", "scripts.demo_server", String(port)), { stdio: "inherit", env: { ...process.env, ...env } });
  servers.push(server);
  await waitUp(`http://127.0.0.1:${port}/api/version`, server);
  return `http://127.0.0.1:${port}`;
}
mkdirSync(out, { recursive: true });
let browser;
try {
  const runs = [[[["card-a1-1280", { width: 1280, height: 900 }, "light", "#view=dashboard", undefined, "#usage-panel"],["card-a1-1440", { width: 1440, height: 900 }, "light", "#view=dashboard", undefined, "#usage-panel"],["card-a1-1920", { width: 1920, height: 900 }, "light", "#view=dashboard", undefined, "#usage-panel"],["card-a1-2560", { width: 2560, height: 1300 }, "light", "#view=dashboard", undefined, "#usage-panel"],["card-a2-1280", { width: 1280, height: 900 }, "light", "#view=dashboard", undefined, "#usage-panel"],["card-a2-1440", { width: 1440, height: 900 }, "light", "#view=dashboard", undefined, "#usage-panel"],["card-a2-1920", { width: 1920, height: 900 }, "light", "#view=dashboard", undefined, "#usage-panel"],["card-a2-2560", { width: 2560, height: 1300 }, "light", "#view=dashboard", undefined, "#usage-panel"]], await serve()], [[["card-a3-1280", { width: 1280, height: 900 }, "light", "#view=dashboard", two(), "#usage-panel"],["card-a3-1440", { width: 1440, height: 900 }, "light", "#view=dashboard", two(), "#usage-panel"],["card-a3-1920", { width: 1920, height: 900 }, "light", "#view=dashboard", two(), "#usage-panel"],["card-a3-2560", { width: 2560, height: 1300 }, "light", "#view=dashboard", two(), "#usage-panel"],["card-a4-1280", { width: 1280, height: 900 }, "light", "#view=dashboard", two(), "#usage-panel"],["card-a4-1440", { width: 1440, height: 900 }, "light", "#view=dashboard", two(), "#usage-panel"],["card-a4-1920", { width: 1920, height: 900 }, "light", "#view=dashboard", two(), "#usage-panel"],["card-a4-2560", { width: 2560, height: 1300 }, "light", "#view=dashboard", two(), "#usage-panel"],["full-a4-1920", HD, "dark", "#view=usage", two()],["phone-a4-dash", PHONE, "light", "#view=dashboard", two((page) => page.locator("#usage-panel").scrollIntoViewIfNeeded())],["full-a4-1440", WIDE, "light", "#view=usage", two()]], await serve({ TMUXRC_DEMO_ACCOUNTS: "2" })]];
  // Font rendering settled by flags, not by whatever fontconfig answers: on a fresh CI runner
  // one shot came out with three text baselines (and an icon aligned to one) a pixel lower
  // than every other render of the same page. Unhinted metrics and grayscale antialiasing
  // leave nothing for that answer to change.
  browser = await chromium.launch({ args: ["--disable-lcd-text", "--font-render-hinting=none"] });
  for (const [shots, base] of runs) for (const [name, viewport, colorScheme, hash, step, crop] of shots) {
    const context = await browser.newContext({ viewport, colorScheme, deviceScaleFactor: 2,
      reducedMotion: "reduce", locale: "en-US", timezoneId: "UTC", serviceWorkers: "block",
      isMobile: viewport === PHONE, hasTouch: viewport === PHONE });
    // Start the clock at the fleet's NOW and let it run: a clock frozen solid (setFixedTime)
    // starves the app's own Date-based throttles and makes layout racy between runs.
    await context.clock.setSystemTime(now * 1000);
    context.setDefaultTimeout(10000);
    const page = await context.newPage();
    const patch = (re, f) => page.route(re, async (r) => { const res = await r.fetch(); const j = await res.json(); f(j); await r.fulfill({ response: res, json: j }); });
    if (name.includes("-a4")) {
      await patch(/\/api\/accounts/, (j) => { const p = j.profiles.find((p) => p.name === "Personal"); j.profiles.push({ ...p, dir: "~/.claude-demo", tilde: "~/.claude-demo", name: "Demo", label: "Demo", who: "demo@example.net", color: 3, default: false, panes: [] }); });
      await patch(/\/api\/usage/, (j) => { const a = j.accounts.find((a) => a.dirs[0] === "~/.claude-personal"); j.accounts.push({ ...a, dirs: ["~/.claude-demo"], panes: [] }); });
    }
    if (name.includes("-a1")) {
      await patch(/\/api\/accounts/, (j) => { j.profiles = j.profiles.filter((p) => p.provider === "claude"); });
      await patch(/\/api\/usage/, (j) => { j.accounts = j.accounts.filter((a) => a.provider === "claude"); });
    }
    await page.routeWebSocket(/\/api\/live-mode/, stubChat);
    // Wait on the API rather than a selector, so a UI that renames its markup still gets
    // shot and the diff shows the change instead of the run failing.
    const state = page.waitForResponse((r) => r.url().includes("/api/state"));
    await page.goto(`${base}/m/${hash}`);
    await page.addStyleTag({ content: STILL });
    await state;
    // A step written against one UI may not fit another; shoot what is there, unless it says otherwise.
    await step?.(page).catch((e) => { if (e.loud) throw e; console.warn(`${name}: step failed, shooting as is: ${e.message}`); });
    await page.evaluate(() => document.fonts.ready);
    // Long polls never let the network idle, so wait for the pixels instead: shoot until two
    // frames in a row agree (charts and the live frame are still settling before that).
    // `crop`: just that sheet, when it is open (a base without it is shot whole).
    const target = crop && await page.locator(crop).isVisible() ? page.locator(crop) : page;
    let last, shot;
    for (let i = 0; i < 20 && !(shot && last?.equals(shot)); i++) {
      last = shot;
      await page.waitForTimeout(500);
      shot = await target.screenshot();
    }
    // Never write a frame that did not settle: that is how nondeterminism slips in unseen.
    if (!last?.equals(shot)) throw new Error(`${name}: still changing after 10s`);
    writeFileSync(path.join(out, `${name}.png`), shot);
    await context.close();
    console.log(`shot ${name}`);
  }
} finally {
  await browser?.close();
  for (const server of servers) server.kill();
}
