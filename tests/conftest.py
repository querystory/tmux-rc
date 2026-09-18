import pytest

from openbus import tmux


@pytest.fixture(autouse=True)
def _no_enter_settle(monkeypatch):
    """send_keys waits before the Return so a TUI doesn't read it as a newline
    (tmux._ENTER_SETTLE_S). Real time in every send would tax the whole suite for a
    delay that is about a terminal's paste heuristic, not about our logic — so it is
    zero by default here. test_send_enter_settle.py opts back in."""
    monkeypatch.setattr(tmux, "_ENTER_SETTLE_S", 0)
