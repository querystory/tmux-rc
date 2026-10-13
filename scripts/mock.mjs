// Mocks: variants of the real /m over the demo fleet, in one harness (scripts/mock/).
// See docs/deploy/previews.md, "Mocks", for the why and the manifest.
//
//   node scripts/mock.mjs build <src> <out>   snapshot /m into a self-contained folder   (make mock)
//   node scripts/mock.mjs check <out>         shoot the harness at desktop and phone size (make mock-check)
//   node scripts/mock.mjs live <src> [<out>]  the harness over the live demo app        (make mock-live)
//
// <src> holds variants.json and the files it names. A build boots the demo server, opens
// each view at each width in both themes, and keeps the finished DOM with its stylesheet
// and images inlined: scratch pages cannot fetch, so everything a frame shows travels in
// the folder's own scripts. Then it stops the server.
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import markdownit from "../web/m/vendor/markdown-it.mjs";
import { PHONE, chat, launch, newPage, settle, startDemo } from "./demo_browser.mjs";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const DESKTOP = { width: 1920, height: 1080 };

// The manifest, resolved into what the harness reads. The note panel shows the mock's
// working note, then the checked-in decision record it feeds (docs/design/<doc>.md).
export function manifest(src) {
  const m = JSON.parse(readFileSync(path.join(src, "variants.json"), "utf8"));
  const read = (file) => readFileSync(path.resolve(src, file), "utf8");
  const doc = m.doc && path.join(ROOT, "docs/design", `${m.doc}.md`);
  // The record links GitHub-style to sibling .md files; point those at the docs site's
  // pretty URLs (as docs-site/layouts/_markup/render-link.html does), not at the mock.
  // Its images (../img/…) travel with the mock, as record-img/<name>, since a scratch page
  // can reach nothing outside its own folder.
  const recordImages = [];
  const record = doc && existsSync(doc) && read(doc).replace(/\]\((?![a-z]+:|\/|#)([^)\s#]+)\.md(#[^)\s]*)?\)/g,
    (_, rel, frag = "") => `](/docs/${path.posix.join("design", rel)}/${frag})`)
    .replace(/(!\[[^\]]*\]\()(?![a-z]+:|\/|#)([^)\s]+)\)/g,
      (_, open, rel) => {
        const file = path.resolve(path.dirname(doc), rel);
        recordImages.push(file);
        return `${open}record-img/${path.basename(file)})`;
      });
  const note = [m.note && read(m.note), record].filter(Boolean).join("\n\n---\n\n");
  return {
    recordImages,
    title: m.title || path.basename(src),
    note: note && markdownit().render(note),
    views: m.views || [{ name: "app", label: "App" }],
    variants: m.variants,
    states: m.states || [],
    fragments: m.fragments || {},
    widths: [PHONE.width, ...(m.widths || [1280, 1440, 1920])],
    scripts: Object.fromEntries(m.variants.filter((v) => v.script).map((v) => [v.script, read(v.script)])),
  };
}

// In the page: the DOM as it stands, made static. State that lives in properties (form
// values, scroll offsets, canvas pixels) is written into the markup, the app's scripts are
// dropped, and its linked stylesheets come back as one string with absolute URLs.
function snapshot() {
  for (const el of document.querySelectorAll("input")) {
    el.setAttribute("value", el.value);
    el.toggleAttribute("checked", el.checked);
  }
  for (const el of document.querySelectorAll("textarea")) el.textContent = el.value; // a textarea's markup value is its text
  for (const o of document.querySelectorAll("option")) o.toggleAttribute("selected", o.selected);
  for (const d of document.querySelectorAll("dialog:modal")) d.dataset.mockModal = "";
  // Images are inlined, which would break selectors on their paths (img[src$="-working.svg"]):
  // keep each path in data-mock-src and point those selectors at it.
  for (const el of document.querySelectorAll("[src]:not([src^='data:'])")) el.dataset.mockSrc = el.getAttribute("src");
  for (const el of document.querySelectorAll("*")) if (el.scrollTop || el.scrollLeft) el.dataset.mockScroll = `${el.scrollTop},${el.scrollLeft}`;
  for (const c of document.querySelectorAll("canvas")) {
    const img = Object.assign(document.createElement("img"), { src: c.toDataURL("image/webp", 0.9), className: c.className });
    img.style.cssText = c.style.cssText;
    c.replaceWith(img);
  }
  for (const s of document.querySelectorAll("style")) if (s.textContent.startsWith("/* demo-still")) s.remove();
  const css = [...document.styleSheets].filter((s) => s.ownerNode?.tagName === "LINK").map((s) => [...s.cssRules]
    .map((r) => r.cssText).join("\n").replace(/url\("?([^")]+)"?\)/g, (m, u) => u.startsWith("#") ? m : `url("${new URL(u, s.href).href}")`))
    .join("\n").replace(/\[src(?=[~|^$*]?=)/g, "[data-mock-src");
  const root = document.documentElement.cloneNode(true);
  for (const n of root.querySelectorAll("script, link")) n.remove();
  return { html: root.outerHTML, css };
}

// Every image a snapshot references, fetched once and swapped for an @@IMG<n>@@ index into
// one shared table, since the same icons recur in every frame. Only src attributes and
// url()s are rewritten, never the data-mock-src copies the stylesheet's selectors read.
const IMAGE = /((?<![-\w])src="|url\((?:"|&quot;))([^"&)]+)/g, skip = (u) => /^(data:|blob:|#)/.test(u);
async function inliner(base) {
  const imgs = [], seen = new Map();
  // A fragment selects a view inside the file (claude-working.svg#still is its still
  // frame), so the file is fetched once and the fragment rides on after its placeholder.
  const file = (u) => new URL(u.split("#")[0], base).href;
  async function inline(text) {
    for (const [, , u] of text.matchAll(IMAGE)) {
      if (skip(u) || seen.has(file(u))) continue;
      const res = await fetch(file(u));
      if (!res.ok) throw new Error(`${file(u)}: ${res.status}`);
      imgs.push(`data:${res.headers.get("content-type")};base64,${Buffer.from(await res.arrayBuffer()).toString("base64")}`);
      seen.set(file(u), imgs.length - 1);
    }
    return text.replace(IMAGE, (m, at, u) => skip(u) ? m : `${at}@@IMG${seen.get(file(u))}@@${u.includes("#") ? u.slice(u.indexOf("#")) : ""}`);
  }
  return { imgs, inline };
}

// data.js holds everything but the frames; each frame is its own snaps/<view>-<theme>-<width>.js,
// loaded only when shown, so a phone never downloads the desktop ones.
const snapFile = (key) => `snaps/${key.replaceAll("/", "-")}.js`;

const readData = (out) => JSON.parse(readFileSync(path.join(out, "data.js"), "utf8").replace(/^window.MOCK = |;\n$/g, ""));

function write(out, { snaps = {}, recordImages = [], ...data }) {
  rmSync(path.join(out, "snaps"), { recursive: true, force: true }); // no frame outlives its build
  mkdirSync(path.join(out, "snaps"), { recursive: true });
  if (recordImages.length) mkdirSync(path.join(out, "record-img"), { recursive: true });
  for (const file of recordImages) copyFileSync(file, path.join(out, "record-img", path.basename(file)));
  for (const [key, snap] of Object.entries(snaps)) writeFileSync(path.join(out, snapFile(key)), `MOCK.snaps[${JSON.stringify(key)}] = ${JSON.stringify(snap)};\n`);
  copyFileSync(path.join(ROOT, "scripts/mock/harness.html"), path.join(out, "index.html"));
  copyFileSync(path.join(ROOT, "scripts/mock/harness.js"), path.join(out, "harness.js"));
  writeFileSync(path.join(out, "data.js"), `window.MOCK = ${JSON.stringify({ ...data, snaps: {} })};\n`);
  console.log(`wrote ${out}`);
}

async function build(src, out) {
  const data = manifest(src), css = [], snaps = {}, fragments = {};
  const demo = await startDemo(ROOT);
  // /m in `theme` at `width`, after a view's (or a fragment's) steps, made static.
  async function capture(step, theme, width, name) {
    const page = await demo.open({ width, height: width === PHONE.width ? PHONE.height : 900 }, theme, step.hash);
    for (const selector of step.click || []) await page.click(selector);
    if (step.chat) await chat(step.chat, step.wait)(page);
    else if (step.wait) await page.waitForSelector(step.wait);
    await page.evaluate(() => document.fonts.ready);
    await settle(page, name);
    const snap = await page.evaluate(snapshot);
    const fragment = step.selector && await page.$eval(step.selector, (el) => el.outerHTML);
    await page.context().close();
    console.log(`snapshot ${name}`);
    return { ...snap, fragment };
  }
  try {
    const { imgs, inline } = await inliner(`${demo.base}/m/`);
    for (const theme of ["light", "dark"]) for (const width of data.widths) for (const view of data.views) {
      const name = `${view.name}/${theme}/${width}`, snap = await capture(view, theme, width, name), sheet = await inline(snap.css);
      snaps[name] = { html: await inline(snap.html), css: css.includes(sheet) ? css.indexOf(sheet) : css.push(sheet) - 1 };
    }
    // A fragment is one element (a populated sheet, say) the variant can drop into any frame.
    for (const [name, step] of Object.entries(data.fragments)) fragments[name] = await inline((await capture(step, "light", PHONE.width, name)).fragment);
    write(out, { ...data, fragments, css, imgs, snaps });
  } finally {
    await demo.close();
  }
}

// The same harness over the running app: make mock-live serves <src> at /mock/ from the
// demo server, so frames are same-origin with /m and the harness patches them after boot.
// Variant scripts are fetched fresh on every render, so an edit shows on reload. The harness
// captures fragments itself, except a chat one, which needs the build's canned socket: that
// comes from the last build in `built` (images and all), or is missing, said so here.
function live(src, built) {
  const { scripts, ...data } = manifest(src);
  const prior = built && existsSync(path.join(built, "data.js")) ? readData(built) : { fragments: {} }, fragments = {};
  for (const [name, step] of Object.entries(data.fragments)) {
    if (!step.chat) continue;
    if (prior.fragments[name]) fragments[name] = prior.fragments[name].replace(/@@IMG(\d+)@@/g, (_, i) => prior.imgs[i]);
    else console.warn(`fragment ${name} needs the canned chat: run make mock first`);
  }
  write(path.join(src, "live"), { ...data, fragments, fragmentSteps: data.fragments, live: true });
  console.log("open http://127.0.0.1:18039/mock/live/");
}

// The harness as each kind of viewer gets it, served under the policy /scratch/ sends
// (openbus/server.py), so it runs in the same opaque-origin sandbox as the published page
// wherever the check runs. A frame that is not the width it claims, or a page error, fails.
const SANDBOX = "sandbox allow-scripts allow-popups; form-action 'none'; connect-src 'none'";
async function check(out) {
  const serve = (route) => route.fulfill({ headers: { "Content-Security-Policy": SANDBOX },
    path: path.join(out, new URL(route.request().url()).pathname.replace(/\/$/, "/index.html")) });
  const data = readData(out);
  const second = data.variants[1]?.name;
  // Every frame the harness can ask for, not only the ones shot: a missing one would quietly
  // render the "rebuild the mock" placeholder.
  const missing = data.views.flatMap((v) => ["light", "dark"].flatMap((t) => data.widths.map((w) => snapFile(`${v.name}/${t}/${w}`))))
    .filter((file) => !existsSync(path.join(out, file)));
  const failures = missing.map((file) => `missing ${file}`);
  const shots = [
    ["desktop", DESKTOP, ""], ["desktop-note", DESKTOP, "note=1"], ["desktop-hidden", DESKTOP, "hide=1"],
    ["desktop-ab", DESKTOP, `w=${PHONE.width}&ab=1`],
    ["phone", PHONE, "hide=0"], ["phone-hidden", PHONE, ""],
    // Every other variant once, so each one's script runs; with one variant, nothing to flip to.
    ...data.variants.slice(1).map((v) => [`phone-${v.name}`, PHONE, `v=${v.name}&hide=0`]),
  ].filter(([name]) => second || name !== "desktop-ab");
  const dir = path.join(out, "shots"), browser = await launch();
  rmSync(dir, { recursive: true, force: true }); // only this check's shots, none left over from an older mock
  mkdirSync(dir, { recursive: true });
  try {
    for (const [name, viewport, hash] of shots) {
      // Blocking service workers reads navigator.serviceWorker, which throws in the sandbox.
      const page = await newPage(browser, viewport, "dark", 0, { serviceWorkers: "allow" });
      page.on("pageerror", (e) => failures.push(`${name}: ${e.message}`));
      await page.route("http://mock.check/**", serve);
      await page.goto(`http://mock.check/#${hash}`);
      await page.waitForSelector("iframe");
      await page.waitForTimeout(1500); // no settling: the app's spinners keep turning in a snapshot
      writeFileSync(path.join(dir, `${name}.png`), await page.screenshot());
      // Each frame as wide as it was asked to be (the whole phone screen on a phone), and two
      // of them only where both fit.
      const frames = await page.$$eval("iframe", (fs) => fs.map((f) => [f.getBoundingClientRect().width, parseFloat(f.style.width)]));
      const count = hash.includes("ab=1") ? 2 : 1;
      if (frames.length !== count || frames.some(([got, asked]) => got !== (viewport === PHONE ? PHONE.width : asked))) {
        failures.push(`${name}: frames ${JSON.stringify(frames)} ([rendered, asked] px), expected ${count}`);
      }
      console.log(`shot ${path.join(dir, name)}.png`);
      await page.context().close();
    }
  } finally {
    await browser.close();
  }
  if (failures.length) throw new Error(failures.join("\n"));
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [cmd, dir, out] = process.argv.slice(2);
  const run = { build: () => build(dir, out), check: () => check(dir), live: () => live(dir, out) }[cmd];
  if (run) await run();
  else { console.error("usage: node scripts/mock.mjs build <src> <out> | check <out> | live <src> [<built>]"); process.exitCode = 2; }
}
