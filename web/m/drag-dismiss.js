// A phone's sheet (or drawer) is dragged away as on iOS: it follows the finger with no
// transition, and its backdrop fades in step (--drag, 1 to 0, which ::backdrop inherits).
// Let go past a third of its size, or flick it, and it closes by `onClose`, its own close
// path (so focus goes back as from its ×); short of that it springs back. A drag starts
// anywhere nothing under the finger is scrolled down (a sheet at its top, its handle, its
// heading), so a scroll inside still scrolls. y: a bottom sheet pulled down; x: a left
// drawer pushed left, from anywhere (a drawer has nothing that scrolls sideways). Touch
// events, not pointer events: a touchmove can be cancelled, so the drag is never handed to
// the browser's own panning half way.
export const FLICK = 0.5; // px/ms toward closing, measured over the last move

// `moved` px toward closing, of a `size` px sheet, at `speed` px/ms (negative = back).
export const dismisses = (moved, size, speed) => speed > FLICK || (moved > size / 3 && speed > -FLICK);

const WIDE = "(min-width: 1100px)"; // wide, sheets are docked panels and dropdowns: no drag
const SPRING_MS = 200;

export function dragToDismiss(el, { axis = "y", onClose }) {
  const y = axis === "y", sign = y ? 1 : -1, at = (t) => sign * (y ? t.clientY : t.clientX);
  let g = null, settling = false;
  el.addEventListener("touchstart", (e) => {
    if (g?.on) return end(e); // a second finger: spring back
    const t = e.touches[0];
    g = e.touches.length === 1 && !settling && !matchMedia(WIDE).matches ? { x0: t.clientX, y0: t.clientY, from: at(t), last: at(t), when: e.timeStamp, speed: 0, on: null } : null;
  }, { passive: true });
  el.addEventListener("touchmove", (e) => {
    if (!g || g.on === false) return;
    const t = e.touches[0], d = at(t) - g.from;
    if (g.on === null) {
      const along = Math.abs(y ? t.clientY - g.y0 : t.clientX - g.x0), across = Math.abs(y ? t.clientX - g.x0 : t.clientY - g.y0);
      if (Math.max(along, across) < 8) return; // not yet a direction
      let n = e.target;
      while (n && n !== el.parentNode && !(y && n.scrollTop > 0)) n = n.parentNode;
      g.on = d > 0 && along > across && n === el.parentNode;
      if (!g.on) return;
      g.size = y ? el.offsetHeight : el.offsetWidth;
      el.style.transition = "none";
    }
    e.preventDefault(); // the sheet moves, not the page under it
    g.speed = (at(t) - g.last) / Math.max(1, e.timeStamp - g.when);
    g.last = at(t); g.when = e.timeStamp;
    const px = Math.max(0, d);
    el.style.transform = `translate${axis.toUpperCase()}(${sign * px}px)`;
    el.style.setProperty("--drag", String(1 - px / g.size));
  }, { passive: false });
  function end(e) {
    if (!g?.on) return void (g = null);
    const close = e.type === "touchend" && dismisses(g.last - g.from, g.size, e.timeStamp - g.when > 100 ? 0 : g.speed); // a pause, then letting go, is no flick
    g = null;
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.style.transition = still ? "none" : `transform ${SPRING_MS}ms ease-out`;
    el.style.transform = close ? `translate${axis.toUpperCase()}(${sign * 100}%)` : "";
    el.style.setProperty("--drag", close ? "0" : "1");
    settling = true; // a new drag waits for this one to settle (and close, if it does)
    setTimeout(() => {
      settling = false;
      if (close) onClose();
      el.style.transition = el.style.transform = "";
      el.style.removeProperty("--drag");
    }, still ? 0 : SPRING_MS);
  }
  el.addEventListener("touchend", end);
  el.addEventListener("touchcancel", end);
}
