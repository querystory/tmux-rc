// Every tmux-rc tab icon (docs/design/tab-icons.md): a brand tile (scratch.svg's, or the app
// icon for the sessions home), optionally its glyph swapped for another Lucide one, plus the
// state of the tab's subject as a dot top-right, or the in-progress arc round the edge. The app
// imports this; the sandboxed scratch placeholder gets it inlined, so both draw one way.
const DOTS = { needs: "#f85149", working: "#e3a92b", done: "#2ea043", stopped: "#8b949e" };
const CHECK = '<path d="M134 41l12 12 22-24" fill="none" stroke="#fff" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>';
export function tabIcon(tile, { glyph, state, frame } = {}) {
  const svg = glyph ? tile.replace(/(<g [^>]*>)[\s\S]*?<\/g>/, `$1${glyph}</g>`) : tile;
  const dot = DOTS[state] ? `<circle cx="152" cy="40" r="38" fill="${DOTS[state]}" stroke="#0b0f14" stroke-width="14"/>${state === "done" ? CHECK : ""}` : "";
  const arc = frame === undefined ? "" : `<rect x="8" y="8" width="176" height="176" rx="34" fill="none" stroke="#1BB91F" stroke-width="16" pathLength="8" stroke-dasharray="2 6" stroke-dashoffset="${-frame}"/>`;
  return "data:image/svg+xml," + encodeURIComponent(svg.replace("</svg>", dot + arc + "</svg>"));
}
