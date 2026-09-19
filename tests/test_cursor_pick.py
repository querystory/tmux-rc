"""Behavioural checks for the shared cursor walk in web/cursor-pick.js.

The module is plain ESM with no DOM dependency — everything surface-specific arrives as
an `io` object — so node can import it directly and a fake picker can stand in for tmux.
Each scenario builds a FakePicker (a highlight index, an options list, an advertised
keymap) plus a script of what each send is allowed to do, runs the walk against it, and
asserts on the keys that actually went out and where the highlight ended up.

Sends are logged and answered synchronously; parsed_at ticks on every delivered send, so
the walk's wait-for-a-fresh-frame resolves on its first poll. A send scripted as
undelivered ticks NOTHING, which is the whole point of the delivery contract: the walk
must not compute its next step from a move that never happened.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = (Path(__file__).resolve().parent.parent / "web" / "cursor-pick.js").as_uri()

FULL_KM = {"next": "Down", "prev": "Up", "select": "Enter", "search": True}
NO_SEARCH_KM = {"next": "Down", "prev": "Up", "select": "Enter"}
ROWS = ["alpha", "beta", "gamma", "delta"]

# (description, picker, target text, tapped index, expected {sent, selected, notes})
CASES = [
    (
        "walks down to the tapped row and commits with the advertised select key",
        {"options": ROWS, "selected": 0, "keymap": FULL_KM},
        "gamma", 2,
        {"sent": ["Down", "Down", "Enter"], "selected": 2, "notes": []},
    ),
    (
        "walks UP when the row is above the highlight",
        {"options": ROWS, "selected": 3, "keymap": FULL_KM},
        "beta", 1,
        {"sent": ["Up", "Up", "Enter"], "selected": 1, "notes": []},
    ),
    (
        "already on the row: commits without moving",
        {"options": ROWS, "selected": 1, "keymap": FULL_KM},
        "beta", 1,
        {"sent": ["Enter"], "selected": 1, "notes": []},
    ),
    (
        # The whole point of reading the footer: a widget that never said how to commit
        # gets told to the user rather than guessed at with a bare Enter.
        "no advertised select key and no search: says so instead of guessing Enter",
        {"options": ROWS, "selected": 2, "keymap": {"next": "Down", "prev": "Up"}},
        "gamma", 2,
        {"sent": [], "selected": 2, "notes": 1},
    ),
    (
        "no advertised direction: says so instead of inventing an arrow",
        {"options": ROWS, "selected": 0, "keymap": {"select": "Enter"}},
        "gamma", 2,
        {"sent": [], "selected": 0, "notes": 1},
    ),
    (
        # A wrong anchor walks confidently to the wrong row, so an unknown one must not
        # walk at all.
        "unknown anchor falls back to the search box rather than walking blind",
        {"options": ROWS, "selected": None, "keymap": FULL_KM, "filters": True},
        "gamma", 2,
        {"sent": ["gamma", "Enter"], "selected": 0, "notes": []},
    ),
    (
        "unknown anchor with no search binding: nothing is sent at all",
        {"options": ROWS, "selected": None, "keymap": NO_SEARCH_KM},
        "gamma", 2,
        {"sent": [], "selected": None, "notes": 1},
    ),
    (
        # Regression, Copilot: the budget counts MOVES, and the check happens before each
        # move — a row exactly CURSOR_MAX_STEPS away used to be reached by the last move
        # and then reported as a failure, with the highlight sitting on it.
        "a row exactly CURSOR_MAX_STEPS away is still committed",
        {"options": [f"r{i}" for i in range(20)], "selected": 0, "keymap": FULL_KM},
        "r12", 12,
        {"sent": ["Down"] * 12 + ["Enter"], "selected": 12, "notes": []},
    ),
    (
        "a row beyond the step budget hands over to the search box",
        {"options": [f"r{i}" for i in range(20)], "selected": 0, "keymap": FULL_KM},
        "r13", 13,
        # 13 moves is one past the budget, so the walk stops with the highlight parked
        # partway and the search box finishes the job.
        {"sent": ["Down"] * 13 + ["r13", "Enter"], "selected": 13, "notes": []},
    ),
    (
        # Regression, Copilot: indexOf resolves both duplicates to the first one, so
        # tapping the SECOND "beta" used to select the first.
        "a duplicate row label follows the tapped index, not the first match",
        {"options": ["beta", "alpha", "beta"], "selected": 0, "keymap": FULL_KM},
        "beta", 2,
        {"sent": ["Down", "Down", "Enter"], "selected": 2, "notes": []},
    ),
    (
        # Regression, Copilot: send() used to swallow POST failures, so the walk carried
        # on believing it had moved. `tick_on_fail` is what makes that visible — an
        # ordinary watcher tick lands a fresh frame while the key is still undelivered, so
        # waiting for a new parse is NOT on its own evidence that anything moved. Without
        # the delivery check the walk reads that frame, counts the move it never made, and
        # commits a row further down.
        "an undelivered move aborts the walk instead of counting as a step",
        {"options": ROWS, "selected": 0, "keymap": NO_SEARCH_KM,
         "fail_send": 2, "tick_on_fail": True},
        "delta", 3,
        {"sent": ["Down", "Down"], "selected": 1, "notes": 1},
    ),
    (
        # The fallback needs BOTH bindings: typing filters the list but does not commit
        # it, so a search box with no advertised select key is still a dead end — and
        # typing into it first would leave the picker filtered and the user staring at it.
        "search advertised but no select key: the fallback is not attempted",
        {"options": ROWS, "selected": None, "keymap": {"next": "Down", "prev": "Up",
                                                       "search": True}},
        "gamma", 2,
        {"sent": [], "selected": None, "notes": 1},
    ),
    (
        # The pane can move on to an entirely different prompt between two frames. Once
        # it is no longer a cursor list, the remaining moves would be typed into whatever
        # replaced it.
        "a picker that turns into an ordinary question mid-walk stops the walk",
        {"options": ROWS, "selected": 0, "keymap": NO_SEARCH_KM, "morph_after": 1},
        "delta", 3,
        {"sent": ["Down"], "selected": 1, "notes": 1},
    ),
    (
        # A cursor question with no options at all is malformed; reading an index out of
        # it is how a guard-less walk throws inside a click handler and does nothing.
        "a cursor question carrying no options is refused, not indexed into",
        {"options": ROWS, "selected": 0, "keymap": NO_SEARCH_KM, "no_options": True},
        "delta", 3,
        {"sent": [], "selected": 0, "notes": 1},
    ),
    (
        "the picker closing mid-walk stops the walk",
        {"options": ROWS, "selected": 0, "keymap": NO_SEARCH_KM, "close_after": 1},
        "delta", 3,
        {"sent": ["Down"], "selected": 1, "notes": 1},
    ),
    (
        # The fallback types the row into the search box WITHOUT Enter and then commits
        # with the advertised key. Appending a literal Enter — what `answer()` does — is
        # the unadvertised guess this module exists to stop.
        "a widget with search+select but no arrows filters instead of walking",
        {"options": ROWS, "selected": 0, "keymap": {"select": "Enter", "search": True},
         "filters": True},
        "gamma", 2,
        {"sent": ["gamma", "Enter"], "selected": 0, "notes": []},
    ),
]


def render_script() -> str:
    return f"""
import assert from "node:assert/strict";
import {{ pickCursorRow }} from {json.dumps(MODULE)};

// A picker that answers exactly like the daemon does: parsed_at only advances on a
// DELIVERED send, and Up/Down move the highlight the way the real widget would.
function fake(spec) {{
  const p = {{
    options: spec.options.slice(), selected: spec.selected, keymap: spec.keymap,
    parsed_at: 1, sent: [], notes: [], open: true, sends: 0, style: "cursor",
  }};
  const deliver = (what) => {{
    p.sent.push(what);
    p.sends += 1;
    if (spec.fail_send === p.sends) {{
      // An unrelated watcher tick can still land a frame while the key never went out.
      if (spec.tick_on_fail) p.parsed_at += 1;
      return false;
    }}
    p.parsed_at += 1;
    if (spec.close_after === p.sends) p.open = false;
    if (spec.morph_after === p.sends) p.style = "text";
    return true;
  }};
  p.io = {{
    question: () => p.open
      ? {{
        answer_style: p.style, selected: p.selected, keymap: p.keymap,
        options: spec.no_options ? undefined : p.options,
      }}
      : null,
    parsedAt: () => p.parsed_at,
    sendKey: async (k) => {{
      const moved = deliver(k);
      if (!moved) return false;
      if (k === p.keymap.next && p.selected !== null) p.selected += 1;
      if (k === p.keymap.prev && p.selected !== null) p.selected -= 1;
      return true;
    }},
    sendText: async (t) => {{
      const ok = deliver(t);
      // "filters" models a widget that narrows to the typed row and lands the highlight
      // on it; without it the typed text merely re-reads the same list.
      if (ok && spec.filters) {{ p.options = [t]; p.selected = 0; }}
      return ok;
    }},
    note: (m) => p.notes.push(m),
  }};
  return p;
}}

const failures = [];
async function check(description, spec, target, index, want) {{
  try {{
    const p = fake(spec);
    await pickCursorRow(p.io, target, index);
    assert.deepEqual(p.sent, want.sent, "keys sent");
    if (!spec.filters) assert.deepEqual(p.selected, want.selected, "final highlight");
    assert.deepEqual(p.notes.length, Array.isArray(want.notes) ? 0 : want.notes, "notices");
  }} catch (err) {{ failures.push(`${{description}}: ${{err.message}}`); }}
}}

for (const [description, spec, target, index, want] of {json.dumps(CASES)}) {{
  await check(description, spec, target, index, want);
}}

if (failures.length) {{
  console.error(failures.join("\\n\\n"));
  process.exit(1);
}}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cursor_pick_behaviour(tmp_path):
    script = tmp_path / "cursor_pick_test.mjs"
    script.write_text(render_script())
    res = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=120, check=False,
    )
    assert res.returncode == 0, res.stderr
