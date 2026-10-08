"""Password prompts, end to end against a private tmux server: detected from the tty's
echo flag and the cursor's row, answered through the composer's secret field, and never
put in argv or the audit trail. The server is reached only by its own -S socket (never
$TMUX), and its panes are closed by the ids created here, which ends it."""

import logging
import os
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from openbus import server, tmux, watcher

AT_PROMPT = tmux.at_password_prompt  # the real one: conftest stubs it for unit tests

SECRET = "hunter2-correct-horse"
ENV = {k: v for k, v in os.environ.items() if k != "TMUX"}


@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    socket, argvs, created = str(tmp_path / "secret.sock"), [], []

    def run(args, stdin=None):
        argvs.append(args)
        return subprocess.run(["tmux", "-f", "/dev/null", "-S", socket, *args], input=stdin,
                              env=ENV, text=True, capture_output=True, check=True).stdout

    def spawn(command):
        verb = ["new-window", "-d", "-t", "t:"] if created else ["new-session", "-d", "-s", "t"]
        created.append(run([*verb, "-P", "-F", "#{pane_id}", command]).strip())
        return created[-1]

    monkeypatch.setattr(tmux, "_run", run)
    monkeypatch.setattr(tmux, "pane_pid", lambda pane_id: tmux.find_pane(pane_id).pid)
    monkeypatch.setattr(tmux, "at_password_prompt", AT_PROMPT)
    yield SimpleNamespace(run=run, spawn=spawn, argvs=argvs)
    for pane_id in created:
        subprocess.run(["tmux", "-S", socket, "kill-pane", "-t", pane_id], env=ENV,
                       check=False, capture_output=True)


def _until(check):
    for _ in range(100):
        if check():
            return True
        time.sleep(0.05)
    return False


def test_password_prompts_are_detected_answered_and_never_logged(private_tmux, caplog):
    # ICANON off, as sudo prompts on a tty an earlier app left raw: seen in the field.
    prompt = private_tmux.spawn(
        """bash -c 'stty -icanon; read -s -p "Password: " x; echo; echo "got:$x"; sleep 30'""")
    canonical = private_tmux.spawn("""bash -c 'read -s -p "[sudo] password for x: " x'""")
    echoing = private_tmux.spawn("cat")
    # A full-screen app runs with echo off too, like an agent TUI, vim or an idle shell:
    # a "password:" on the screen but not on the cursor's row is not a prompt.
    raw = private_tmux.spawn("""python3 -c 'import sys, tty; print("Password:"); \
print("$ ", end="", flush=True); tty.setraw(0); sys.stdin.read(1)'""")
    pane = lambda pane_id: tmux.find_pane(pane_id)  # noqa: E731 - fresh flags per read
    assert _until(lambda: pane(prompt).secret and pane(canonical).secret)
    assert _until(lambda: "Password:" in tmux.capture_pane(prompt))
    assert not pane(echoing).secret
    assert _until(lambda: "$" in tmux.capture_pane(raw))
    time.sleep(0.2)  # let python reach setraw
    assert not pane(raw).secret

    state = {}
    watcher._stamp_identity(state, pane(prompt))
    assert state["secret"] is True

    server.app.state.watcher = SimpleNamespace(request_reparse=lambda p: None)
    client = TestClient(server.app)
    caplog.set_level(logging.INFO)
    # A stale page cannot type a password into a pane that echoes.
    assert client.post(f"/api/panes/{echoing}/compose",
                       files=[("secret", (None, SECRET))]).status_code == 409
    # Plain text is refused at the prompt: it would ride send-keys argv and the audit line.
    # So is a "key name" tmux would just type.
    assert client.post(f"/api/panes/{prompt}/send", json={"keys": SECRET}).status_code == 409
    assert client.post(f"/api/panes/{prompt}/send",
                       json={"keys": SECRET, "literal": False}).status_code == 409
    # One line only: the rest of a multi-line "password" would run as the shell's input.
    assert client.post(f"/api/panes/{prompt}/compose",
                       files=[("secret", (None, f"{SECRET}\nls"))]).status_code == 400
    assert client.post(f"/api/panes/{prompt}/compose",
                       files=[("text", (None, SECRET))]).status_code == 409
    assert client.post(f"/api/panes/{prompt}/compose", files=[
        ("secret", (None, SECRET)), ("text", (None, "x"))]).status_code == 400

    assert client.post(f"/api/panes/{prompt}/compose",
                       files=[("secret", (None, SECRET))]).status_code == 200
    assert _until(lambda: f"got:{SECRET}" in tmux.capture_pane(prompt))
    assert _until(lambda: not pane(prompt).secret)  # `sleep` runs with echo back on

    assert not any(SECRET in " ".join(args) for args in private_tmux.argvs)
    assert SECRET not in caplog.text
    assert "AUDIT compose" in caplog.text and "secret" in caplog.text
    assert not private_tmux.run(["list-buffers"]).strip()  # the one-shot buffer is gone
