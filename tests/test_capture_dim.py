"""Faint/gray color is the ONLY on-screen signal separating an agent TUI's
de-emphasized text (unsent input-box drafts, ghost suggestions, borders, chrome) from
real output. _mark_dim re-encodes that signal as ⟪dim⟫…⟪/dim⟫ before _ANSI strips the
colors, so the parser LLM can still see what was visually muted; strip_dim collapses a
marked capture back to the plain text the phone renders. SGR patterns pinned here were
observed in live Claude Code panes."""

from daemon.tmux import (
    _ANSI,
    _mark_dim,
    _mark_placeholder,
    _materialize_links,
    strip_dim,
)


def _capture(raw: str, mark_dim: bool = True) -> str:
    """The capture_pane pipeline minus tmux: links, optional markers, strip."""
    out = _materialize_links(raw)
    if mark_dim:
        out = _mark_placeholder(_mark_dim(out))
    return _ANSI.sub("", out)


def test_faint_ghost_suggestion_marked():
    # Faint (SGR 2) input-box suggestion right after ❯ is the agent's placeholder — the
    # dim run is promoted to ⟪placeholder⟫ (a suggestion offered, not typed).
    raw = "❯ \x1b[2mdrop the copy-link-3891 tag too\x1b[0m"
    assert _capture(raw) == "❯ ⟪placeholder⟫drop the copy-link-3891 tag too⟪/placeholder⟫"


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
    # Sent-message echo: gray-on-gray ❯ then near-white text on bg 237. Extended-color
    # args must never parse as style codes — bg 48;5;2 carries a literal 2 (faint).
    raw = (
        "\x1b[38;5;239m\x1b[48;5;237m❯ \x1b[38;5;231msent message\x1b[0m"
        " \x1b[48;5;2mok\x1b[0m"
    )
    assert _capture(raw) == "⟪dim⟫❯ ⟪/dim⟫sent message ok"


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


def test_placeholder_after_prompt_glyph_marked_distinctly():
    # Claude Code (❯) and Codex (›) both show greyed suggestion text in an EMPTY input
    # box; it is SGR-2 faint, so _mark_dim wraps it — then _mark_placeholder promotes the
    # run right after the prompt glyph to ⟪placeholder⟫ so no LLM path reads the tool's
    # offered suggestion as a real, pending instruction. Claude uses a non-breaking space.
    claude = "\x1b[39m❯\xa0\x1b[2mdraft the doc-review package\x1b[0m"
    assert _capture(claude) == "❯\xa0⟪placeholder⟫draft the doc-review package⟪/placeholder⟫"
    codex = "\x1b[1m›\x1b[0m\x1b[48;2;55;55;57m \x1b[2mWrite tests for @filename\x1b[0m"
    assert _capture(codex) == "› ⟪placeholder⟫Write tests for @filename⟪/placeholder⟫"


def test_real_typed_draft_after_prompt_is_not_placeholder():
    # Text the user actually typed renders near-white (231), not faint — it is NOT a
    # placeholder and stays unmarked, so the model can see the real pending draft.
    raw = "❯ \x1b[38;5;231mmerge 75 and 63\x1b[0m"
    assert _capture(raw) == "❯ merge 75 and 63"


def test_dim_not_after_prompt_stays_dim():
    # A faint run that isn't the input-box suggestion (chrome, status) stays ⟪dim⟫.
    raw = "\x1b[38;5;246mCrunched for 2m 3s\x1b[39m"
    assert _capture(raw) == "⟪dim⟫Crunched for 2m 3s⟪/dim⟫"


def test_strip_dim_clears_placeholder_markers():
    # Chrome line (stays ⟪dim⟫) above an input-box placeholder line (⟪placeholder⟫).
    raw = "\x1b[38;5;246mCrunched 2m\x1b[39m\n❯\xa0\x1b[2mtry this\x1b[0m"
    marked = _capture(raw)
    assert "⟪placeholder⟫" in marked and "⟪dim⟫" in marked
    assert strip_dim(marked) == _capture(raw, mark_dim=False)


def test_tail_marked_preserves_placeholder_markers():
    # tail_marked must keep ⟪placeholder⟫ tokens whole too, not just ⟪dim⟫ — a long
    # placeholder line char-sliced to the budget must not emit a token fragment or an
    # orphaned close, and a tail opening inside the run re-opens the matching open.
    from daemon.tmux import (
        PLACEHOLDER_CLOSE,
        PLACEHOLDER_OPEN,
        tail_marked,
    )

    line = "a" * 30 + PLACEHOLDER_OPEN + "b" * 40 + PLACEHOLDER_CLOSE + "c" * 30
    out = tail_marked(line, 20)
    assert out  # never empty
    assert out.count(PLACEHOLDER_OPEN) >= out.count(PLACEHOLDER_CLOSE)  # no orphan close
    stripped = out.replace(PLACEHOLDER_OPEN, "").replace(PLACEHOLDER_CLOSE, "")
    assert "⟪" not in stripped and "⟫" not in stripped  # no split-token fragment


def test_tail_marked_never_splits_or_orphans_markers():
    from daemon.tmux import DIM_CLOSE, DIM_OPEN, tail_marked

    # Under budget: unchanged.
    assert tail_marked(f"a{DIM_OPEN}b{DIM_CLOSE}", 999) == f"a{DIM_OPEN}b{DIM_CLOSE}"

    # Cut lands inside a dim run that spans a line boundary: the kept tail re-opens the
    # run, so a close is never orphaned and markers stay balanced.
    text = "x" * 50 + "\n" + DIM_OPEN + "draft\n text" + DIM_CLOSE + "\ny" * 3
    out = tail_marked(text, 20)
    assert out.count(DIM_OPEN) >= out.count(DIM_CLOSE)  # no orphaned close
    assert "⟫" not in out.split(DIM_OPEN)[0] if DIM_OPEN in out else True  # no split token
    # Whatever survives contains only WHOLE marker tokens (no ⟪…/⟫… fragments).
    stripped = out.replace(DIM_OPEN, "").replace(DIM_CLOSE, "")
    assert "⟪" not in stripped and "⟫" not in stripped

    # A single line LONGER than the budget must not truncate to empty — keep its tail,
    # still marker-clean (no split-token fragment, no orphaned close).
    long_line = "a" * 30 + DIM_OPEN + "b" * 40 + DIM_CLOSE + "c" * 30
    out3 = tail_marked(long_line, 20)
    assert out3  # not empty
    assert out3.count(DIM_OPEN) >= out3.count(DIM_CLOSE)
    s3 = out3.replace(DIM_OPEN, "").replace(DIM_CLOSE, "")
    assert "⟪" not in s3 and "⟫" not in s3
