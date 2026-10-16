"""The /live long-poll (docs/design/live-view.md): a round returns a new colored frame
the instant the screen's hash changes, or a tiny no-change reply after the hold expires;
tmux failures map through _pane_err. Drives the async handler directly with monkeypatched
capture / sleep / clock so no real tmux or wall-time is involved."""

import asyncio
import subprocess

import pytest

from openbus import server


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _inline_to_thread(monkeypatch):
    """Run the capture inline instead of on a real worker thread, so the test doesn't
    depend on the event loop's own clock (which shares time.monotonic with the code
    under test) advancing between iterations."""

    async def inline(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(server.asyncio, "to_thread", inline)


def test_changed_frame_returns_immediately(monkeypatch):
    monkeypatch.setattr(server.tmux, "capture_live", lambda pid: ("hello", 7, 80))
    # client is showing an empty/old hash, so the first capture already differs
    out = _run(server.live_frame("%1", frame=""))
    assert out["text"] == "hello"
    assert (out["top"], out["width"]) == (7, 80)  # where the client pages back from
    assert out["frame"] and "text" in out


def test_unchanged_holds_then_replies_no_text(monkeypatch):
    monkeypatch.setattr(server.tmux, "capture_live", lambda pid: ("same", 0, 80))
    # Don't fake the clock (it's shared with asyncio's loop) — just collapse the hold
    # and check interval to ~0 so the real timing loop expires almost immediately.
    monkeypatch.setattr(server, "LIVE_HOLD_SECONDS", 0.0)
    monkeypatch.setattr(server, "LIVE_CHECK_SECONDS", 0.0)
    h = server.tmux.frame_id("same", 0, 80)  # client already holds the current hash
    out = _run(server.live_frame("%1", frame=h))
    assert out == {"frame": h}  # no change, past deadline → {frame} only, no "text"


def test_full_not_truncated_hash(monkeypatch):
    monkeypatch.setattr(server.tmux, "capture_live", lambda pid: ("x", 0, 80))
    out = _run(server.live_frame("%1", frame=""))
    assert len(out["frame"]) == 32  # full md5 hex, not truncated (collision → stall)


def test_missing_pane_maps_to_404(monkeypatch):
    def boom(pid):
        raise subprocess.CalledProcessError(returncode=1, cmd=["tmux"])

    monkeypatch.setattr(server.tmux, "capture_live", boom)
    with pytest.raises(server.HTTPException) as e:
        _run(server.live_frame("%1", frame=""))
    assert e.value.status_code == 404


def test_tmux_timeout_maps_to_504(monkeypatch):
    def boom(pid):
        raise subprocess.CalledProcessError(returncode=124, cmd=["tmux"])  # _run's timeout code

    monkeypatch.setattr(server.tmux, "capture_live", boom)
    with pytest.raises(server.HTTPException) as e:
        _run(server.live_frame("%1", frame=""))
    assert e.value.status_code == 504
