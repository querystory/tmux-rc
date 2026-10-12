"""A cursor picker's highlight is read off the screen, not trusted from the model: the
parse's `selected` is grounded on the last ❯/› row, and /send takes a walk's key only
while the picker hashes as it did in the frame the step was computed from. The
end-to-end case drives a fake digitless No/Yes menu (the shape of Claude Code's
artifact-delete prompt) in a private tmux server reached only by its own -L socket,
never $TMUX, and closes it by the pane id it created."""

import os
import shutil
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from openbus import server, tmux
from openbus.classify import classify, highlighted_row, picker_digest
from openbus.tmux import Pane

# A user turn that starts with the target's own word, above a box highlighting No.
SCREEN = """❯ Yes, please remove it.

● Remove(widget "Demo")

────────────────────
 Permanently remove "Demo"?

 ❯ No
   Yes

 Esc to cancel · Tab to amend"""

TABS = "Settings  Status   Config   Usage   Stats"
PANEL = f"""▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔ ◐ medium · /effort ▔
   {TABS}

   ╭──────────────────────╮
   │ ⌕ Search settings…   │
   ╰──────────────────────╯

   ❯ Auto-compact                true
     Thinking mode               true
     Thinking can't be turned off for Opus 5.5
     Theme                       Auto (match terminal) ›

   Type to filter · Enter/↓ to select · ↑ to tabs · Esc to clear"""

PANE = Pane("work", "0", "claude", "0", "%0", "claude", "t", "/home/x/proj")


def test_highlighted_row_reads_the_pointer_row_below_the_prompt():
    no_yes = ["No", "Yes"]
    assert highlighted_row(SCREEN, 'Permanently remove "Demo"?', no_yes) == "No"
    box = "│ Which color?   │\n│ ❯ ○ Red        │\n│   ○ Green      │"
    assert highlighted_row(box, "Which color?", ["Red", "Green"]) == "Red"
    assert highlighted_row("Effort?\n› 2. Medium\n  3. High", "Effort?", ["Medium"]) == "Medium"
    # No glyph under the prompt: the ❯ turn above it is history, not the cursor.
    assert highlighted_row("❯ Yes\nProceed?\n  No\n  Yes", "Proceed?", no_yes) is None
    assert highlighted_row(SCREEN, "Some other prompt?", no_yes) is None
    assert highlighted_row(SCREEN, "  ", no_yes) is None  # no prompt is no anchor
    # A picker that has closed: the live input box below it is not its highlight.
    closed = "Proceed?\n  No\n  Yes\n────────\n❯ Yes\n────────"
    assert highlighted_row(closed, "Proceed?", no_yes) is None
    # A search box between the prompt and the list is not the widget's edge, and a row
    # names its option ahead of a value column (Claude Code's /resume and /config).
    names = ["Auto-compact", "Thinking mode"]
    assert highlighted_row(PANEL, "Search settings…", names) == "Auto-compact"
    resume = PANEL.replace(TABS, "Resume session")
    assert highlighted_row(resume, "Resume session", names) == "Auto-compact"
    # A label's own double space is kept: only the whole label, or the label then a gap.
    title, pick = "Fix  login bug", "Pick?\n❯ Fix  login bug"
    assert highlighted_row(pick, "Pick?", [title]) == title
    assert highlighted_row(pick, "Pick?", ["Fix", title]) is None
    # The prompt's words again inside its own widget (an option, a footer) are not its anchor.
    assert highlighted_row("Pick?\n❯ No\n  Pick?", "Pick?", no_yes) == "No"
    old = "Pick?\n❯ Yes\n────────\nPick?\n  No\n  Yes"  # an earlier widget's match is history
    assert highlighted_row(old, "Pick?", no_yes) is None
    # The prompt is a whole row: a longer one heads a different picker.
    assert highlighted_row("Pick a target?\n❯ No", "Pick", no_yes) is None
    # A picker above a later one (or above the input box) is history, not the live widget.
    stacked = "│ Old?   │\n│ ❯ No   │\n╰───────╯\n│ New?   │\n│ ❯ Keep │\n╰───────╯"
    assert highlighted_row(stacked, "Old?", no_yes) is None
    assert highlighted_row(stacked, "New?", ["Keep"]) == "Keep"
    # Only a row that opens with a frame loses a closing bar; a label's own bar is kept.
    assert highlighted_row("Pick?\n❯ Fix │", "Pick?", ["Fix │"]) == "Fix │"
    assert highlighted_row("│ Pick?   │\n│ ❯ a │   │", "Pick?", ["a │"]) == "a │"


def test_classify_overrides_a_misread_anchor():
    misread = {"tool": "claude", "question": {
        "prompt": 'Permanently remove "Demo"?', "answer_style": "cursor",
        "options": ["No", "Yes"], "selected": 1,
        "keymap": {"next": "Down", "prev": "Up", "select": "Enter"},
    }}
    result = classify(PANE, f"{tmux.VISIBLE_SCREEN}\n{SCREEN}", lambda s, t: misread)
    assert result["question"]["selected"] == 0  # grounded off the screen
    assert result["question"]["picker"] == picker_digest(SCREEN, 'Permanently remove "Demo"?')
    # A colour-only highlight hashes alike whichever row it is on: no walk is held to it.
    plain = SCREEN.replace(" ❯ No", "   No")
    result = classify(PANE, f"{tmux.VISIBLE_SCREEN}\n{plain}", lambda s, t: misread)
    assert "picker" not in result["question"]


def test_a_picker_digest_changes_with_anything_drawn_from_its_prompt_down():
    """What /send holds a walk's keys to: the parse's own record of the picker."""
    wait = "Delay?\n❯ Wait 30s\n  Now"
    assert picker_digest(wait, "Delay?") == picker_digest("old output\n" + wait, "Delay?")
    assert picker_digest(wait, "Delay?") != picker_digest(wait.replace("30s", "60s"), "Delay?")
    # A replacement drawn with a colour highlight (no glyph) below the old picker.
    assert picker_digest(wait, "Delay?") != picker_digest(wait + "\nNew?\n  Red", "Delay?")
    assert picker_digest(wait, "Other?") is None
    # A generic prompt approves whatever its widget shows above it: that is part of it,
    # and the conversation above the widget's edge is not.
    asked = "Do you want to proceed?"
    dialog = SCREEN.replace('Permanently remove "Demo"?', asked)
    other = dialog.replace("────────────────────", "────────────────────\n rm -rf build")
    assert picker_digest(dialog, asked) != picker_digest(other, asked)
    long = dialog.replace("────────────────────", "────────────────────" + "\n echo step" * 20)
    assert picker_digest(long, asked) != picker_digest(long.replace("echo", "rm -rf"), asked)
    # A command's own rule-like row is indented: the dialog's edge is still the one above.
    ruled = dialog.replace("────────────────────", "────────────────────\n cat x\n   ─────")
    assert picker_digest(ruled, asked) != picker_digest(ruled.replace("cat x", "rm -rf"), asked)
    above = dialog.replace("remove it", "drop it")  # a user turn above the widget's edge
    assert picker_digest(dialog, asked) == picker_digest(above, asked)


# Claude Code's /resume: a live count after the prompt that the model reads as chrome.
RESUME = """▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
   Resume session (1 of 12)
   ╭──────────────────────────────────╮
   │ ⌕ Search…                        │
   ╰──────────────────────────────────╯
   ❯ Tune the lighthouse scheduler
     4 minutes ago · main · 2.1MB

     Draft the harbor release notes
     1 hour ago · docs/harbor · 880KB

     Type to search · Esc to cancel"""
SESSIONS = ["Tune the lighthouse scheduler", "Draft the harbor release notes"]


def test_a_picker_prompt_may_close_on_a_count():
    assert highlighted_row(RESUME, "Resume session", SESSIONS) == SESSIONS[0]
    assert picker_digest(RESUME, "Resume session") is not None
    assert picker_digest(RESUME, "Resume session (1 of 12)") is not None
    # More words, outside a parenthetical, are still a different picker.
    longer = RESUME.replace("(1 of 12)", "for deployment (1 of 12)")
    assert picker_digest(longer, "Resume session") is None
    other = RESUME.replace("(1 of 12)", "(archived)")  # only a live count is dropped
    assert picker_digest(other, "Resume session") is None


def test_a_walk_down_a_counted_picker_is_sent(monkeypatch):
    sent, reparsed = [], []
    monkeypatch.setattr(tmux, "find_pane", lambda p: PANE)
    monkeypatch.setattr(tmux, "capture_pane", lambda *a, **k: f"{tmux.VISIBLE_SCREEN}\n{RESUME}")
    monkeypatch.setattr(tmux, "at_password_prompt", lambda p: False)
    monkeypatch.setattr(tmux, "_run", lambda args, *a, **k: sent.append(args) or "")
    server.app.state.watcher = SimpleNamespace(request_reparse=reparsed.append)
    read = {"tool": "claude", "question": {
        "prompt": "Resume session (1 of 12)", "answer_style": "cursor", "options": SESSIONS,
        "selected": 1, "keymap": {"next": "Down", "prev": "Up", "select": "Enter"}}}
    question = classify(PANE, tmux.capture_pane(PANE.id), lambda s, t: read)["question"]
    assert question["selected"] == 0  # off the screen
    assert question["prompt"] == "Resume session"  # the count redraws with every move

    def send(picker):
        body = {"keys": "Down", "enter": False, "literal": False,
                "prompt": question["prompt"], "picker": picker}
        return TestClient(server.app).post(f"/api/panes/{PANE.id}/send", json=body).status_code

    assert send(question["picker"]) == 200
    assert any("Down" in args for args in sent)
    # Read off another frame, or off none: refused, and reread so the walk can go again.
    for stale in ("an earlier frame", ""):
        reparsed.clear()
        assert send(stale) == 409
        assert reparsed
    with monkeypatch.context() as closing:  # the pane closed after it was found
        closing.setattr(tmux, "capture_pane", lambda *a, **k: subprocess.check_output("false"))
        assert send(question["picker"]) == 409


MENU = """
import sys, time, tty
rows, at = ["No", "Yes"], 0
tty.setraw(0)
def draw():
    marks = "".join(f" {'❯' if i == at else ' '} {r}\\r\\n" for i, r in enumerate(rows))
    sys.stdout.write("\\x1b[H\\x1b[2J❯ Yes, please remove it.\\r\\n\\r\\n Remove it?\\r\\n" + marks)
    sys.stdout.flush()
draw()
while (key := sys.stdin.read(1)) != "\\r":
    if key == "\\x1b":
        at = min(max(at + {"B": 1, "A": -1}.get(sys.stdin.read(2)[-1], 0), 0), 1)
        draw()
sys.stdout.write(f"SELECTED {rows[at]}\\r\\n")
sys.stdout.flush()
time.sleep(30)
"""


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")
def test_a_step_goes_only_to_the_screen_it_was_read_off(tmp_path, monkeypatch):
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    socket = f"tmuxrc-cursor-{uuid.uuid4().hex[:12]}"

    def run(args, stdin=None):
        return subprocess.run(["tmux", "-f", "/dev/null", "-L", socket, *args], input=stdin,
                              env=env, text=True, capture_output=True, check=True).stdout

    (tmp_path / "menu.py").write_text(MENU)
    pane = run(["new-session", "-d", "-x", "60", "-y", "12", "-P", "-F", "#{pane_id}",
                f"{sys.executable} {tmp_path / 'menu.py'}"]).strip()
    try:
        monkeypatch.setattr(tmux, "_run", run)
        monkeypatch.setattr(tmux, "pane_pid", lambda p: tmux.find_pane(p).pid)
        parsed = {"pane_id": pane, "question": {}}
        def reparse():  # the watcher's parse records the picker as it is drawn now
            screen = tmux.capture_pane(pane, mark_dim=True)
            parsed["question"]["picker"] = picker_digest(screen, "Remove it?")

        server.app.state.watcher = SimpleNamespace(request_reparse=lambda p: None,
                                                   states=[parsed])
        client = TestClient(server.app)

        def screen_until(text):
            for _ in range(100):
                if text in (shown := tmux.capture_pane(pane, lines=0)):
                    return shown
                time.sleep(0.05)
            raise AssertionError(shown)

        def send(keys, prompt="Remove it?"):
            body = {"keys": keys, "enter": False, "literal": False, "prompt": prompt,
                    "picker": parsed["question"]["picker"]}
            return client.post(f"/api/panes/{pane}/send", json=body)

        screen_until(" ❯ No")
        reparse()
        assert send("Down", prompt="Remove that?").status_code == 409  # another picker's
        assert send("Down").status_code == 200
        screen_until(" ❯ Yes")
        # The screen moved since the frame the step was read off: nothing goes, not even
        # the select a walk would press here, until it is reread.
        refused = send("Enter")
        assert refused.status_code == 409
        assert "changed" in refused.json()["detail"]
        reparse()
        assert send("Enter").status_code == 200
        assert "SELECTED Yes" in screen_until("SELECTED")
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-pane", "-t", pane], env=env,
                       check=False, capture_output=True)
