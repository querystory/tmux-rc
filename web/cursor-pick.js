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
const POLL_MS = 120;

// One wording for every dead end. They are all the same thing from the user's side —
// this UI cannot drive that widget — and the alternative to saying it is the silent
// no-op the whole feature exists to remove.
const STUCK = "Can't select that row from here — use the keyboard.";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Wait for a frame NEWER than `after`. A cursor move leaves the question itself
// unchanged — same picker, same prompt, same options — so the card's ordinary reparse
// marker is no signal here: it is cleared by the PROMPT changing, which never happens
// mid-walk, and waiting on it returned false after every single move (the walk stopped
// after one step and fell straight through to the fallback). parsed_at is the thing that
// actually moves: the server forces a reparse on every send, and the frame it produces is
// the new highlight position this walk needs before it can choose the next step.
async function waitForParse(io, after, ms = PARSE_WAIT_MS) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    if (!io.question()) return false; // picker closed under us — stop, don't keep typing
    if (io.parsedAt() > after) return true;
  }
  return false;
}

// MOVE, VERIFY, THEN SELECT — returns true only once the row is committed.
//
// The anchor (question.selected) comes from a frame that is already stale by the time a
// tap arrives, so each pass re-reads the screen instead of firing move+move+Enter in one
// burst. That burst would be faster and would silently pick the wrong session whenever
// the anchor had moved, which is the same class of bug in a new costume.
async function walk(io, km, targetText, targetIndex) {
  // `<=` because the budget counts MOVES, and a row exactly CURSOR_MAX_STEPS away is
  // reached by the last of them: without the extra pass the loop spends its whole budget
  // arriving and then exits without ever noticing it had arrived, reporting failure while
  // the highlight sits on the requested row.
  for (let moves = 0; moves <= CURSOR_MAX_STEPS; moves++) {
    const q = io.question();
    // The picker closed, or the pane moved on to something else — stop rather than type
    // into whatever replaced it.
    if (!q || q.answer_style !== "cursor" || !Array.isArray(q.options)) return false;
    // Prefer the index the user actually tapped, for exactly as long as it still names
    // their row. Two sessions can share a title, and indexOf would resolve both taps to
    // the first of them — the wrong session, selected confidently. Once a filter or a
    // scroll has renumbered the list that index stops matching, and the text is then the
    // only identity the row has left.
    const want = q.options[targetIndex] === targetText
      ? targetIndex
      : q.options.indexOf(targetText);
    const at = typeof q.selected === "number" ? q.selected : null;
    if (want < 0) return false; // row is no longer on screen
    if (at === null) return false; // no trustworthy anchor — walking blind picks a row
    if (at === want) return km.select ? io.sendKey(km.select) : false;
    const key = at < want ? km.next : km.prev;
    if (!key) return false; // the widget never advertised this direction — don't invent one
    const before = io.parsedAt();
    if (!(await io.sendKey(key))) return false; // undelivered: the anchor is now a lie
    if (!(await waitForParse(io, before))) return false;
  }
  return false;
}

// Drive the picker to `targetText` and commit it. `targetIndex` is where the row sat in
// the frame the user tapped.
export async function pickCursorRow(io, targetText, targetIndex) {
  const km = (io.question() || {}).keymap || {};
  if (await walk(io, km, targetText, targetIndex)) return true;
  // Fallback: the widget's own search box. It needs BOTH bindings advertised — typing
  // filters the list but does not commit it, so the obvious shortcut of appending Enter
  // is precisely the unadvertised guess this file exists to stop. A picker that binds Tab
  // to select, or binds nothing, gets told rather than guessed at.
  if (!km.search || !km.select || !io.question()) { io.note(STUCK); return false; }
  const before = io.parsedAt();
  const filtered = await io.sendText(targetText) && await waitForParse(io, before);
  // Re-walk the filtered list rather than trusting the filter to have landed on the row:
  // the same title can still match twice, and the commit is verified here like any other.
  if (filtered && await walk(io, km, targetText, 0)) return true;
  io.note(STUCK);
  return false;
}
