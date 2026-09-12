"""A pane shared by a tmux session GROUP must produce exactly one entry.

`tmux new-session -t <name>` makes a session that SHARES the group's windows, and
`list-panes -a` reports per session — so one pane arrives once per group member, every
copy carrying the same pane id. That id keys the watcher's buffers and every UI card, so
the copies collide rather than merely repeat (issue: a phone deck of identical cards).
"""

import openbus.tmux as tmux
from openbus.tmux import Pane, list_panes

FIELDS = 13  # _PANE_FMT width; Pane(*parts) is positional


def _row(session, pane_id, attached="0", window_index="0"):
    return "\t".join([session, window_index, "claude", "0", pane_id, "node", "t",
                      "/home/x/proj", "111", "1", "1", "", attached])


def _tmux(monkeypatch, rows):
    monkeypatch.setattr(tmux, "_run", lambda args: "\n".join(rows))


def test_grouped_sessions_collapse_to_one_pane(monkeypatch):
    """The regression: two sessions in one group turned every pane into two cards."""
    _tmux(monkeypatch, [_row("gtm-0", "%0", attached="1"), _row("gtm-1", "%0")])
    panes = list_panes()
    assert [p.id for p in panes] == ["%0"]


def test_the_attached_group_member_names_the_pane(monkeypatch):
    """The card should name the session you would actually land in, whichever order
    tmux happens to emit the group in."""
    _tmux(monkeypatch, [_row("gtm-1", "%0"), _row("gtm-0", "%0", attached="1")])
    assert [p.session for p in list_panes()] == ["gtm-0"]


def test_an_unattached_group_keeps_the_first_row(monkeypatch):
    """No member attached: pick deterministically rather than arbitrarily."""
    _tmux(monkeypatch, [_row("gtm-1", "%0"), _row("gtm-0", "%0")])
    assert [p.session for p in list_panes()] == ["gtm-1"]


def test_position_survives_a_later_attached_row(monkeypatch):
    """Replacing a row must not move the pane to the end, or the deck reshuffles
    whenever you attach somewhere else."""
    _tmux(monkeypatch, [_row("gtm-1", "%0"), _row("misc", "%9"), _row("gtm-0", "%0", attached="1")])
    assert [p.id for p in list_panes()] == ["%0", "%9"]


def test_distinct_panes_are_untouched(monkeypatch):
    """The ordinary case: no groups, nothing collapsed."""
    _tmux(monkeypatch, [_row("a", "%1"), _row("b", "%2"), _row("c", "%3")])
    assert [p.id for p in list_panes()] == ["%1", "%2", "%3"]


def test_row_width_matches_the_format(monkeypatch):
    """_row must stay in step with _PANE_FMT, or these tests silently skip every line."""
    assert len(_row("s", "%1").split("\t")) == tmux._PANE_FMT.count("\t") + 1 == FIELDS
    assert isinstance(Pane(*_row("s", "%1").split("\t")), Pane)
