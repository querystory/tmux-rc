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
# A window's row: its id, its sessions and every attached session (a window can be in
# several, a group or link-window, and a terminal on any of them sees it shrink), its width
# and our mark.
_ROW = "\t".join(["#{window_id}", "#{window_linked_sessions_list}",
                  "#{S:#{?session_attached,#{session_name}#,,}}", "#{window_width}",
                  f"#{{{MARK}}}"])


def _run(args: list[str]) -> str:
    return tmux._run(args)  # noqa: SLF001 - via the module, so a swapped-out tmux (demo, tests) holds


def _attached(linked: str, watched: str) -> bool:
    return bool(set(linked.split(",")) & set(watched.split(",")) - {""})


# Held across each read-decide-resize, so a stale read can never undo a newer one.
_lock = threading.Lock()
# viewer -> (window, cols, seen, force): one page views one pane, so one window at a time.
_viewers: dict[str, tuple[str, int, float, bool]] = {}


def _settle() -> None:
    """Give every window what its live viewers want: the narrowest width among those it
    may fit (all of them, or only the ones that forced it while a terminal is attached),
    else its own size back. A width that is no longer ours is the user's: unmark it and
    stop fitting it."""
    try:
        rows = set(_run(["list-windows", "-a", "-F", _ROW]).splitlines())
    except subprocess.CalledProcessError:
        rows = set()  # no server: nothing is narrow
    now = time.monotonic()
    windows = {row.split("\t")[0] for row in rows}
    for viewer, (window, _, seen, _) in list(_viewers.items()):
        if now - seen >= IDLE_SECONDS or window not in windows:
            del _viewers[viewer]
    for window, linked, watched, width, ours in (row.split("\t") for row in rows):
        attached = _attached(linked, watched)
        want = min((cols for w, cols, _, force in _viewers.values()
                    if w == window and (force or not attached)), default=0)
        with suppress(subprocess.CalledProcessError):  # closed meanwhile
            if ours and width != ours:
                _run(["set-option", "-wu", "-t", window, MARK])
                for viewer in [v for v, (w, *_) in _viewers.items() if w == window]:
                    del _viewers[viewer]
            elif not want:
                if ours:  # -A leaves window-size at manual: unset it to inherit again
                    _run(["resize-window", "-A", "-t", window, ";", "set-option", "-wu", "-t",
                          window, "window-size", ";", "set-option", "-wu", "-t", window, MARK])
            elif str(want) != width:
                _run(["resize-window", "-x", str(want), "-t", window, ";",
                      "set-option", "-w", "-t", window, MARK, str(want)])


def view(pane_id: str, viewer: str, cols: int, force: bool = False) -> bool:
    """A viewer showing `cols` columns of the pane (0: it left). It may fit the window when
    the pane fills it (its only pane, or zoomed) and its window-size is not one the user
    set; whether it does depends on attachment (see _settle). Returns whether a terminal
    is attached, which is when the viewer offers its force toggle."""
    with _lock:
        _viewers.pop(viewer, None)  # wherever it was: a release works even for a pane gone
        try:
            out = _run(["display-message", "-p", "-t", pane_id,
                        f"#{{window_panes}}\t#{{window_zoomed_flag}}\t#{{pane_active}}\t{_ROW}",
                        ";", "show-options", "-wqv", "-t", pane_id, "window-size"]).split("\n")
            panes, zoomed, active, window, linked, watched, _, ours = out[0].split("\t")
            if not window:  # tmux 3.7 prints blanks, exit 0, for a pane that is gone
                raise subprocess.CalledProcessError(1, "display-message")
            fills = panes == "1" or zoomed == active == "1"
            if cols > 0 and fills and (ours or not out[1].strip()):
                _viewers[viewer] = (window, max(MIN_COLS, cols), time.monotonic(), force)
        finally:
            _settle()
    return _attached(linked, watched)


def sweep() -> None:
    """_settle on a timer: after viewers go quiet, and on startup for whatever a previous
    daemon left narrow."""
    with _lock:
        _settle()


async def run() -> None:
    while True:
        await asyncio.to_thread(sweep)
        await asyncio.sleep(SWEEP_SECONDS)
