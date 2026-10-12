// Mocks: variants of the real /m over the demo fleet, in one harness (scripts/mock/).
// See docs/deploy/previews.md, "Mocks", for the why and the manifest.
//
//   node scripts/mock.mjs build <src> <out>   snapshot /m into a self-contained folder   (make mock)
//   node scripts/mock.mjs check <out>         shoot the harness at desktop and phone size (make mock-check)
//   node scripts/mock.mjs live <src>          the harness over the live demo app        (make mock-live)
//
// <src> holds variants.json and the files it names. A build boots the demo server, opens
// each view at each width in both themes, and keeps the finished DOM with its stylesheet
// and images inlined: scratch pages cannot fetch, so everything a frame shows travels in
// the folder's own scripts. Then it stops the server.
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
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
  const note = [m.note && read(m.note), doc && existsSync(doc) && read(doc)].filter(Boolean).join("\n\n---\n\n");
  return {
    title: m.title || path.basename(src),
    note: note && markdownit().render(note),
    views: m.views || [{ name: "app", label: "App" }],
    variants: m.variants,
    widths: [PHONE.width, ...(m.widths || [1280, 1440, 1920])],
    scripts: Object.fromEntries(m.variants.filter((v) => v.script).map((v) => [v.script, read(v.script)])),
  };
}

// In the page: the DOM as it stands, made static. State that lives in properties (form
// values, scroll offsets, canvas pixels) is written into the markup, the app's scripts are
// dropped, and its linked stylesheets come back as one string with absolute URLs.
function snapshot() {
  for (const el of document.querySelectorAll("input, textarea")) {
    el.setAttribute("value", el.value);
    el.toggleAttribute("checked", !!el.checked);
  }
  for (const o of document.querySelectorAll("option")) o.toggleAttribute("selected", o.selected);
  for (const el of document.querySelectorAll("*")) if (el.scrollTop || el.scrollLeft) el.dataset.mockScroll = `${el.scrollTop},${el.scrollLeft}`;
  for (const c of document.querySelectorAll("canvas")) {
    const img = Object.assign(document.createElement("img"), { src: c.toDataURL("image/webp", 0.9), className: c.className });
    img.style.cssText = c.style.cssText;
    c.replaceWith(img);
  }
  for (const s of document.querySelectorAll("style")) if (s.textContent.startsWith("/* demo-still")) s.remove();
  const css = [...document.styleSheets].filter((s) => s.ownerNode?.tagName === "LINK").map((s) => [...s.cssRules]
    .map((r) => r.cssText).join("\n").replace(/url\("?([^")]+)"?\)/g, (m, u) => u.startsWith("#") ? m : `url("${new URL(u, s.href).href}")`)).join("\n");
  const root = document.documentElement.cloneNode(true);
  for (const n of root.querySelectorAll("script, link")) n.remove();
  return { html: root.outerHTML, css };
}

// Every image a snapshot references, fetched once and swapped for an @@IMG<n>@@ index into
// one shared table, since the same icons recur in every frame.
async function inliner(base) {
  const imgs = [], seen = new Map();
  async function inline(text) {
    const urls = new Set([...text.matchAll(/(?:\bsrc="|url\((?:"|&quot;))([^"&)]+)/g)].map((m) => m[1]).filter((u) => !/^(data:|blob:|#)/.test(u)));
    for (const u of urls) {
      // A fragment selects a view inside the file (claude-working.svg#still is its still
      // frame), so the file is fetched once and the fragment rides on after its placeholder.
      const [file, frag] = u.split("#"), url = new URL(file, base).href;
      if (!seen.has(url)) {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`${url}: ${res.status}`);
        imgs.push(`data:${res.headers.get("content-type")};base64,${Buffer.from(await res.arrayBuffer()).toString("base64")}`);
        seen.set(url, imgs.length - 1);
      }
      text = text.replaceAll(u, `@@IMG${seen.get(url)}@@${frag === undefined ? "" : `#${frag}`}`);
    }
    return text;
  }
  return { imgs, inline };
}

// data.js holds everything but the frames; each frame is its own snaps/<view>-<theme>-<width>.js,
// loaded only when shown, so a phone never downloads the desktop ones.
const snapFile = (key) => `snaps/${key.replaceAll("/", "-")}.js`;

function write(out, { snaps = {}, ...data }) {
  rmSync(path.join(out, "snaps"), { recursive: true, force: true }); // no frame outlives its build
  mkdirSync(path.join(out, "snaps"), { recursive: true });
  for (const [key, snap] of Object.entries(snaps)) writeFileSync(path.join(out, snapFile(key)), `MOCK.snaps[${JSON.stringify(key)}] = ${JSON.stringify(snap)};\n`);
  copyFileSync(path.join(ROOT, "scripts/mock/harness.html"), path.join(out, "index.html"));
  copyFileSync(path.join(ROOT, "scripts/mock/harness.js"), path.join(out, "harness.js"));
  writeFileSync(path.join(out, "data.js"), `window.MOCK = ${JSON.stringify({ ...data, snaps: {} })};\n`);
  console.log(`wrote ${out}`);
}

async function build(src, out) {
  const data = manifest(src), css = [], snaps = {};
  const demo = await startDemo(ROOT);
  try {
    const { imgs, inline } = await inliner(`${demo.base}/m/`);
    for (const theme of ["light", "dark"]) for (const width of data.widths) for (const view of data.views) {
      const name = `${view.name}/${theme}/${width}`;
      const page = await demo.open({ width, height: width === PHONE.width ? PHONE.height : 900 }, theme, view.hash);
      for (const selector of view.click || []) await page.click(selector);
      if (view.chat) await chat(view.chat, view.wait)(page);
      else if (view.wait) await page.waitForSelector(view.wait);
      await page.evaluate(() => document.fonts.ready);
      await settle(page, name);
      const snap = await page.evaluate(snapshot), sheet = await inline(snap.css);
      snaps[name] = { html: await inline(snap.html), css: css.includes(sheet) ? css.indexOf(sheet) : css.push(sheet) - 1 };
      await page.context().close();
      console.log(`snapshot ${name}`);
    }
    write(out, { ...data, css, imgs, snaps });
  } finally {
    await demo.close();
  }
}

// The same harness over the running app: make mock-live serves <src> at /mock/ from the
// demo server, so frames are same-origin with /m and the harness patches them after boot.
// Variant scripts are fetched fresh on every render, so an edit shows on reload.
function live(src) {
  const { scripts, ...data } = manifest(src);
  write(path.join(src, "live"), { ...data, live: true });
  console.log("open http://127.0.0.1:18039/mock/live/");
}

// The harness as each kind of viewer gets it, from the scratch server when the mock is
// published there, so the sandbox it runs under is the real one. A frame that is not the
// width it claims, or a page error, fails the check.
async function check(out) {
  const scratch = process.env.TMUXRC_SCRATCH_DIR, rel = scratch && path.relative(scratch, path.resolve(out));
  const url = rel && !rel.startsWith("..") && process.env.TMUXRC_SCRATCH_LOCAL_URL
    ? `${process.env.TMUXRC_SCRATCH_LOCAL_URL}/${rel}/` : pathToFileURL(path.join(out, "index.html")).href;
  const second = JSON.parse(readFileSync(path.join(out, "data.js"), "utf8").replace(/^window.MOCK = |;\n$/g, "")).variants[1]?.name;
  const shots = [
    ["desktop", DESKTOP, ""], ["desktop-note", DESKTOP, "note=1"], ["desktop-hidden", DESKTOP, "hide=1"],
    ["desktop-ab", DESKTOP, `w=${PHONE.width}&ab=1`],
    ["phone", PHONE, "hide=0"], ["phone-next", PHONE, `v=${second}&hide=0`], ["phone-hidden", PHONE, "hide=1"],
  ];
  const dir = path.join(out, "shots"), browser = await launch(), failures = [];
  mkdirSync(dir, { recursive: true });
  try {
    for (const [name, viewport, hash] of shots) {
      // Blocking service workers reads navigator.serviceWorker, which throws in the sandbox.
      const page = await newPage(browser, viewport, "dark", 0, { serviceWorkers: "allow" });
      page.on("pageerror", (e) => failures.push(`${name}: ${e.message}`));
      await page.goto(`${url}#${hash}`);
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
  const run = { build: () => build(dir, out), check: () => check(dir), live: () => live(dir) }[cmd];
  if (run) await run();
  else { console.error("usage: node scripts/mock.mjs build <src> <out> | check <out> | live <src>"); process.exitCode = 2; }
}
