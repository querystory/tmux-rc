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
# The window's sessions and every attached session: a window can be in several (a group,
# link-window), and a terminal on any of them sees it shrink.
_FMT = "\t".join(["#{window_id}", "#{window_panes}", "#{window_zoomed_flag}", "#{pane_active}",
                  "#{window_linked_sessions_list}", "#{S:#{?session_attached,#{session_name}#,,}}",
                  "#{window_width}", f"#{{{MARK}}}"])


def _run(args: list[str]) -> str:
    return tmux._run(args)  # noqa: SLF001 - via the module, so a swapped-out tmux (demo, tests) holds


# Held across each read-decide-resize, so a stale read can never undo a newer one.
_lock = threading.Lock()
_viewers: dict[str, dict[str, tuple[int, float]]] = {}  # window -> viewer -> (cols, seen)


def _settle(window: str, width: str, ours: str, *, theirs: bool) -> None:
    """Size the window for its live viewers (the narrowest), or give it back when none is
    left. `ours` is the width we gave it; `theirs`, a window-size the user set themselves."""
    now = time.monotonic()
    viewers = _viewers.get(window, {})
    for viewer in [v for v, (_, seen) in viewers.items() if now - seen >= IDLE_SECONDS]:
        del viewers[viewer]
    want = min((cols for cols, _ in viewers.values()), default=0)
    if ours and width != ours:  # resized by someone else since: theirs now
        _run(["set-option", "-wu", "-t", window, MARK])
    elif not want:
        if ours:  # -A leaves window-size at manual: unset it to inherit again
            _run(["resize-window", "-A", "-t", window, ";", "set-option", "-wu", "-t", window,
                  "window-size", ";", "set-option", "-wu", "-t", window, MARK])
    elif (ours or not theirs) and str(want) != width:
        _run(["resize-window", "-x", str(want), "-t", window, ";",
              "set-option", "-w", "-t", window, MARK, str(want)])


def view(pane_id: str, viewer: str, cols: int, force: bool = False) -> bool:
    """A viewer showing `cols` columns of the pane (0: it left). Fits the window when the
    pane fills it (its only pane, or zoomed) and no terminal is attached to its session (or
    the viewer forces it); restores it when nobody wants it fitted any more. Returns whether
    a terminal is attached, which is when the viewer offers its force toggle."""
    with _lock:
        out = _run(["display-message", "-p", "-t", pane_id, _FMT, ";",
                    "show-options", "-wqv", "-t", pane_id, "window-size"]).split("\n")
        window, panes, zoomed, active, linked, watched, width, ours = out[0].split("\t")
        if not window:  # tmux 3.7 prints blanks, exit 0, for a pane that is gone
            raise subprocess.CalledProcessError(1, "display-message")
        attached = bool(set(linked.split(",")) & set(watched.split(",")) - {""})
        viewers = _viewers.setdefault(window, {})
        if cols > 0 and (panes == "1" or zoomed == active == "1") and (force or not attached):
            viewers[viewer] = (max(MIN_COLS, cols), time.monotonic())
        else:
            viewers.pop(viewer, None)
        _settle(window, width, ours, theirs=bool(out[1].strip()))
    return attached


def sweep() -> None:
    """Restore every marked window no live viewer wants: after viewers go quiet, and on
    startup for whatever a previous daemon left narrow."""
    with _lock:
        try:
            rows = _run(["list-windows", "-a", "-F",
                         f"#{{window_id}}\t#{{window_width}}\t#{{{MARK}}}"]).splitlines()
        except subprocess.CalledProcessError:
            return  # no server: nothing is narrow
        for window, width, ours in (row.split("\t") for row in rows):
            if ours:
                with suppress(subprocess.CalledProcessError):  # closed meanwhile
                    _settle(window, width, ours, theirs=True)


async def run() -> None:
    while True:
        await asyncio.to_thread(sweep)
        await asyncio.sleep(SWEEP_SECONDS)
