"""Fit a window to the phone viewing it (docs/design/fit-width.md).

The phone renders capture-pane output, and the apps in it lay themselves out at their
window's width. When the phone opens a pane that fills its window (its only pane, or
zoomed) it reports its column count, and the window takes that width. Its height is left
alone, unless the fit is "tall": a full-screen app (on the alternate screen, so tmux keeps
it no history) then gets a tall window, whose extra rows are what the phone scrolls
through. A fitted window carries what it was given in @tmuxrc-fit ("<cols>" or
"<cols>x<rows>", with "!" when the viewer forced it past an attached terminal), and keeps
it after the viewer leaves. It is given back when a terminal attaches to an unforced fit,
or the viewer picks Off for the pane."""

import re
import subprocess
import threading
import time
from contextlib import suppress

from . import tmux

MIN_COLS = 40  # narrower and most TUIs stop laying out sensibly
MARK = "@tmuxrc-fit"
# A client counts as a terminal someone is at only if it was used this recently: tmux keeps
# a desk terminal attached for days after its owner walked away (or reaches the machine only
# through tmux-rc), and a forgotten one must not hold every window at its size.
ACTIVE_CLIENT_SECONDS = 15 * 60
REDRAW_SECONDS = 1.0  # how long an app takes to redraw: looks before then may see it half done
_SIZE = "#{window_width}x#{window_height}"
# The pane's window: does the pane fill it, its sessions and every client's session and last
# activity (a window can be in several sessions, and a terminal on any of them sees it
# shrink), whether the pane's app is on the alternate screen, its size, our mark, and its
# panes (all of them redraw when it is resized).
_FMT = "\t".join(["#{window_panes}", "#{window_zoomed_flag}", "#{pane_active}",
                  "#{window_linked_sessions_list}",
                  "#{L:#{client_activity} #{client_session},}", "#{alternate_on}", _SIZE,
                  f"#{{{MARK}}}", "#{P:#{pane_id} }"])

_REFLOWED = re.compile(r"[\s\u2500-\u259f]+")  # whitespace, box drawing, block elements
_lock = threading.Lock()  # one read-decide-resize at a time
# pane -> when the first and the latest still-unread resizes of its window began
_resized: dict[str, tuple[float, float]] = {}


def _run(args: list[str]) -> str:
    return tmux._run(args)  # noqa: SLF001 - via the module, so a swapped-out tmux (demo, tests) holds


def just_resized(pane_id: str, looked: float) -> str:
    """Where a look (its capture finished at `looked`) stands against our last resize of
    the pane's window: "redraw" while the app may still be redrawing, so the screen can be
    half done; "settled" for the first look after that, which spends the resize; "" with
    none pending, or one begun after the capture finished (it cannot be in it). Under the
    lock, so a look taken while a resize lands waits for its token."""
    with _lock:
        first, latest = _resized.get(pane_id, (float("inf"),) * 2)
        if looked < first:  # none pending, or begun after this capture
            return ""
        if looked - latest < REDRAW_SECONDS:
            return "redraw"
        del _resized[pane_id]
        return "settled"


def reflow_key(text: str) -> str:
    """A screen's content regardless of the width it was laid out at: what a reflow moves
    around (whitespace) or redraws to a new length (box-drawing rules) is left out, and
    nothing else (markers such as a draft's placeholder are content)."""
    return _REFLOWED.sub("", text)


def _in_use(clients: str) -> set[str]:
    """The sessions a recently active client is attached to, from the client loop in _FMT."""
    since = time.time() - ACTIVE_CLIENT_SECONDS
    return {session for activity, _, session in (c.partition(" ") for c in clients.split(","))
            if session and activity.isdigit() and int(activity) >= since}


def alternate(pane_id: str) -> bool | None:
    """Is the pane's app on the alternate screen (None if the pane is gone)?"""
    try:
        return _run(["display-message", "-p", "-t", pane_id, "#{alternate_on}"]).strip() == "1"
    except subprocess.CalledProcessError:
        return None


def view(pane_id: str, cols: int, force: bool = False, mode: str = "width",
         rows: int = 0) -> bool:
    """The phone shows `cols` columns of the pane (0 only checks for a give-back). Fits its
    window when the pane fills it and no terminal is attached, or the viewer forces it (a
    choice made for this pane); never one the user sized themselves. `mode` is "width",
    "tall" (and `rows` high), "auto" (tall only for an app on the alternate screen, which
    tmux keeps no history for) or "off". A fitted window is given back for a forced "off",
    and, without force, once a terminal is attached. Returns whether one is."""
    with _lock:
        out = _run(["display-message", "-p", "-t", pane_id, _FMT, ";",
                    "show-options", "-wqv", "-t", pane_id, "window-size"]).split("\n")
        panes, zoomed, active, linked, clients, alt, size, mark, members = out[0].split("\t")
        if not panes:  # tmux 3.7 prints blanks, exit 0, for a pane that is gone
            raise subprocess.CalledProcessError(1, "display-message")
        attached = bool(set(linked.split(",")) & _in_use(clients))
        fills = panes == "1" or zoomed == active == "1"
        width, height = size.split("x")
        fit_w, _, fit_h = mark.rstrip("!").partition("x")  # what we gave it, if anything
        tall = rows > 0 and (mode == "tall" or (mode == "auto" and alt == "1"))
        want = str(max(MIN_COLS, cols))
        new = want + f"x{rows}" * tall + "!" * force
        # -A leaves window-size at manual: unset it too.
        give_back = ["resize-window", "-A", "-t", pane_id, ";", "set-option", "-wu", "-t",
                     pane_id, "window-size", ";", "set-option", "-wu", "-t", pane_id, MARK]
        # Resized, or its window-size changed, by someone else since (our fit leaves it
        # manual): theirs now.
        if mark and (width != fit_w or (fit_h and height != fit_h)
                     or out[1].strip() != "manual"):
            _run(["set-option", "-wu", "-t", pane_id, MARK])
        elif mode == "off":
            if mark and force:
                _resize(pane_id, members, size, give_back)
        elif cols and fills and (force or not attached) and (mark or not out[1].strip()) \
                and new != mark:
            _resize(pane_id, members, size, [
                # From tall to width only: the height goes back first.
                *(["resize-window", "-A", "-t", pane_id, ";"] if fit_h and not tall else []),
                "resize-window", "-x", want, *(["-y", str(rows)] if tall else []),
                "-t", pane_id, ";", "set-option", "-w", "-t", pane_id, MARK, new])
        # The watcher (cols 0) gives back only an unforced fit, read under the lock.
        elif mark and attached and not force and (cols or not mark.endswith("!")):
            _resize(pane_id, members, size, give_back)
        return attached


def _resize(pane_id: str, members: str, size: str, args: list[str]) -> None:
    """Run `args`; if the window's size changed (only then do its apps redraw), hand its
    panes a token for the watcher, stamped with when the resize began. A token not yet
    spent keeps its first stamp (a second resize can't hide the first one's redraw) and
    takes the new one as its latest (whose redraw must finish before the screen settles)."""
    began = time.monotonic()
    out = _run([*args, ";", "display-message", "-p", "-t", pane_id, _SIZE])
    if out.strip().rsplit("\n", 1)[-1] != size:
        for pane in members.split():
            _resized[pane] = (_resized.get(pane, (began,))[0], began)


def release_attached(panes: list[tmux.Pane]) -> None:
    """The watcher's tick: a terminal attached to a session holding a window we fitted
    without force gets it back. `panes` is every (session, pane) row, so any attached
    session sharing the window counts."""
    for pane in panes:
        if pane.fit and not pane.fit.endswith("!") and pane.is_attached:
            with suppress(subprocess.CalledProcessError):  # closed meanwhile
                view(pane.id, 0)
