"""TMUXRC_SCRATCH_ADVERTISE exports the scratch dir and URL to tmux's global environment,
and does nothing unless opted in. Runs against a private tmux server, never the user's."""

import shutil
import subprocess

import pytest

from openbus import server, tmux


def test_advertise_scratch_only_when_opted_in(tmp_path, monkeypatch):
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    # The server copies its starting environment, so keep the caller's own values out.
    base = ["env", "-u", "TMUX", "-u", "TMUXRC_SCRATCH_DIR", "-u", "TMUXRC_SCRATCH_URL",
            "tmux", "-S", str(tmp_path / "tmux.sock")]

    def run(args):
        return subprocess.run([*base, *args], capture_output=True, text=True, check=True,
                              timeout=10).stdout

    def shown():
        return run(["show-environment", "-g"])

    pane = run(["new-session", "-d", "-P", "-F", "#{pane_id}", "sleep 60"]).strip()
    try:
        monkeypatch.setattr(tmux, "_run", run)
        monkeypatch.setattr(server, "_scratch_dir", str(tmp_path))
        monkeypatch.setenv("TMUXRC_SCRATCH_URL", "https://host.example/scratch")
        monkeypatch.delenv("TMUXRC_SCRATCH_ADVERTISE", raising=False)
        server._advertise_scratch()
        assert "TMUXRC_SCRATCH" not in shown()
        monkeypatch.setenv("TMUXRC_SCRATCH_ADVERTISE", "1")
        server._advertise_scratch()
        assert f"TMUXRC_SCRATCH_DIR={tmp_path}\n" in shown()
        assert "TMUXRC_SCRATCH_URL=https://host.example/scratch\n" in shown()
        # tmux outlives the daemon: a restart with less configured clears what went away.
        monkeypatch.delenv("TMUXRC_SCRATCH_URL")
        server._advertise_scratch()
        assert "TMUXRC_SCRATCH_URL" not in shown()
        assert f"TMUXRC_SCRATCH_DIR={tmp_path}\n" in shown()
        monkeypatch.setenv("TMUXRC_SCRATCH_URL", "https://host.example/scratch")
        monkeypatch.setattr(server, "_scratch_dir", None)
        server._advertise_scratch()
        assert "TMUXRC_SCRATCH" not in shown()
    finally:
        # Ending the only pane ends this private server; never kill-server.
        run(["kill-pane", "-t", pane])
