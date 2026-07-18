"""Live-view telemetry: emit_live attr shape + fail-closed behavior, and the watcher's
has-live-viewer presence flag (docs/design/live-telemetry.md). Telemetry must never send
frame text and must be a no-op when disabled; presence must age out on its own."""

import time

from daemon import telemetry
from daemon.watcher import Watcher


def _capture(monkeypatch):
    """Capture what emit_live hands to _emit_record, without touching the OTel SDK."""
    seen = {}
    monkeypatch.setattr(
        telemetry, "_emit_record",
        lambda body, attrs, scope=telemetry._SCOPE: seen.update(body=body, attrs=attrs, scope=scope),
    )
    return seen


def test_change_round_carries_bytes_and_own_scope(monkeypatch):
    seen = _capture(monkeypatch)
    telemetry.emit_live(
        session="s1", pane_uid="b:1:%0", pane_label="work", tool="claude",
        hold_s=0.512, changed=True, raw_bytes=13000, actor="a@b.com",
    )
    assert seen["scope"] == telemetry._LIVE_SCOPE  # NOT the parse scope
    a = seen["attrs"]
    assert a["session"] == "s1" and a["pane_uid"] == "b:1:%0"
    assert a["changed"] is True and a["raw_bytes"] == 13000
    assert a["hold_s"] == 0.512 and a["tool"] == "claude" and a["actor"] == "a@b.com"
    assert "text" not in a and "pane_text" not in a  # never the frame content


def test_session_is_capped(monkeypatch):
    # session is a client query param — must be length-capped so it can't bloat payloads.
    seen = _capture(monkeypatch)
    telemetry.emit_live(
        session="x" * 500, pane_uid="u", pane_label="l", tool=None,
        hold_s=1.0, changed=False, raw_bytes=None,
    )
    assert len(seen["attrs"]["session"]) == 64


def test_absent_session_is_unattributable(monkeypatch):
    # A missing session must NOT collapse into a shared placeholder (that would mis-sum
    # unrelated viewers' watch-time). The key is simply absent ⇒ rollups exclude it.
    for missing in (None, ""):
        seen = _capture(monkeypatch)
        telemetry.emit_live(
            session=missing, pane_uid="u", pane_label="l", tool=None,
            hold_s=1.0, changed=False, raw_bytes=None,
        )
        assert "session" not in seen["attrs"]


def test_idle_round_omits_bytes(monkeypatch):
    seen = _capture(monkeypatch)
    telemetry.emit_live(
        session="s1", pane_uid="b:1:%0", pane_label="work", tool=None,
        hold_s=25.0, changed=False, raw_bytes=None,
    )
    a = seen["attrs"]
    # No frame was sent on an idle-timeout round, so byte sums stay honest.
    assert a["changed"] is False and "raw_bytes" not in a
    assert "tool" not in a and "actor" not in a  # absent optionals are dropped


def test_noop_when_disabled(monkeypatch):
    # No OTLP endpoint ⇒ _logger() is None ⇒ emit is a no-op that raises nothing.
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    telemetry._provider.cache_clear()
    telemetry._logger.cache_clear()
    telemetry.emit_live(
        session="s", pane_uid="u", pane_label="l", tool=None,
        hold_s=1.0, changed=True, raw_bytes=1,
    )  # must not raise


def test_has_live_viewer_respects_window():
    w = Watcher(target=None, use_llm=False)
    assert w.has_live_viewer("%0") is False  # never polled
    w.note_live_poll("%0")
    assert w.has_live_viewer("%0") is True
    # Age the stamp past the window: no longer counted as watched (leak-proof, no cleanup).
    w._live_seen["%0"] = time.monotonic() - (w.LIVE_PRESENCE_WINDOW + 1)
    assert w.has_live_viewer("%0") is False
