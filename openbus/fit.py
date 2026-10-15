"""Fit a window's width to the phone viewing it (docs/design/fit-width.md).

The phone renders capture-pane output, and the apps in it lay themselves out at their
window's width. When the phone opens a pane that fills its window (its only pane, or
zoomed) it reports its column count, and the window takes that width; its height is left
alone. A fitted window carries the width in @tmuxrc-fit, with "!" when the viewer forced
it past an attached terminal, and keeps it after the viewer leaves. It is given back only
when a terminal attaches to an unforced fit, or the viewer unticks "Fit to my screen"."""

import subprocess
import threading
import time
from contextlib import suppress

from . import tmux

MIN_COLS = 40  # narrower and most TUIs stop laying out sensibly
MARK = "@tmuxrc-fit"
SETTLE_SECONDS = 3.0  # a redraw this soon after our own resize is the resize, not the app
# The pane's window: does the pane fill it, its sessions and every attached session (a
# window can be in several, and a terminal on any of them sees it shrink), its width, our
# mark, and its panes (all of them redraw when it is resized).
_FMT = "\t".join(["#{window_panes}", "#{window_zoomed_flag}", "#{pane_active}",
                  "#{window_linked_sessions_list}",
                  "#{S:#{?session_attached,#{session_name}#,,}}", "#{window_width}",
                  f"#{{{MARK}}}", "#{P:#{pane_id} }"])

_lock = threading.Lock()  # one read-decide-resize at a time
_resized: dict[str, float] = {}  # pane -> when we last resized its window


def _run(args: list[str]) -> str:
    return tmux._run(args)  # noqa: SLF001 - via the module, so a swapped-out tmux (demo, tests) holds


def just_resized(pane_id: str) -> bool:
    """Did we resize this pane's window a moment ago? Its app redraws for the new width,
    and the watcher must not read that redraw as the app doing something. One answer per
    resize: the change after the redraw is the app's own."""
    return time.monotonic() - _resized.pop(pane_id, float("-inf")) < SETTLE_SECONDS


def view(pane_id: str, cols: int, force: bool = False) -> bool:
    """The phone shows `cols` columns of the pane (0 only checks for a give-back). Fits its
    window when the pane fills it and no terminal is attached, or the viewer forces it;
    never one the user sized themselves. With a terminal attached and no force, a fitted
    window is given back: the toggle unticked, or a terminal attached since. Returns
    whether a terminal is attached, which is when the phone offers the toggle."""
    with _lock:
        out = _run(["display-message", "-p", "-t", pane_id, _FMT, ";",
                    "show-options", "-wqv", "-t", pane_id, "window-size"]).split("\n")
        panes, zoomed, active, linked, watched, width, mark, members = out[0].split("\t")
        if not panes:  # tmux 3.7 prints blanks, exit 0, for a pane that is gone
            raise subprocess.CalledProcessError(1, "display-message")
        attached = bool(set(linked.split(",")) & set(watched.split(",")) - {""})
        fills = panes == "1" or zoomed == active == "1"
        want = str(max(MIN_COLS, cols))
        if mark and width != mark.rstrip("!"):  # resized by someone else since: theirs now
            _run(["set-option", "-wu", "-t", pane_id, MARK])
        elif cols and fills and (force or not attached) and (mark or not out[1].strip()) \
                and (want, want + "!" * force) != (width, mark):
            _resize(members, ["resize-window", "-x", want, "-t", pane_id, ";",
                              "set-option", "-w", "-t", pane_id, MARK, want + "!" * force])
        # The watcher (cols 0) gives back only an unforced fit, read under the lock.
        elif mark and attached and not force and (cols or not mark.endswith("!")):
            # -A leaves window-size at manual: unset it too
            _resize(members, ["resize-window", "-A", "-t", pane_id, ";", "set-option", "-wu",
                              "-t", pane_id, "window-size", ";", "set-option", "-wu", "-t",
                              pane_id, MARK])
        return attached


def _resize(members: str, args: list[str]) -> None:
    _run(args)
    _resized.update(dict.fromkeys(members.split(), time.monotonic()))


def release_attached(panes: list[tmux.Pane]) -> None:
    """The watcher's tick: a terminal attached to a session holding a window we fitted
    without force gets it back. `panes` is every (session, pane) row, so any attached
    session sharing the window counts."""
    for pane in panes:
        if pane.fit and not pane.fit.endswith("!") and pane.is_attached:
            with suppress(subprocess.CalledProcessError):  # closed meanwhile
                view(pane.id, 0)
