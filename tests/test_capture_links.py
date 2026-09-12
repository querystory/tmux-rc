"""OSC 8 hyperlinks carry their URL in an escape sequence terminals never display —
capture materializes them as markdown ('[label](url)') before stripping the rest of
the escapes: the LLM parser reads markdown natively and the web linkifier renders it
as a label-only anchor."""

from openbus.tmux import _ANSI, _materialize_links


def test_osc8_link_materializes_as_markdown():
    raw = "see \x1b]8;;https://x.io/pr/7\x1b\\PR #7\x1b]8;;\x1b\\ done"
    assert _materialize_links(raw) == "see [PR #7](https://x.io/pr/7) done"


def test_osc8_label_equal_to_url_not_duplicated():
    raw = "\x1b]8;;https://x.io\x07https://x.io\x1b]8;;\x07"
    assert _materialize_links(raw) == "https://x.io"


def test_osc8_label_containing_url_keeps_href():
    raw = "\x1b]8;;https://x.io\x07see https://x.io\x1b]8;;\x07"
    assert _materialize_links(raw) == "[see https://x.io](https://x.io)"


def test_ansi_strip_removes_colors_and_osc():
    raw = "\x1b[1;32mgreen\x1b[0m \x1b]0;title\x07plain"
    assert _ANSI.sub("", raw) == "green plain"
