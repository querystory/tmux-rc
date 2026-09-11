"""The change check must ignore Codex's ambient "sparkle" animation.

Codex scatters single-dot braille over the rows around its input box and reshuffles
them every frame. That is decoration, not activity — but it made every tick look like a
changed screen, so an idle pane re-fired the classifier every 1.5s (and each call may
take 20s, starving the tick that drives the stalled flag).

Pins the property that matters: frames that differ ONLY by the animation share one
fingerprint, while a real content change still produces a different one."""

from daemon.watcher import _fingerprint

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


def test_screens_without_the_animation_keep_their_own_shape():
    """No sparkle anywhere ⇒ the fast path: the volatile list still runs (it trims the
    trailing space after the prompt), but no line is flattened, so indentation survives
    verbatim."""
    plain = "$ make test\n   297 passed\n$ "
    assert _fingerprint(plain) == "$ make test\n   297 passed\n$"
