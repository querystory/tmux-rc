// Picking a row in a "cursor" picker — a highlighted list you ARROW through, where
// neither a digit nor the row's own text selects anything (both land in the widget's
// search box, which is what made tapping a /resume session do nothing, issue #206).
//
// SHARED BY BOTH SURFACES. The deck and the phone PWA have entirely different send
// plumbing but the identical problem, and a copy on each side is how one of them ends up
// several fixes behind — which is precisely where the phone was while this lived only in
// web/app.js: the classifier had started saying "cursor" and the phone went on typing the
// row's text into the search box. Everything surface-specific arrives as `io`, so the
// walk itself is one implementation with one set of tests:
//
//   io.question()   → the pane's current question object, or null if it is gone
//   io.parsedAt()   → parsed_at of the frame that question() came from (seconds, float)
//   io.sendKey(k)   → send ONE tmux key-name, no Enter. Resolves true only if DELIVERED.
//   io.sendText(t)  → type literal text, NO Enter appended. Same delivery contract.
//   io.note(msg)    → say something to the user
//
// The delivery contract on sendKey/sendText is load-bearing, not politeness: a move that
// was never delivered leaves the highlight where it was, so every later step computed
// from "I moved once" is off by one and the walk commits the WRONG ROW. Both surfaces
// used to swallow POST failures and resolve anyway; they no longer do.
//
// An adapter may assume the pane still exists inside sendKey/sendText — question() is
// checked immediately before each of them with no await in between.

// How many moves we will walk before handing over to the widget's own search box. A row
// thirty rows down is better filtered for than arrowed to, and an anchor that is wrong
// only gets more wrong the further we walk on it.
export const CURSOR_MAX_STEPS = 12;
const PARSE_WAIT_MS = 4000; // per move; a wedged parse must not strand the walk
const SETTLE_MS = 4000;     // after the commit, before the lock is handed back
const POLL_MS = 120;

// One wording for every dead end. They are all the same thing from the user's side —
// this UI cannot drive that widget — and the alternative to saying it is the silent
// no-op the whole feature exists to remove.
const STUCK = "Can't select that row from here — use the keyboard.";
// A walk spans several seconds and several sends. Each step releases the surface's own
// send guard while it waits for the next frame, which is exactly the gap a second tap
// falls into — and two interleaved walks commit on whichever row their mixed moves happen
// to reach. The surfaces can't cover this for us: the deck's spinner does it by accident
// (its reparse marker never clears mid-walk, since the prompt never changes) and the
// phone's does not. One at a time, anywhere, is the whole rule.
const BUSY = "Still picking a row — wait for that to finish.";
let walking = false;

// The question as a DRIVABLE cursor picker, or null. Checked before EVERY send, because
// the pane can move on to a different prompt between two frames and the remaining keys
// would be typed into whatever replaced it. `options` is guarded too: classify() pipes
// model JSON through unvalidated, so a malformed question can carry anything at all.
// The one row carrying this text, or -1 when there is no such row — or two of them, which
// for the purpose of identifying a row is the same answer. Only ever consulted once the
// tapped index has stopped naming the row, which a filter or a scroll does immediately.
function soleIndex(options, text) {
  const i = options.indexOf(text);
  return i === options.lastIndexOf(text) ? i : -1;
}

function picker(io) {
  const q = io.question();
  return q && q.answer_style === "cursor" && Array.isArray(q.options) ? q : null;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Wait for a frame that is both NEWER than the one we sent from and DIFFERENT in the way
// this send should have changed things.
//
// Newer is needed because the card's ordinary reparse marker is no signal here: it clears
// when the question's PROMPT changes, which a cursor move never does, so waiting on it
// returned false after every single move and the walk stopped after one step. parsed_at
// is what actually moves — the server forces a reparse on every send.
//
// Newer is not SUFFICIENT, though, and that is the subtle half. parsed_at is read before
// the POST, and an ordinary watcher tick can land a parse in the gap before the key is
// even accepted. Taking that pre-key frame as the answer would have the next step computed
// from the old anchor: an overshoot at best, and at worst a commit fired on a highlight
// that has already moved past the row. So each caller also says what its send was supposed
// to change, and nothing counts until that has happened too.
async function waitForFrame(io, after, changed) {
  const deadline = Date.now() + PARSE_WAIT_MS;
  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    const q = picker(io);
    if (!q) return false; // picker closed under us — stop, don't keep typing
    if (io.parsedAt() > after && changed(q)) return true;
  }
  return false;
}

// MOVE, VERIFY, THEN SELECT — returns true only once the row is committed.
//
// The anchor (question.selected) comes from a frame that is already stale by the time a
// tap arrives, so each pass re-reads the screen instead of firing move+move+Enter in one
// burst. That burst would be faster and would silently pick the wrong session whenever
// the anchor had moved, which is the same class of bug in a new costume.
// Same rows, same order — i.e. the list has not renumbered under us.
const sameRows = (a, b) => a.length === b.length && a.every((row, i) => row === b[i]);

async function walk(io, km, targetText, targetIndex) {
  // The rows as of the previous pass. A long picker shows a WINDOW onto its list and
  // scrolls it as the highlight leaves the edge, so the options array can renumber between
  // two passes of this loop — see the identity check below for why that matters.
  let rows = null;
  // One pass more than the budget, because the budget counts MOVES and the arrival check
  // runs before each one: a row exactly CURSOR_MAX_STEPS away is reached by the last move,
  // and without a final check-only pass the loop spends its whole budget arriving and then
  // reports failure with the highlight sitting on the requested row. That last pass may
  // only LOOK — see the bail below — or the budget would quietly be one larger than the
  // constant says.
  for (let moves = 0; moves <= CURSOR_MAX_STEPS; moves++) {
    const q = picker(io);
    if (!q) return false;
    // Prefer the index the user actually tapped, for exactly as long as it still names
    // their row. Once a filter or a scroll has renumbered the list that index stops
    // matching, and the row's text is the only identity left — which is no identity at
    // all when two sessions share a title, so that case refuses rather than resuming a
    // coin-flip. Selecting the wrong session confidently is worse than not selecting.
    // The tapped index identifies the row only while the list has not moved under it.
    // "Same text at the same index" is not proof on its own: two sessions can share a
    // title, and after a scroll index 2 can be a DIFFERENT session wearing the same name.
    // Comparing the rows themselves is the proof — unchanged list, index still good;
    // changed list, the text is all the row has left, and that is nothing at all when it
    // matches twice. (First pass has nothing to compare against: the tap was made against
    // that frame, and the text check below is the only corroboration available.)
    const steady = rows === null || sameRows(rows, q.options);
    rows = q.options;
    const want = steady && q.options[targetIndex] === targetText
      ? targetIndex
      : soleIndex(q.options, targetText);
    // `selected` is model JSON too, so "a number" is not enough: -1, 2.5 and an index off
    // the end of the list all arrive as numbers and all make the walk step a wrong
    // distance in a confident direction. Anything that isn't a real row is the
    // unknown-anchor case, which already has an honest answer below.
    const at = Number.isInteger(q.selected) && q.selected >= 0 && q.selected < q.options.length
      ? q.selected
      : null;
    if (want < 0) return false; // gone, or two rows wear the title and neither is "the" one
    if (at === null) return false; // no trustworthy anchor — walking blind picks a row
    if (at === want) return km.select ? io.sendKey(km.select) : false;
    if (moves === CURSOR_MAX_STEPS) return false; // budget spent; this pass only looked
    const key = at < want ? km.next : km.prev;
    if (!key) return false; // the widget never advertised this direction — don't invent one
    const before = io.parsedAt();
    if (!(await io.sendKey(key))) return false; // undelivered: the anchor is now a lie
    // A move that landed MOVED the highlight. A frame still reporting the old anchor is
    // either one that predates the key or a move that did nothing (the end of the list, a
    // binding the widget doesn't really have) — and both mean this walk cannot continue.
    if (!(await waitForFrame(io, before, (f) => f.selected !== at))) return false;
  }
  return false;
}

// Drive the picker to `targetText` and commit it. `targetIndex` is where the row sat in
// the frame the user tapped.
export async function pickCursorRow(io, targetText, targetIndex) {
  if (walking) { io.note(BUSY); return false; }
  walking = true;
  try {
    const done = await pick(io, targetText, targetIndex);
    // Hold the lock PAST the commit, until a frame arrives that has noticed it. The picker
    // does not vanish the instant Enter is delivered — the phone goes on rendering the
    // stale question with live buttons until the next parse — and a second tap in that
    // window starts a walk against a list that is no longer on screen, firing arrows and
    // an Enter into whatever the selection just opened. (The deck escapes it only by
    // accident: its reparse spinner happens to cover the same window.) Bounded, and only
    // ever best-effort — the row is already selected either way, so a screen that never
    // settles must not leave picking disabled for the life of the page.
    if (done) {
      const deadline = Date.now() + SETTLE_MS;
      while (Date.now() < deadline && picker(io)) await sleep(POLL_MS);
    }
    return done;
  } finally {
    walking = false;
  }
}

async function pick(io, targetText, targetIndex) {
  const q0 = picker(io);
  // A keymap is model output too, so it can be absent, a string, or a list. Only null and
  // undefined need substituting: reading `.next` off a string or an array is undefined,
  // which is the right answer anyway — every binding is optional and the walk refuses each
  // one it was not given rather than inventing it.
  const km = (q0 && q0.keymap) || {};
  if (await walk(io, km, targetText, targetIndex)) return true;
  // Fallback: the widget's own search box. It needs BOTH bindings advertised — typing
  // filters the list but does not commit it, so the obvious shortcut of appending Enter
  // is precisely the unadvertised guess this file exists to stop. A picker that binds Tab
  // to select, or binds nothing, gets told rather than guessed at.
  //
  // Re-read the picker rather than reusing q0: walk() can have spent seconds waiting, and
  // if the pane has moved on to a DIFFERENT prompt in that time, typing the old row's text
  // and the old picker's select key into it is worse than doing nothing.
  // The ambiguity check applies BEFORE typing, not after: a filter can only remove rows,
  // so a title that matches twice here still matches twice once filtered, and typing it in
  // would leave the user's picker filtered for a walk that was never going to commit.
  const q = picker(io);
  // `km.search === true`, not truthy: the field is declared boolean but arrives from the
  // model unvalidated, and the string "false" is truthy. Typing into a list that does not
  // filter is stray keystrokes, which is the one thing this file exists to not do — so it
  // fails closed on anything that is not the literal boolean.
  if (km.search !== true || !km.select || !q || soleIndex(q.options, targetText) < 0) {
    io.note(STUCK);
    return false;
  }
  const before = io.parsedAt();
  // Typing into the search box changes the LIST, and usually the highlight with it —
  // either is proof the filter took. Neither changing means the box did not filter, which
  // is the one thing this fallback assumed and must not commit on.
  const was = q.options, wasAt = q.selected;
  const filtered = await io.sendText(targetText)
    && await waitForFrame(io, before, (f) => !sameRows(was, f.options) || f.selected !== wasAt);
  // Re-walk the filtered list rather than trusting the filter to have landed on the row.
  // -1, not 0: the filter renumbered everything, so the tapped index is spent and no index
  // we pass here means anything. An index that can never match says that plainly and sends
  // walk() down the by-text path, which is the only identity the row still has.
  if (filtered && await walk(io, km, targetText, -1)) return true;
  io.note(STUCK);
  return false;
}
