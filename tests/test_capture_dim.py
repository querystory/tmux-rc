"""Faint/gray color is the ONLY on-screen signal separating an agent TUI's
de-emphasized text (unsent input-box drafts, ghost suggestions, borders, chrome) from
real output. _mark_dim re-encodes that signal as ⟪dim⟫…⟪/dim⟫ before _ANSI strips the
colors, so the parser LLM can still see what was visually muted; strip_dim collapses a
marked capture back to the plain text the phone renders. SGR patterns pinned here were
observed in live Claude Code panes."""

from daemon.tmux import _ANSI, _mark_dim, _materialize_links, strip_dim


def _capture(raw: str, mark_dim: bool = True) -> str:
    """The capture_pane pipeline minus tmux: links, optional markers, strip."""
    out = _materialize_links(raw)
    if mark_dim:
        out = _mark_dim(out)
    return _ANSI.sub("", out)


def test_faint_ghost_suggestion_marked():
    # Input-box history/autocomplete ghost text renders as SGR 2 (faint).
    raw = "❯ \x1b[2mdrop the copy-link-3891 tag too\x1b[0m"
    assert _capture(raw) == "❯ ⟪dim⟫drop the copy-link-3891 tag too⟪/dim⟫"


def test_gray_256_foreground_marked():
    # Chrome/status text renders in the xterm grayscale ramp (38;5;239/244/246 live).
    raw = "\x1b[38;5;246mCrunched for 2m 3s\x1b[39m done"
    assert _capture(raw) == "⟪dim⟫Crunched for 2m 3s⟪/dim⟫ done"


def test_content_colors_untouched():
    # Near-white 231 (response text), light blue 153 (code refs), basic green: content.
    raw = "\x1b[38;5;231mDone\x1b[39m \x1b[38;5;153mapp.js\x1b[39m \x1b[32m+768\x1b[0m"
    assert _capture(raw) == "Done app.js +768"


def test_plain_text_untouched():
    assert _capture("just words, no escapes") == "just words, no escapes"


def test_adjacent_dim_runs_merge():
    # Status lines alternate faint/gray every word; no empty marker pairs between.
    raw = "\x1b[2mon\x1b[0m\x1b[38;5;246m main\x1b[0m ok"
    assert _capture(raw) == "⟪dim⟫on main⟪/dim⟫ ok"


def test_sgr_22_and_39_end_dim():
    raw = "\x1b[2mfaint\x1b[22m normal \x1b[38;5;244mgray\x1b[39m normal"
    assert _capture(raw) == "⟪dim⟫faint⟪/dim⟫ normal ⟪dim⟫gray⟪/dim⟫ normal"


def test_extended_bg_args_not_misread():
    # Sent-message echo: gray-on-gray ❯ then near-white text on bg 237. The bg args
    # (48;5;2 here) must not be parsed as style codes (2 = faint).
    raw = "\x1b[38;5;239m\x1b[48;5;237m❯ \x1b[38;5;231msent message\x1b[0m"
    assert _capture(raw) == "⟪dim⟫❯ ⟪/dim⟫sent message"


def test_truecolor_fg_consumed_not_gray():
    raw = "\x1b[38;2;100;100;100mrgb text\x1b[0m plain"
    assert _capture(raw) == "rgb text plain"


def test_truecolor_fg_clears_gray_state():
    # Any code-38 sequence REPLACES the foreground, so gray set by 38;5;24x must
    # clear on a following 38;2;R;G;B — while 48/58 (bg/underline color) leave it.
    raw = "\x1b[38;5;246mgray\x1b[38;2;200;50;50mred\x1b[0m end"
    assert _capture(raw) == "⟪dim⟫gray⟪/dim⟫red end"
    raw = "\x1b[38;5;246mgray\x1b[58;5;100mstill gray\x1b[0m end"
    assert _capture(raw) == "⟪dim⟫graystill gray⟪/dim⟫ end"


def test_input_box_between_dim_borders():
    # The composed shape the parser prompt keys on: dim border rules around the box.
    raw = (
        "\x1b[38;5;244m────────\x1b[39m\n"
        "❯ ok now do the tunnel fix\n"
        "\x1b[38;5;244m────────\x1b[39m"
    )
    assert _capture(raw) == (
        "⟪dim⟫────────⟪/dim⟫\n❯ ok now do the tunnel fix\n⟪dim⟫────────⟪/dim⟫"
    )


def test_osc8_links_still_materialize_inside_dim():
    raw = "\x1b[38;5;246m\x1b]8;;https://x.io\x1b\\docs\x1b]8;;\x1b\\ hint\x1b[39m"
    assert _capture(raw) == "⟪dim⟫[docs](https://x.io) hint⟪/dim⟫"


def test_final_close_lands_before_trailing_whitespace():
    # Found on live data: a close marker AFTER trailing newlines would shield them
    # from capture_pane's trailing-\n rstrip and break the strip_dim roundtrip.
    raw = "\x1b[2mhint\x1b[0m   \n\n"
    assert _capture(raw) == "⟪dim⟫hint⟪/dim⟫   \n\n"


def test_strip_dim_roundtrips_to_plain_capture():
    raw = "\x1b[2mghost\x1b[0m ❯ \x1b[38;5;246mchrome\x1b[39m \x1b[32mreal\x1b[0m"
    assert strip_dim(_capture(raw)) == _capture(raw, mark_dim=False)
