// Overscroll into the pane's own app, with no DOM so `node --test` can drive it.
//
// A fullscreen agent keeps its transcript inside the app, so the live view's top is not the
// top of the history (see tmux.wheel). Scrolling on past that top first meets RESIST_PX of
// resistance, so an ordinary fling that merely reaches the top never touches the pane; past
// it, every NOTCH_PX becomes one wheel notch for the app (about a line, like a wheel in a
// real terminal). `net` estimates how far up the app is, in notches: while it is non-zero
// scrolling up goes straight through, with no resistance.
//
// It is only an estimate. Apps accelerate the wheel: a burst of 30 notches moves Claude Code
// about 4 lines each, a lone notch moves it 1, and Claude drops a gesture's first notch. So
// counting notches back down to zero can leave the app well short of its bottom. Scrolling
// down past the view's bottom therefore keeps going to the app notch for notch whenever
// anything was sent up during this visit (`up`), even past the estimate. Notches below the
// app's bottom do nothing.
export const RESIST_PX = 160, NOTCH_PX = 20, IDLE_MS = 500;

export const overscrollState = () => ({ pull: 0, rem: 0, net: 0, up: 0, at: -Infinity });

// `dy` is the scroll delta in px (negative = up), given only while the view is pinned at the
// edge it pushes against. Returns the notches to send, positive = up.
export function overscroll(s, dy, now) {
  if (now - s.at > IDLE_MS) s.pull = s.rem = 0; // a pause lets the resistance spring back
  s.at = now;
  if (dy >= 0 ? !s.up : !s.net) {
    if (dy >= 0) { s.pull = 0; return 0; } // nothing was ever scrolled up: nothing to return
    const take = Math.min(-dy, RESIST_PX - s.pull);
    s.pull += take; dy += take;
    if (s.pull < RESIST_PX) return 0;
  }
  s.rem -= dy / NOTCH_PX;
  const notches = Math.trunc(s.rem);
  s.rem -= notches;
  s.net = Math.max(0, s.net + notches);
  if (notches > 0) s.up += notches;
  else if (notches && !s.net) s.pull = 0; // home by the estimate: scrolling up resists again
  return notches;
}
