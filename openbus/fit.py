"""Fit a window's width to the screen viewing it (docs/design/fit-width.md).

The phone renders capture-pane output, so lines arrive wrapped at the window's width. While
a client views a pane it reports its column count here, and a single-pane (or zoomed)
window is resized to that width; its height is left alone. A window tmux-rc resized carries
the width it was given in the window option @tmuxrc-fit, so the mark lives exactly as long
as the window and a restarted daemon finds every window it has to put back."""

import asyncio
import subprocess
import threading
import time
from contextlib import suppress

from . import tmux

MIN_COLS = 40  # narrower and most TUIs stop laying out sensibly
IDLE_SECONDS = 180  # a viewer that stops polling for this long has left
SWEEP_SECONDS = 60
MARK = "@tmuxrc-fit"
_FMT = "\t".join(["#{window_id}", "#{window_panes}", "#{window_zoomed_flag}",
                  "#{session_attached}", "#{window_width}", f"#{{{MARK}}}"])

def _run(args: list[str]) -> str:
    return tmux._run(args)  # noqa: SLF001 - via the module, so a swapped-out tmux (demo, tests) holds


_lock = threading.Lock()
_viewers: dict[str, dict[str, tuple[int, float]]] = {}  # window -> viewer -> (cols, seen)


def _want(window: str, now: float) -> int:
    """The narrowest width a live viewer of the window asked for, else 0. Prunes the rest."""
    viewers = _viewers.get(window, {})
    for viewer in [v for v, (_, seen) in viewers.items() if now - seen >= IDLE_SECONDS]:
        del viewers[viewer]
    return min((cols for cols, _ in viewers.values()), default=0)


def _restore(window: str) -> None:
    """Hand the window back: -A sizes it to its clients (or its last size, with none), and
    -A leaves window-size at manual, so unset it to inherit the session/global value."""
    _run(["resize-window", "-A", "-t", window, ";", "set-option", "-wu", "-t", window,
               "window-size", ";", "set-option", "-wu", "-t", window, MARK])


def view(pane_id: str, viewer: str, cols: int, force: bool = False) -> bool:
    """A viewer showing `cols` columns of the pane (0: it left). Fits the window when it is
    one pane wide and no terminal is attached to its session (or the viewer forces it);
    restores it when nobody wants it fitted any more. Returns whether a terminal is
    attached, which is when the viewer offers its force toggle."""
    out = _run(["display-message", "-p", "-t", pane_id, _FMT, ";",
                     "show-options", "-wqv", "-t", pane_id, "window-size"]).split("\n")
    window, panes, zoomed, attached, width, ours = out[0].split("\t")
    if not window:  # tmux 3.7 prints blanks, exit 0, for a pane that is gone
        raise subprocess.CalledProcessError(1, "display-message")
    attached = attached not in ("", "0")
    fits = cols > 0 and (panes == "1" or zoomed == "1") and (force or not attached)
    with _lock:
        viewers = _viewers.setdefault(window, {})
        if fits:
            viewers[viewer] = (max(MIN_COLS, cols), time.monotonic())
        else:
            viewers.pop(viewer, None)
        want = _want(window, time.monotonic())
    if ours and width != ours:  # resized by someone else since: theirs now
        _run(["set-option", "-wu", "-t", window, MARK])
    elif not want:
        if ours:
            _restore(window)
    elif (ours or not out[1].strip()) and str(want) != width:  # never a size the user set
        _run(["resize-window", "-x", str(want), "-t", window, ";",
                   "set-option", "-w", "-t", window, MARK, str(want)])
    return attached


def sweep() -> None:
    """Restore every marked window no live viewer wants: after viewers go quiet, and on
    startup for whatever a previous daemon left narrow."""
    try:
        rows = _run(["list-windows", "-a", "-F", f"#{{window_id}}\t#{{{MARK}}}"]).splitlines()
    except subprocess.CalledProcessError:
        return  # no server: nothing is narrow
    for window, _, ours in (row.partition("\t") for row in rows):
        with _lock:
            want = _want(window, time.monotonic())
        if ours and not want:
            with suppress(subprocess.CalledProcessError):  # closed meanwhile
                _restore(window)


async def run() -> None:
    while True:
        await asyncio.to_thread(sweep)
        await asyncio.sleep(SWEEP_SECONDS)
