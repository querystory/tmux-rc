"""A panel an agent TUI draws beside its conversation reaches the capture appended to every
row. split_side_panel moves it into its own block, so the conversation reads as drawn; boxes
and tables, whose bars also line up, stay whole."""

from openbus.classify import _visible, screen_offers
from openbus.tmux import (
    DIM_CLOSE,
    DIM_OPEN,
    SIDE_PANEL,
    VISIBLE_SCREEN,
    split_side_panel,
    strip_dim,
)

W = 40  # the conversation column's width; the panel's bar sits in the next cell


def _row(left: str, right: str, *, wide: int = 0) -> str:
    # A wide glyph takes two cells, so it shortens the padding by one each.
    return f"{left.ljust(W - wide)}{DIM_OPEN}│{right}{DIM_CLOSE}"


SCREEN = [
    _row("● Moved the parser into its own module.", "Changes"),
    _row("", "notes/ (untracked)"),
    _row("  Tests pass 🎉 on the new layout.", "│──────────────", wide=1),
    _row("", "New file not yet tracked."),
    _row("  Run this: ! make migrate", "↓ 2 more below"),
    _row("", ""),
    _row("✻ Worked for 12s", ""),
    "─" * 60,
    "❯ ",
    "─" * 60,
]


def _capture(rows: list[str]) -> str:
    return "earlier output\n" + VISIBLE_SCREEN + "\n" + "\n".join(rows)


def test_panel_moves_into_its_own_block():
    text = split_side_panel(_capture(SCREEN))
    visible = _visible(text)
    assert "untracked" not in visible
    assert "│" not in visible
    assert "  Tests pass 🎉 on the new layout.\n" in visible
    assert "  Run this: ! make migrate\n" in visible  # trailing padding trimmed
    head = text.rpartition(VISIBLE_SCREEN)[0]
    assert head.startswith("earlier output\n" + SIDE_PANEL + "\nChanges\nnotes/ (untracked)\n")
    assert text.count(DIM_OPEN) == text.count(DIM_CLOSE)  # runs stay paired
    assert strip_dim(text) == "earlier output\n" + visible.removeprefix("\n")


def test_offer_reads_the_conversation_column():
    reply = ("Moved the parser into its own module.\n\nTests pass 🎉 on the new layout.\n"
             "\nRun this: ! make migrate")
    assert screen_offers(_capture(SCREEN), "claude", reply) == []  # the bug: panel in every row
    assert screen_offers(split_side_panel(_capture(SCREEN)), "claude", reply) == ["! make migrate"]


def test_box_dialog_stays_whole():
    rows = [f"│ {f'option {i}'.ljust(29)}│" for i in range(6)]
    box = ["╭" + "─" * 30 + "╮", *rows, "╰" + "─" * 30 + "╯"]
    text = _capture(box)
    assert split_side_panel(text) == text


def test_table_stays_whole():
    table = [f"  │ {f'row {i}'.ljust(10)}│ {f'value {i}'.ljust(10)}│" for i in range(8)]
    text = _capture(table)
    assert split_side_panel(text) == text


def test_bar_on_a_few_rows_stays_whole():
    rows = [_row("  checking the build", "log") for _ in range(2)] + [f"line {i}" for i in range(6)]
    text = _capture(rows)
    assert split_side_panel(text) == text
