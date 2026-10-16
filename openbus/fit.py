"""Fit a window's width to the phone viewing it (docs/design/fit-width.md).

The phone renders capture-pane output, and the apps in it lay themselves out at their
window's width. When the phone opens a pane that fills its window (its only pane, or
zoomed) it reports its column count, and the window takes that width; its height is left
alone. A fitted window carries the width in @tmuxrc-fit, with "!" when the viewer forced
it past an attached terminal, and keeps it after the viewer leaves. It is given back only
when a terminal attaches to an unforced fit, or the viewer unticks "Fit to my screen"."""

import re
import subprocess
import threading
import time
from contextlib import suppress

from . import tmux

MIN_COLS = 40  # narrower and most TUIs stop laying out sensibly
MARK = "@tmuxrc-fit"
REDRAW_SECONDS = 1.0  # how long an app takes to redraw: looks before then may see it half done
_SIZE = "#{window_width}x#{window_height}"
# The pane's window: does the pane fill it, its sessions and every attached session (a
# window can be in several, and a terminal on any of them sees it shrink), its width and
# size, our mark, and its panes (all of them redraw when it is resized).
_FMT = "\t".join(["#{window_panes}", "#{window_zoomed_flag}", "#{pane_active}",
                  "#{window_linked_sessions_list}",
                  "#{S:#{?session_attached,#{session_name}#,,}}", "#{window_width}", _SIZE,
                  f"#{{{MARK}}}", "#{P:#{pane_id} }"])

_REFLOWED = re.compile(r"[\s\u2500-\u259f]+")  # whitespace, box drawing, block elements
_lock = threading.Lock()  # one read-decide-resize at a time
_resized: dict[str, float] = {}  # pane -> when we began the last resize of its window


def _run(args: list[str]) -> str:
    return tmux._run(args)  # noqa: SLF001 - via the module, so a swapped-out tmux (demo, tests) holds


def just_resized(pane_id: str, looked: float) -> str:
    """Where a look (its capture finished at `looked`) stands against our last resize of
    the pane's window: "redraw" while the app may still be redrawing, so the screen can be
    half done; "settled" for the first look after that, which spends the resize; "" with
    none pending, or one begun after the capture finished (it cannot be in it). Under the
    lock, so a look taken while a resize lands waits for its token."""
    with _lock:
        at = _resized.get(pane_id)
        if at is None or looked < at:
            return ""
        if looked - at < REDRAW_SECONDS:
            return "redraw"
        del _resized[pane_id]
        return "settled"


def reflow_key(text: str) -> str:
    """A screen's content regardless of the width it was laid out at: what a reflow moves
    around (whitespace) or redraws to a new length (box-drawing rules) is left out."""
    return _REFLOWED.sub("", tmux.strip_dim(text))


def view(pane_id: str, cols: int, force: bool = False) -> bool:
    """The phone shows `cols` columns of the pane (0 only checks for a give-back). Fits its
    window when the pane fills it and no terminal is attached, or the viewer forces it;
    never one the user sized themselves. With a terminal attached and no force, a fitted
    window is given back: the toggle unticked, or a terminal attached since. Returns
    whether a terminal is attached, which is when the phone offers the toggle."""
    with _lock:
        out = _run(["display-message", "-p", "-t", pane_id, _FMT, ";",
                    "show-options", "-wqv", "-t", pane_id, "window-size"]).split("\n")
        panes, zoomed, active, linked, watched, width, size, mark, members = out[0].split("\t")
        if not panes:  # tmux 3.7 prints blanks, exit 0, for a pane that is gone
            raise subprocess.CalledProcessError(1, "display-message")
        attached = bool(set(linked.split(",")) & set(watched.split(",")) - {""})
        fills = panes == "1" or zoomed == active == "1"
        want = str(max(MIN_COLS, cols))
        # Resized, or its window-size changed, by someone else since (our fit leaves it
        # manual): theirs now.
        if mark and (width != mark.rstrip("!") or out[1].strip() != "manual"):
            _run(["set-option", "-wu", "-t", pane_id, MARK])
        elif cols and fills and (force or not attached) and (mark or not out[1].strip()) \
                and (want, want + "!" * force) != (width, mark):
            _resize(pane_id, members, size, ["resize-window", "-x", want, "-t", pane_id, ";",
                                             "set-option", "-w", "-t", pane_id, MARK,
                                             want + "!" * force])
        # The watcher (cols 0) gives back only an unforced fit, read under the lock.
        elif mark and attached and not force and (cols or not mark.endswith("!")):
            # -A leaves window-size at manual: unset it too
            _resize(pane_id, members, size, ["resize-window", "-A", "-t", pane_id, ";",
                                             "set-option", "-wu", "-t", pane_id, "window-size",
                                             ";", "set-option", "-wu", "-t", pane_id, MARK])
        return attached


def _resize(pane_id: str, members: str, size: str, args: list[str]) -> None:
    """Run `args`; if the window's size changed (only then do its apps redraw), hand its
    panes a token for the watcher, stamped with when the resize began."""
    began = time.monotonic()
    out = _run([*args, ";", "display-message", "-p", "-t", pane_id, _SIZE])
    if out.strip().rsplit("\n", 1)[-1] != size:
        _resized.update(dict.fromkeys(members.split(), began))


def release_attached(panes: list[tmux.Pane]) -> None:
    """The watcher's tick: a terminal attached to a session holding a window we fitted
    without force gets it back. `panes` is every (session, pane) row, so any attached
    session sharing the window counts."""
    for pane in panes:
        if pane.fit and not pane.fit.endswith("!") and pane.is_attached:
            with suppress(subprocess.CalledProcessError):  # closed meanwhile
                view(pane.id, 0)
