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
_PANE = f"#{{pane_id}}\t#{{window_panes}}\t#{{window_zoomed_flag}}\t#{{pane_active}}\t{_ROW}"


def _run(args: list[str]) -> str:
    return tmux._run(args)  # noqa: SLF001 - via the module, so a swapped-out tmux (demo, tests) holds


def _attached(linked: str, watched: str) -> bool:
    return bool(set(linked.split(",")) & set(watched.split(",")) - {""})


# Held across each read-decide-resize, so a stale read can never undo a newer one.
_lock = threading.Lock()
# viewer -> (pane, cols, seen, force): one page views one pane at a time.
_viewers: dict[str, tuple[str, int, float, bool]] = {}


def _settle(checked: str = "") -> list[str]:
    """Give every window what its live viewers want: the narrowest width among viewers
    whose pane still fills it (its only pane, or zoomed) and who may fit it (all, or only
    those that forced it while a terminal is attached); else its own size back. A width
    that is no longer ours is the user's: unmark it and drop its viewers. An unmarked
    window is fitted only when it is `checked`, the one whose window-size view() just
    found the user had not set. Returns what it changed, for the audit log."""
    done = []
    try:
        rows = set(_run(["list-panes", "-a", "-F", _PANE]).splitlines())
    except subprocess.CalledProcessError:
        rows = set()  # no server: nothing is narrow
    fills, windows = {}, {}
    for pane, panes, zoomed, active, window, linked, watched, width, ours in (
            row.split("\t") for row in rows):
        fills[pane] = window if panes == "1" or zoomed == active == "1" else ""
        windows[window] = (_attached(linked, watched), width, ours)
    now = time.monotonic()
    for viewer, (pane, _, seen, _) in list(_viewers.items()):
        if now - seen >= IDLE_SECONDS or pane not in fills:
            del _viewers[viewer]
    for window, (attached, width, ours) in windows.items():
        want = min((cols for pane, cols, _, force in _viewers.values()
                    if fills[pane] == window and (force or not attached)), default=0)
        with suppress(subprocess.CalledProcessError):  # closed meanwhile
            if ours and width != ours:
                _run(["set-option", "-wu", "-t", window, MARK])
                done.append(f"{window} left to the user")
                for viewer in [v for v, (p, *_) in _viewers.items() if fills[p] == window]:
                    del _viewers[viewer]
            elif not want:
                if ours:  # -A leaves window-size at manual: unset it to inherit again
                    _run(["resize-window", "-A", "-t", window, ";", "set-option", "-wu", "-t",
                          window, "window-size", ";", "set-option", "-wu", "-t", window, MARK])
                    done.append(f"{window} restored")
            elif str(want) != width and (ours or window == checked):
                _run(["resize-window", "-x", str(want), "-t", window, ";",
                      "set-option", "-w", "-t", window, MARK, str(want)])
                done.append(f"{window} {want} cols")
    return done


def view(pane_id: str, viewer: str, cols: int, force: bool = False) -> tuple[bool, list[str]]:
    """A viewer showing `cols` columns of the pane (0: it left). It may fit the window
    unless the user set the window's window-size themselves; _settle decides whether it
    does. Returns whether a terminal is attached, which is when the viewer offers its
    force toggle, and what changed."""
    with _lock:
        _viewers.pop(viewer, None)  # wherever it was: a release works even for a pane gone
        checked = ""
        try:
            out = _run(["display-message", "-p", "-t", pane_id, f"#{{pane_id}}\t{_ROW}", ";",
                        "show-options", "-wqv", "-t", pane_id, "window-size"]).split("\n")
            pane, window, linked, watched, _, ours = out[0].split("\t")
            if not window:  # tmux 3.7 prints blanks, exit 0, for a pane that is gone
                raise subprocess.CalledProcessError(1, "display-message")
            if ours or not out[1].strip():
                checked = window
                if cols > 0:
                    _viewers[viewer] = (pane, max(MIN_COLS, cols), time.monotonic(), force)
        finally:
            done = _settle(checked)
    return _attached(linked, watched), done


def sweep() -> None:
    """_settle on a timer: after viewers go quiet, and on startup for whatever a previous
    daemon left narrow."""
    with _lock:
        _settle()


async def run() -> None:
    while True:
        await asyncio.to_thread(sweep)
        await asyncio.sleep(SWEEP_SECONDS)
