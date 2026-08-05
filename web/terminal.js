// terminal.js — the ONE path from a tmux capture to display HTML.
//
// tmux is the terminal emulator; we read its composed framebuffer (capture-pane),
// never a raw pty stream. Two jobs live here so they can't drift apart the way the
// bolted-on linkify + inline colorizer were starting to:
//
//   1. Links — OSC 8 hyperlinks (materialized upstream into markdown [label](url))
//      render terminal-style (label only); bare URLs, including ones a TUI hard-wrapped
//      across lines, link themselves.
//   2. Color — SGR runs (only present when the caller kept escapes, i.e. the live
//      view) become styled spans. Plain captures skip this entirely.
//
// renderCapture(text, {color}) is the single entry point. Link anchoring is applied
// to the TEXT BETWEEN color runs, so a URL that a color change splits still anchors
// per-segment and decoloring never touches the <a> tags.

const _AMP = /[&<>"']/g;
const _ENT = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s).replace(_AMP, (c) => _ENT[c]);

// 0-15 tuned for the dark theme (not raw xterm defaults, which are muddy on #0b0f14).
const PALETTE = [
  "#484f58", "#ff7b72", "#3fb950", "#d29922", "#58a6ff", "#bc8cff", "#39c5cf", "#b1bac4",
  "#6e7681", "#ffa198", "#56d364", "#e3b341", "#79c0ff", "#d2a8ff", "#56d4dd", "#f0f6fc",
];
const clamp = (n) => (n < 0 ? 0 : n > 255 ? 255 : n | 0); // pane content is untrusted
function color256(n) {
  n = clamp(n); // a crafted \x1b[38;5;999m must not produce an out-of-range gray value
  if (n < 16) return PALETTE[n];
  if (n > 231) { const v = 8 + 10 * (n - 232); return `rgb(${v},${v},${v})`; } // gray ramp
  n -= 16;
  const c = [0, 95, 135, 175, 215, 255];
  return `rgb(${c[(n / 36) | 0]},${c[((n / 6) | 0) % 6]},${c[n % 6]})`;
}

// --- link anchoring (was app.js linkifyCapture; now the shared authority) ---
// Anchor URLs in a run of text. A URL a TUI hard-wrapped across full-width lines is
// rejoined into one href (wrapping heuristics computed inline per call) while the
// anchor keeps the visible line breaks. Markdown links match first — that's how the
// capture materializes OSC 8 — and show the label alone, URL hidden.
// URL char class: exclude whitespace, quotes/brackets, and ALL control + non-ASCII
// as explicit escapes — writing those ranges as literal bytes embedded raw control
// chars (incl. NUL) that tools read as binary. Non-ASCII exclusion also stops TUI
// box-drawing (│) gluing onto an href.
const _U = "[^\\s<>\"')\\]\\u0000-\\u001F\\u007F-\\uFFFF]";
const LINK_RE = new RegExp(
  `\\[([^\\]\\n]{1,120})\\]\\((https?:\\/\\/${_U}+)\\)` +   // markdown [label](url)
  `|https?:\\/\\/${_U}+(?:\\n[ \\t]*${_U}+)*`,              // bare url (wrap-joined)
  "gi");
const anchor = (href, shown) =>
  `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(shown)}</a>`;

// `join` (default on) enables cross-line URL rejoining. It is a TERMINAL-GRID heuristic and
// must be off for prose — see linkifyText: prose lines are broken by the author, not by a
// fixed width, so "these two lines are the same length" carries no meaning there and the
// heuristic mistakes coincidence for wrapping.
function linkify(text, join = true) {
  // Cross-line URL joining: a URL that reaches a full-width line's end continues on the
  // next line (that's wrapping). Only join when the frame shows real wrapping — a
  // plausible width (>=40) and >=2 full-width lines — so a lone long line ending in a
  // URL, or a uniform narrow pane, isn't mistaken for wrap. The href drops the breaks;
  // the shown text keeps them so every visual fragment is clickable.
  const lines = text.split("\n");
  const maxLen = Math.max(0, ...lines.map((l) => l.length));
  const fullLines = lines.filter((l) => l.length >= maxLen - 1).length;
  const canJoin = join && maxLen >= 40 && fullLines >= 2 && fullLines < lines.length;
  const joinWidth = maxLen - 1;
  const lineEndLen = new Map(); // offset of each \n -> length of the line it ends
  let off = 0;
  for (const l of lines) { lineEndLen.set(off + l.length, l.length); off += l.length + 1; }

  let out = "", pos = 0, m;
  LINK_RE.lastIndex = 0;
  while ((m = LINK_RE.exec(text))) {
    out += esc(text.slice(pos, m.index));
    if (m[1] !== undefined) { // markdown link: label shown, url hidden
      out += anchor(m[2], m[1]);
      pos = m.index + m[0].length; LINK_RE.lastIndex = pos; continue;
    }
    // Bare URL: accept newline-continuations only while the line being left was
    // full-width; cut at the first newline that isn't (or if joining is off).
    let cut = m[0].length, search = 0;
    for (;;) {
      const nl = m[0].indexOf("\n", search);
      if (nl === -1) break;
      const endedLine = lineEndLen.get(m.index + nl);
      if (canJoin && endedLine !== undefined && endedLine >= joinWidth) { search = nl + 1; continue; }
      cut = nl; break;
    }
    const shown = m[0].slice(0, cut);
    out += anchor(shown.replace(/\n[ \t]*/g, ""), shown);
    pos = m.index + cut; LINK_RE.lastIndex = pos;
  }
  return out + esc(text.slice(pos));
}

// --- the render ---
// color:false — plain capture (no escapes): just linkify. This is the peek/fullscreen
//   path when we didn't ask tmux to keep colors.
// color:true — live frame with SGR intact: split on SGR runs, style each segment,
//   linkify the text inside it. Non-SGR escapes (OSC/CSI/two-char) are dropped —
//   tmux already composed the screen, so nothing else is load-bearing.
const SGR_SPLIT = /(\x1b\[[0-9;]*m)/;
const SGR_ONE = /^\x1b\[([0-9;]*)m$/;

// Linkify PROSE (card text: events, headlines, summaries). Same anchor treatment the
// terminal gets — markdown [label](url) and bare URLs — but with wrap-joining OFF, because
// that heuristic reads equal line lengths as evidence of a fixed-width grid. In prose the
// line breaks are the author's, so two coincidentally similar lines were enough to glue the
// following words onto the href: a summary line ending in a URL produced an anchor pointing
// at "https://ex.com/abc" + the next line's text, and swallowed that text into the link.
// Escapes everything it doesn't anchor, so it is a drop-in replacement for esc() on
// untrusted model text.
export function linkifyText(text) {
  return linkify(String(text ?? ""), false);
}

// renderCaptureLines(text, {color}) — the SAME render, delivered as one HTML string per
// screen line, so a caller can diff a streaming frame line-by-line instead of swapping the
// whole subtree (web/app.js's terminal paint; see THE RENDER INVARIANT there).
//
// It is deliberately a thin wrapper around renderCapture rather than a second renderer:
// renderCapture stays the ONE authority on what a line's markup looks like, and — crucially
// — the parse stays WHOLE-CAPTURE. linkify's cross-line URL joining is a grid heuristic that
// reads every line's length (maxLen / fullLines) and joins a URL across a full-width line
// break; rendering line-at-a-time would destroy that (each line would be its own "capture"
// of one line, so maxLen==that line and fullLines==1 ⇒ never joins). So we render the frame
// once and split the RESULT.
//
// Splitting HTML on "\n" is not a plain String.split: renderCapture emits spans and anchors
// that STRADDLE newlines (a color run spanning three rows is one <span>; a wrap-joined URL is
// one <a> whose shown text keeps the breaks). Cutting mid-element would hand each line
// unbalanced markup. So we walk the string, keep a stack of open tags, and at every newline
// close the stack (innermost first) and re-open it verbatim on the next line. Rendered under
// `white-space: pre` with one block child per line, the result is visually identical to the
// single-string form: same spans, same styles, same hrefs. A wrap-joined URL becomes one
// <a> per visual fragment instead of one straddling <a> — same href on each, which is exactly
// the clickability contract linkify already documents ("every visual fragment is clickable").
const TAG_RE = /<(\/?)([a-z]+)([^>]*)>/gi;
export function renderCaptureLines(text, opts) {
  const html = renderCapture(text, opts);
  const lines = [];
  const open = []; // [{name, full}] — innermost last
  let cur = "", pos = 0, m;
  const reopen = () => open.map((o) => o.full).join("");
  const closeAll = () => open.map((o) => `</${o.name}>`).reverse().join("");
  TAG_RE.lastIndex = 0;
  // Walk tag boundaries; between them, text may contain newlines to break on.
  const flushText = (s) => {
    let i = 0;
    for (;;) {
      const nl = s.indexOf("\n", i);
      if (nl === -1) { cur += s.slice(i); return; }
      cur += s.slice(i, nl) + closeAll();
      lines.push(cur);
      cur = reopen();
      i = nl + 1;
    }
  };
  while ((m = TAG_RE.exec(html))) {
    flushText(html.slice(pos, m.index));
    pos = m.index + m[0].length;
    if (m[1]) { // closing tag: unwind to it (our own output is well-nested)
      for (let i = open.length - 1; i >= 0; i--)
        if (open[i].name.toLowerCase() === m[2].toLowerCase()) { open.splice(i, 1); break; }
    } else {
      open.push({ name: m[2], full: m[0] });
    }
    cur += m[0];
  }
  flushText(html.slice(pos));
  lines.push(cur);
  return lines;
}

export function renderCapture(text, { color = false } = {}) {
  text = text.replace(/\r/g, ""); // normalize CR for BOTH paths — progress bars etc.
  if (!color) return linkify(text);
  let clean = text
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?/g, "") // OSC (links already markdown)
    .replace(/\x1b\[[0-9;?]*[^0-9;?m]/g, "")            // CSI with non-SGR final
    .replace(/\x1b[^[\]]/g, "");                        // bare two-char escape
  // Drop SGR sequences that fall INSIDE a materialized markdown link — Claude Code
  // colors the label and URL separately, so an SGR often lands between ] and (,
  // splitting the token so the label renders as text AND the URL as a second bare
  // link (the "double link"). Stripping SGR within the token keeps it atomic for
  // linkify; the link's own CSS color applies regardless.
  // Match a markdown-link token even when SGR sequences are sprinkled through it
  // (\s* absorbs them between ]/( and anywhere inside), then strip that SGR so the
  // token is atomic for linkify. Anchor color comes from CSS, so dropping it is fine.
  const S = "(?:\\x1b\\[[0-9;]*m)*"; // any run of SGR
  const LINK_TOKEN = new RegExp(
    `\\[${S}[^\\]\\n]{1,120}${S}\\]${S}\\(${S}https?:\\/\\/[^)\\s\\x1b]+${S}\\)`, "g");
  clean = clean.replace(LINK_TOKEN, (tok) => tok.replace(/\x1b\[[0-9;]*m/g, ""));
  const st = { b: 0, d: 0, i: 0, u: 0, fg: "", bg: "" };
  const styleAttr = () => {
    let s = "";
    if (st.fg) s += `color:${st.fg};`;
    if (st.bg) s += `background:${st.bg};`;
    if (st.b) s += "font-weight:600;";
    if (st.d) s += "opacity:.55;";
    if (st.i) s += "font-style:italic;";
    if (st.u) s += "text-decoration:underline;";
    return s;
  };
  let html = "";
  for (const part of clean.split(SGR_SPLIT)) {
    const sgr = SGR_ONE.exec(part);
    if (!sgr) {
      if (!part) continue;
      const inner = linkify(part);
      const s = styleAttr();
      html += s ? `<span style="${s}">${inner}</span>` : inner;
      continue;
    }
    const a = (sgr[1] || "0").split(";").map(Number);
    for (let i = 0; i < a.length; i++) {
      const c = a[i];
      if (c === 0) { st.b = st.d = st.i = st.u = 0; st.fg = st.bg = ""; }
      else if (c === 1) st.b = 1;
      else if (c === 2) st.d = 1;
      else if (c === 3) st.i = 1;
      else if (c === 4) st.u = 1;
      else if (c === 22) st.b = st.d = 0;
      else if (c === 23) st.i = 0;
      else if (c === 24) st.u = 0;
      else if (c >= 30 && c <= 37) st.fg = PALETTE[c - 30];
      else if (c === 39) st.fg = "";
      else if (c >= 40 && c <= 47) st.bg = PALETTE[c - 40];
      else if (c === 49) st.bg = "";
      else if (c >= 90 && c <= 97) st.fg = PALETTE[c - 82];
      else if (c >= 100 && c <= 107) st.bg = PALETTE[c - 92];
      else if (c === 38 || c === 48) {
        // extended color — consume its args whole so 48;5;2 can't misread as faint
        const isFg = c === 38, mode = a[i + 1];
        if (mode === 5) { const col = color256(a[i + 2] || 0); isFg ? (st.fg = col) : (st.bg = col); i += 2; }
        else if (mode === 2) { const col = `rgb(${clamp(a[i + 2] || 0)},${clamp(a[i + 3] || 0)},${clamp(a[i + 4] || 0)})`; isFg ? (st.fg = col) : (st.bg = col); i += 4; }
        else i = a.length; // malformed: abandon this sequence
      }
    }
  }
  return html;
}
