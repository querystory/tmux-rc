// Overscroll into the pane's own app, with no DOM so `node --test` can drive it.
//
// A fullscreen agent keeps its transcript inside the app, so the live view's top is not the
// top of the history (see tmux.wheel). Scrolling on past that top first meets RESIST_PX of
// resistance, so an ordinary fling that merely reaches the top never touches the pane; past
// it, every NOTCH_PX becomes one wheel notch for the app (about a line, like a wheel in a
// real terminal). `net` counts the notches the app is scrolled up by: while it is non-zero
// scrolling up goes straight through, and scrolling down past the view's bottom brings the
// app back down until it is home, where the resistance applies again.
export const RESIST_PX = 160, NOTCH_PX = 20, IDLE_MS = 500;

export const overscrollState = () => ({ pull: 0, rem: 0, net: 0, at: -Infinity });

// `dy` is the scroll delta in px (negative = up), given only while the view is pinned at the
// edge it pushes against. Returns the notches to send, positive = up.
export function overscroll(s, dy, now) {
  if (now - s.at > IDLE_MS) s.pull = s.rem = 0; // a pause lets the resistance spring back
  s.at = now;
  if (!s.net) {
    if (dy >= 0) { s.pull = 0; return 0; } // the app is home: nothing below to return to
    const take = Math.min(-dy, RESIST_PX - s.pull);
    s.pull += take; dy += take;
    if (s.pull < RESIST_PX) return 0;
  }
  s.rem -= dy / NOTCH_PX;
  const notches = Math.max(Math.trunc(s.rem), -s.net); // never below home
  s.net += notches; s.rem -= notches;
  if (!s.net && notches) s.pull = s.rem = 0; // home again: the resistance re-arms
  return notches;
}
