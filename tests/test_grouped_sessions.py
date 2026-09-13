"""A pane shared by a tmux session GROUP must produce exactly one entry.

`tmux new-session -t <name>` makes a session that SHARES the group's windows, and
`list-panes -a` reports per session — so one pane arrives once per group member, every
copy carrying the same pane id. That id keys the watcher's buffers and every UI card, so
the copies collide rather than merely repeat (issue: a phone deck of identical cards).
"""

import openbus.tmux as tmux
from openbus.tmux import Pane, dedupe_grouped, find_pane, list_panes

FIELDS = 13  # _PANE_FMT width; Pane(*parts) is positional


def _row(session, pane_id, attached="0", window_index="0"):
    return "\t".join([session, window_index, "claude", "0", pane_id, "node", "t",
                      "/home/x/proj", "111", "1", "1", "", attached])


def _tmux(monkeypatch, rows):
    monkeypatch.setattr(tmux, "_run", lambda args: "\n".join(rows))


def test_grouped_sessions_collapse_to_one_pane(monkeypatch):
    """The regression: two sessions in one group turned every pane into two cards."""
    _tmux(monkeypatch, [_row("gtm-0", "%0", attached="1"), _row("gtm-1", "%0")])
    panes = dedupe_grouped(list_panes())
    assert [p.id for p in panes] == ["%0"]


def test_the_attached_group_member_names_the_pane(monkeypatch):
    """The card should name the session you would actually land in, whichever order
    tmux happens to emit the group in."""
    _tmux(monkeypatch, [_row("gtm-1", "%0"), _row("gtm-0", "%0", attached="1")])
    assert [p.session for p in dedupe_grouped(list_panes())] == ["gtm-0"]


def test_a_multi_client_session_still_counts_as_attached(monkeypatch):
    """#{session_attached} is tmux's client COUNT, not a flag — attach a second terminal
    and it reads "2". Comparing it to "1" filed the pane under the UNATTACHED member."""
    _tmux(monkeypatch, [_row("gtm-1", "%0"), _row("gtm-0", "%0", attached="2")])
    assert [p.session for p in dedupe_grouped(list_panes())] == ["gtm-0"]


def test_the_first_attached_member_wins(monkeypatch):
    """Two attached members: keep the first, don't thrash between them."""
    _tmux(monkeypatch, [_row("gtm-0", "%0", attached="1"), _row("gtm-1", "%0", attached="3")])
    assert [p.session for p in dedupe_grouped(list_panes())] == ["gtm-0"]


def test_an_unattached_group_keeps_the_first_row(monkeypatch):
    """No member attached: pick deterministically rather than arbitrarily."""
    _tmux(monkeypatch, [_row("gtm-1", "%0"), _row("gtm-0", "%0")])
    assert [p.session for p in dedupe_grouped(list_panes())] == ["gtm-1"]


def test_position_survives_a_later_attached_row(monkeypatch):
    """Replacing a row must not move the pane to the end, or the deck reshuffles
    whenever you attach somewhere else."""
    _tmux(monkeypatch, [_row("gtm-1", "%0"), _row("misc", "%9"), _row("gtm-0", "%0", attached="1")])
    assert [p.id for p in dedupe_grouped(list_panes())] == ["%0", "%9"]


def test_distinct_panes_are_untouched(monkeypatch):
    """The ordinary case: no groups, nothing collapsed."""
    _tmux(monkeypatch, [_row("a", "%1"), _row("b", "%2"), _row("c", "%3")])
    assert [p.id for p in dedupe_grouped(list_panes())] == ["%1", "%2", "%3"]


def test_row_width_matches_the_format(monkeypatch):
    """_row must stay in step with _PANE_FMT, or these tests silently skip every line."""
    assert len(_row("s", "%1").split("\t")) == tmux._PANE_FMT.count("\t") + 1 == FIELDS
    assert isinstance(Pane(*_row("s", "%1").split("\t")), Pane)


def test_a_grouped_session_is_still_addressable_by_its_own_name(monkeypatch):
    """The regression in the FIRST cut of this fix: deduping inside list_panes() made
    every non-surviving member invisible to name resolution, so `TMUXRC_TARGET=gtm-1:0`
    silently watched nothing and /api/windows rejected a session that plainly exists.
    A grouped session is a real session with a real name — only the DECK dedupes."""
    _tmux(monkeypatch, [_row("gtm-0", "%0", attached="1"), _row("gtm-1", "%0")])
    assert find_pane("gtm-0:0") is not None
    assert find_pane("gtm-1:0") is not None
    # …and the raw rows keep both names, which is what /api/windows validates against.
    assert {p.session for p in list_panes()} == {"gtm-0", "gtm-1"}


def test_the_watcher_publishes_one_card_per_pane(monkeypatch):
    """The deck is what this whole fix is for, so assert it end-to-end: two grouped
    sessions sharing a pane must reach _publish_states as ONE entry. Without this, the
    watcher could quietly stop deduping and only the helper's own tests would notice."""
    from openbus import watcher as W

    shared = [Pane("gtm-0", "0", "claude", "0", "%0", "node", "t", "/x", "1",
                   "1", "1", "", "1"),
              Pane("gtm-1", "0", "claude", "0", "%0", "node", "t", "/x", "1",
                   "1", "1", "", "0")]
    monkeypatch.setattr(W.tmux, "server_running", lambda: True)
    monkeypatch.setattr(W.tmux, "list_panes", lambda: shared)
    monkeypatch.setattr(W.tmux, "active_pane_id", lambda: "%0")

    w = W.Watcher(None, use_llm=False)
    monkeypatch.setattr(w, "_pane_event", lambda *a, **k: None)
    monkeypatch.setattr(w, "_maybe_bootstrap", lambda panes: None)
    published = []
    monkeypatch.setattr(w, "_publish_states", lambda states: published.append(states))

    w._tick()
    assert [s["pane_id"] for s in published[-1]] == ["%0"]
    assert published[-1][0]["session"] == "gtm-0"  # the attached member names it


def test_a_pane_id_target_resolves_to_the_attached_member(monkeypatch):
    """A pane ID names a PANE, so it must land on the same row the deck shows, even when
    tmux emits the unattached member first — otherwise TMUXRC_TARGET=%0 stamps its single
    card with a session nobody is looking at."""
    _tmux(monkeypatch, [_row("gtm-1", "%0"), _row("gtm-0", "%0", attached="1")])
    assert find_pane("%0").session == "gtm-0"


def test_a_session_qualified_target_still_names_its_own_session(monkeypatch):
    """The other half: an address names a SESSION, so it must keep resolving to that
    session's row — that is what makes a grouped session addressable at all."""
    _tmux(monkeypatch, [_row("gtm-1", "%0"), _row("gtm-0", "%0", attached="1")])
    assert find_pane("gtm-1:0").session == "gtm-1"
    assert find_pane("gtm-0:0").session == "gtm-0"


def test_no_target_picks_the_attached_member(monkeypatch):
    """Defaulting to the first pane should default to the row the deck would show."""
    _tmux(monkeypatch, [_row("gtm-1", "%0"), _row("gtm-0", "%0", attached="1")])
    assert find_pane(None).session == "gtm-0"


def test_a_label_target_also_follows_the_deck(monkeypatch):
    """A window LABEL names a window, not a session, and a group shares windows — so a
    label must resolve to the attached member exactly as a pane id does. Only a
    session-qualified address spells out which session it means."""
    rows = ["\t".join([s, "0", "Resolve PR 38", "0", "%0", "node", "t", "/x", "1",
                       "1", "1", "", a]) for s, a in (("gtm-1", "0"), ("gtm-0", "1"))]
    _tmux(monkeypatch, rows)
    assert find_pane("Resolve PR 38").session == "gtm-0"
    assert find_pane("Resolve PR 38.0").session == "gtm-0"
    # …while the spelled-out address still names its own session.
    assert find_pane("gtm-1:0").session == "gtm-1"
