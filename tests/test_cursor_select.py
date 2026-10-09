"""A cursor picker's highlight is read off the screen, not trusted from the model: the
parse's `selected` is grounded on the last ❯/› row, and /send re-reads that row before it
presses a picker's select. The end-to-end case drives a fake digitless No/Yes menu (the
shape of Claude Code's artifact-delete prompt) in a private tmux server reached only by
its own -L socket, never $TMUX, and closes it by the pane id it created."""

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
from openbus.classify import classify, highlighted_row
from openbus.tmux import Pane

# A user turn that starts with the target's own word, above a box highlighting No.
SCREEN = """❯ Yes, please remove it.

● Remove(widget "Demo")

────────────────────
 Permanently remove "Demo"?

 ❯ No
   Yes

 Esc to cancel · Tab to amend"""


def test_highlighted_row_reads_the_pointer_row_below_the_prompt():
    assert highlighted_row(SCREEN, 'Permanently remove "Demo"?') == "No"
    box = "│ Which color?   │\n│ ❯ ○ Red        │\n│   ○ Green      │"
    assert highlighted_row(box, "Which color?") == "Red"
    assert highlighted_row("Effort?\n› 2. Medium\n  3. High", "Effort?") == "Medium"
    # No glyph under the prompt: the ❯ turn above it is history, not the cursor.
    assert highlighted_row("❯ Yes\nProceed?\n  No\n  Yes", "Proceed?") is None
    assert highlighted_row(SCREEN, "Some other prompt?") is None
    # A picker that has closed: the live input box below it is not its highlight.
    closed = "Proceed?\n  No\n  Yes\n────────\n❯ Yes\n────────"
    assert highlighted_row(closed, "Proceed?") is None


def test_classify_overrides_a_misread_anchor():
    misread = {"tool": "claude", "question": {
        "prompt": 'Permanently remove "Demo"?', "answer_style": "cursor",
        "options": ["No", "Yes"], "selected": 1,
        "keymap": {"next": "Down", "prev": "Up", "select": "Enter"},
    }}
    pane = Pane("work", "0", "claude", "0", "%0", "claude", "t", "/home/x/proj")
    result = classify(pane, f"{tmux.VISIBLE_SCREEN}\n{SCREEN}", lambda s, t: misread)
    assert result["question"]["selected"] == 0


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
def test_select_is_refused_until_the_highlight_is_on_the_row(tmp_path, monkeypatch):
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
        server.app.state.watcher = SimpleNamespace(request_reparse=lambda p: None)
        client = TestClient(server.app)

        def screen_until(text):
            for _ in range(100):
                if text in (shown := tmux.capture_pane(pane, lines=0)):
                    return shown
                time.sleep(0.05)
            raise AssertionError(shown)

        def send(keys, row=None):
            on = row and {"prompt": "Remove it?", "row": row}
            body = {"keys": keys, "enter": False, "literal": False, "on_row": on}
            return client.post(f"/api/panes/{pane}/send", json=body)

        screen_until(" ❯ No")
        refused = send("Enter", "Yes")  # the highlight is still on No
        assert refused.status_code == 409
        assert "Yes" in refused.json()["detail"]
        assert send("Down").status_code == 200
        screen_until(" ❯ Yes")
        assert send("Enter", "Yes").status_code == 200
        assert "SELECTED Yes" in screen_until("SELECTED")
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-pane", "-t", pane], env=env,
                       check=False, capture_output=True)
