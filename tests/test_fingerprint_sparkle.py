"""The change check must ignore Codex's ambient "sparkle" animation.

Codex scatters single-dot braille over the rows around its input box and reshuffles
them every frame. That is decoration, not activity — but it made every tick look like a
changed screen, so an idle pane re-fired the classifier every 1.5s (and each call may
take 20s, starving the tick that drives the stalled flag).

Pins the property that matters: frames that differ ONLY by the animation share one
fingerprint, while a real content change still produces a different one."""

from openbus.watcher import _fingerprint

# Real frames, reduced: the input box, its prompt line, and the status bar beneath.
_FRAME_A = (
    "• Working (1h 05m 15s • esc to interrupt)\n"
    "⠁             ⠄          ⢀  ⠈   ⠁ ⢀                ⠐\n"
    "› Ask Codex to do anything              ⠁         ⠈\n"
    "        ⠐                                  ⠈      ⠂\n"
    "  fix-async-mess · gpt-6-astra max · Context 43% left · 4.36M used"
)
# Same screen one frame later: every dot has moved, two have landed against the text,
# and the drifting context/usage counters have ticked.
_FRAME_B = (
    "• Working (1h 05m 18s • esc to interrupt)\n"
    "    ⠈                    ⢀        ⢀     ⠁        ⠐\n"
    "›⠁Ask Codex to do anything              ⠁    ⠄    ⠈\n"
    "       ⢀⠐        ⠄        ⠄         ⠠        ⢀\n"
    "  fix-async-mess · gpt-6-astra max · Context 42% left · 4.38M used"
)


def test_sparkle_frames_share_one_fingerprint():
    """The whole point: decoration must not read as activity."""
    assert _fingerprint(_FRAME_A) == _fingerprint(_FRAME_B)


def test_a_line_the_animation_drifts_off_still_matches():
    """The subtle half. The dots drift BETWEEN lines, so a line can hold one on this
    frame and none on the next; normalizing only the lines that currently hold a dot
    would let that line flip the signature by itself."""
    bare = _FRAME_A.replace(
        "› Ask Codex to do anything              ⠁         ⠈",
        "› Ask Codex to do anything",
    )
    assert _fingerprint(bare) == _fingerprint(_FRAME_A)


def test_dots_leaving_both_band_edges_still_matches():
    """The band must not breathe. Derived from where the dots ARE, it grew and shrank
    between frames, so an edge row was flattened in one fingerprint and left alone in
    the next — re-creating the churn. Anchored to the input line it stays put."""
    empty_edges = (
        "• Working (1h 05m 15s • esc to interrupt)\n"
        "   \n"                                        # top edge: no dot this frame
        "› Ask Codex to do anything        ⠁\n"
        "   \n"                                        # bottom edge: no dot either
        "  fix-async-mess · gpt-6-astra max · Context 43% left · 4.36M used"
    )
    assert _fingerprint(empty_edges) == _fingerprint(_FRAME_A)


def test_dots_leaving_the_outer_rows_still_matches():
    """Review case (antonyliang): the extreme dots move inward, vacating the input box's
    BORDER rows. Those carry real spacing, so if the band were derived from where the
    dots are they would be flattened in one frame and kept verbatim in the next — the
    worst place for it. The band is a fixed offset from the input line, so it holds."""
    outer = (
        "• Working\n"
        "  ⠁    border   row\n"
        "     ⠄\n"
        "› Ask Codex to do anything\n"
        "     ⠂\n"
        "  ⠈    border   row\n"
        "  status bar\n"
    )
    inner = (
        "• Working\n"
        "       border   row\n"
        "     ⠄  ⠁\n"
        "› Ask Codex to do anything\n"
        "     ⠂ ⠈\n"
        "       border   row\n"
        "  status bar\n"
    )
    assert _fingerprint(outer) == _fingerprint(inner)


def test_single_cell_braille_outside_the_band_is_content():
    """U+2801 is the braille letter "a". Outside Codex's input band a lone dot is text,
    not decoration, so a change to it is a real change and must be seen."""
    a = "output: ⠁\ndone"
    b = "output: ⠂\ndone"
    assert _fingerprint(a) != _fingerprint(b)


def test_a_stray_dot_in_scrollback_flattens_nothing():
    """A lone dot far from the input box must not drag unrelated rows into the band and
    erase their indentation."""
    a = "  ⠁ log line\n    indented   detail\nplain"
    assert _fingerprint(a) == a


def test_no_codex_input_line_means_no_band():
    """A Claude Code or shell pane has no "›" prompt: nothing is normalized at all."""
    plain = "$ make test\n   297 passed\n$"
    assert _fingerprint(plain) == plain


def test_real_change_under_the_animation_still_registers():
    """The guard against over-stripping: a sparkled screen is not a blind spot."""
    changed = _FRAME_B.replace("• Working", "• Ran pytest")
    assert _fingerprint(changed) != _fingerprint(_FRAME_B)


def test_indentation_outside_the_animation_is_preserved():
    """Only the animated band is flattened. Structured output above it — a diff, a tree,
    nested logs — is distinguished BY its indentation, so two screens differing only in
    leading space must keep different fingerprints."""
    a = "    nested one\n  parent\n" + _FRAME_A
    b = "  nested one\n      parent\n" + _FRAME_A
    assert _fingerprint(a) != _fingerprint(b)


def test_multi_dot_spinners_and_braille_text_are_untouched():
    """Identified by construction — ONE raised dot. A real spinner (7 dots) is handled
    by the volatile list above, and dense braille is content; neither may be eaten by
    the sparkle rule, which would hide a genuinely changing screen."""
    a = "⣾ building\nplain line"
    b = "⣷ building\nplain line"
    # differs only by the spinner glyph the volatile list already strips
    assert _fingerprint(a) == _fingerprint(b)
    # dense braille is real content and must still differ
    assert _fingerprint("⣿⣿⠿⣿ banner") != _fingerprint("⣿⡿⠿⣿ banner")


def test_volatile_list_still_runs_without_a_band():
    """No Codex input line ⇒ no flattening, but the volatile list still trims the
    trailing space after the prompt."""
    plain = "$ make test\n   297 passed\n$ "
    assert _fingerprint(plain) == "$ make test\n   297 passed\n$"
