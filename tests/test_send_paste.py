"""Typed text reaches the pane as one bracketed paste, then one Return.

Typed with `send-keys -l`, a long reply (an auth code pasted on the phone) reached Codex
as a stream of fast keys: each 1KB read became a "[Pasted Content 1024 chars]"
placeholder, the tail waited in its paste-burst buffer for the next key, and that key was
our Return, eaten as part of the paste. The draft sat unsent. Bracketed-paste marks tell
the app where the paste ends, so the Return after them submits."""
import os
import shutil
import subprocess
import time

import pytest

import openbus.tmux as T


def typed(record):
    """A fake tmux._run that hands `record` what each call put in the pane: a paste's
    text once paste-buffer delivers it (a refused paste delivers nothing), or a
    send-keys' last argument."""
    loaded = {}

    def run(args, stdin=None):
        if args[0] == "load-buffer":
            loaded[args[2]] = stdin
        elif args[0] == "paste-buffer":
            record(loaded.pop(args[args.index("-b") + 1]))
        elif args[0] != "delete-buffer":
            record(args[-1])
        return ""
    return run


def test_long_text_is_one_bracketed_paste_then_one_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_run", lambda args, stdin=None: calls.append((args, stdin)))
    text = "a" + "🚀" * 9000  # past tmux's 16KB argv cap, which stdin does not have
    T.send_keys("%1", text, enter=True, paste=True)
    (load, text_in), (paste, _), (enter, _) = calls
    name = load[2]
    assert (load, text_in) == (["load-buffer", "-b", name, "-"], text)
    assert paste == ["paste-buffer", "-r", "-p", "-d", "-b", name, "-t", "%1"]
    assert enter == ["send-keys", "-t", "%1", "Enter"]


def test_keystrokes_are_not_pasted(monkeypatch):
    # a menu's shortcut digit or a typed key must reach the app (or tmux's copy mode) as
    # a key, not as pasted text
    calls = []
    monkeypatch.setattr(T, "_run", lambda args, stdin=None: calls.append(args))
    T.send_keys("%1", "1", enter=False)
    assert calls == [["send-keys", "-t", "%1", "-l", "1"]]


def test_a_refused_paste_leaves_no_buffer(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_run", lambda args, stdin=None: calls.append(args))
    pids = iter(["1234", "replacement"])
    monkeypatch.setattr(T, "pane_pid", lambda pane_id: next(pids))
    with pytest.raises(T.PaneChangedError):
        T.send_keys("%1", "draft", paste=True)
    assert [c[0] for c in calls] == ["load-buffer", "delete-buffer"]
    assert calls[1][2] == calls[0][2]


def test_the_app_sees_one_marked_paste_and_one_return(tmp_path, monkeypatch):
    """End to end on a private tmux server (its own -S socket, never $TMUX; the pane is
    closed by its id, which ends it): a pane that asked for bracketed paste, as Codex and
    Claude Code do, must read a keystroke bare, then the marks around the whole draft,
    its newline still a LF, and then a lone Return. A key typed while tmux's copy mode is
    up is copy mode's, and never reaches the app."""
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    socket, out = str(tmp_path / "paste.sock"), tmp_path / "input"
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}

    def run(args, stdin=None):
        return subprocess.run(["tmux", "-f", "/dev/null", "-S", socket, *args], input=stdin,
                              env=env, text=True, capture_output=True, check=True).stdout

    monkeypatch.setattr(T, "_run", run)
    pane = run(["new-session", "-d", "-P", "-F", "#{pane_id}", "sh", "-c",
                f"stty raw -echo; printf '\\033[?2004h'; touch {out}.ready; exec cat > {out}"]
               ).strip()
    try:
        for _ in range(100):
            if (tmp_path / "input.ready").exists():
                break
            time.sleep(0.05)
        text = "an invented sign-in code\n" + "q7Zx" * 600
        run(["copy-mode", "-t", pane])
        T.send_keys(pane, "q", enter=False)  # copy mode's key: it leaves, the app sees none
        T.send_keys(pane, "j", enter=False)  # a keystroke: no marks
        T.send_keys(pane, text, paste=True)
        want = f"j\x1b[200~{text}\x1b[201~\r".encode()
        for _ in range(100):
            if len(out.read_bytes()) >= len(want):
                break
            time.sleep(0.05)
        assert out.read_bytes() == want
    finally:
        subprocess.run(["tmux", "-S", socket, "kill-pane", "-t", pane], env=env,
                       check=False, capture_output=True)
