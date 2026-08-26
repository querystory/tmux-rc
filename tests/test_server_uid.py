"""server_uid() must not cache a FAILED read.

Started at boot by a systemd unit, the daemon can outlive several tmux servers — and,
the case that motivated this, start before any server exists. The old @cache froze the
no-server fallback for the process's life, so every pane_uid stayed ':0' and telemetry
from unrelated tmux servers fused together under one identity.
"""

import subprocess

import daemon.tmux as T


def _fail(_argv):
    raise subprocess.CalledProcessError(returncode=1, cmd="tmux")


def test_no_server_then_server(monkeypatch):
    monkeypatch.setattr(T, "_server_uid", None)
    monkeypatch.setattr(T, "_run", _fail)
    assert T.server_uid().endswith(":0")  # nothing to report yet

    monkeypatch.setattr(T, "_run", lambda argv: "4242\n")
    uid = T.server_uid()
    assert uid.endswith(":4242")  # the later read wins — no frozen fallback


def test_pid_change_re_derives(monkeypatch):
    monkeypatch.setattr(T, "_server_uid", None)
    monkeypatch.setattr(T, "_run", lambda argv: "1\n")
    first = T.server_uid()
    monkeypatch.setattr(T, "_run", lambda argv: "2\n")
    assert T.server_uid() != first  # a new tmux server is a new identity


def test_failure_keeps_last_good(monkeypatch):
    monkeypatch.setattr(T, "_server_uid", None)
    monkeypatch.setattr(T, "_run", lambda argv: "77\n")
    good = T.server_uid()
    monkeypatch.setattr(T, "_run", _fail)
    # A momentary failure must not invent a DIFFERENT identity — the backend would read
    # that as another server. Serve the last known one.
    assert T.server_uid() == good
