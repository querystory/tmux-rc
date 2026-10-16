"""A panel an agent TUI draws beside its conversation reaches the capture appended to every
row. split_side_panel moves it into its own block, so the conversation reads as drawn; boxes,
tables and gutters, whose bars also line up, stay whole. Rows here are raw (styles and OSC 8
links as tmux -e prints them) and go through capture_pane's own steps."""

import pytest

from openbus.classify import _visible, screen_offers
from openbus.tmux import (
    _ANSI,
    DIM_CLOSE,
    DIM_OPEN,
    SIDE_PANEL,
    VISIBLE_SCREEN,
    _mark_dim,
    _mark_placeholder,
    _materialize_links,
    split_side_panel,
    strip_dim,
)

W = 40  # the conversation column's width; the panel's bar sits in the next cell


def _row(left: str, right: str, *, cells: int | None = None) -> str:
    # `cells`: the screen width of `left` where it differs from its length (a wide glyph,
    # an escape sequence).
    pad = " " * (W - (len(left) if cells is None else cells))
    return f"{left}{pad}\x1b[2m│\x1b[0m\x1b[38;5;246m{right}\x1b[39m"


def _link(label: str, url: str) -> str:
    return f"\x1b]8;;{url}\x1b\\{label}\x1b]8;;\x1b\\"


SCREEN = [
    _row("● Moved the parser into its own module.", "Changes"),
    _row("", "notes/ (untracked)"),
    _row("  Tests pass 🎉 on the new layout.", "│──────────────", cells=34),
    _row("", "New file not yet tracked."),
    _row("  Run this: ! make migrate", "↓ 2 more below"),
    _row("", ""),
    _row("✻ Worked for 12s", ""),
    "─" * 60,
    "❯ ",
    "─" * 60,
]


def _capture(rows: list[str], split=split_side_panel) -> str:
    raw = "earlier output\n" + VISIBLE_SCREEN + "\n" + "\n".join(rows)
    return _ANSI.sub("", _mark_placeholder(_mark_dim(_materialize_links(split(raw)))))


def _unsplit(rows: list[str]) -> str:
    return _capture(rows, split=lambda raw: raw)


def test_panel_moves_into_its_own_block():
    text = _capture(SCREEN)
    visible = _visible(text)
    assert "untracked" not in visible
    assert "│" not in visible
    assert "\n  Tests pass 🎉 on the new layout.\n" in visible
    assert "\n  Run this: ! make migrate\n" in visible  # trailing padding trimmed
    head = text.rpartition(VISIBLE_SCREEN)[0]
    assert head.startswith("earlier output\n" + SIDE_PANEL + "\nChanges\nnotes/ (untracked)\n")
    assert text.count(DIM_OPEN) == text.count(DIM_CLOSE)
    assert strip_dim(text) == "earlier output\n" + visible.removeprefix("\n")


def test_offer_reads_the_conversation_column():
    reply = ("Moved the parser into its own module.\n\nTests pass 🎉 on the new layout.\n"
             "\nRun this: ! make migrate")
    assert screen_offers(_unsplit(SCREEN), "claude", reply) == []  # the bug: panel in every row
    assert screen_offers(_capture(SCREEN), "claude", reply) == ["! make migrate"]


@pytest.mark.parametrize("rows", [
    [f"│ {f'option {i}'.ljust(29)}│" for i in range(6)],  # a box dialog
    [f"  │ {f'row {i}'.ljust(10)}│ {f'value {i}'.ljust(10)}│" for i in range(8)],  # a table
    [f"  {i:>3}│value = {i} + offset  # some code that runs on" for i in range(8)],  # a gutter
    [_row("  checking the build", "log")] * 2 + [f"line {i}" for i in range(6)],  # a few rows
    [_row("build completed", " 3 targets"), "", ""],  # a sparse screen
], ids=["box", "table", "gutter", "few-rows", "sparse"])
def test_lookalikes_stay_whole(rows):
    assert _capture(rows) == _unsplit(rows)


@pytest.mark.parametrize(("left", "cells"), [
    (_link("guide(draft).md", "https://example.com/docs/guide(draft).md"), 15),
    (_link("src/[id].py", "https://example.com/src/%5Bid%5D.py"), 11),
    (f"\x1b[2m{_link('docs', 'https://example.com/docs')}\x1b[0m", 4),
    ("See [docs](https://example.com/docs)", None),  # printed as is, every cell its own
])
def test_link_rows_split_on_their_screen_column(left, cells):
    rows = list(SCREEN)
    rows[1] = _row(left, "notes/", cells=cells)
    assert "notes/" not in _visible(_capture(rows))


def test_panel_link_after_a_literal_bracket():
    rows = list(SCREEN)
    rows[1] = _row("Updated items[0].", _link("notes", "https://example.com/notes"))
    visible = _visible(_capture(rows))
    assert "\nUpdated items[0].\n" in visible
    assert "notes" not in visible


def test_a_joined_row_does_not_stand_for_the_pane():
    rows = list(SCREEN)
    rows[4] = "x" * 100  # a wrapped row -J joined, wider than the pane
    assert SIDE_PANEL in _capture(rows)
