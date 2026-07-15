"""Thin wrappers over the tmux CLI.

We observe panes with `capture-pane` (non-disruptive, works from an unrelated
process) and inject input with `send-keys`. Nothing here holds a pty or attaches
to the session, so a human can stay attached at the same time.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

# Format string for `list-panes -F`. Fields are tab-separated so pane titles /
# commands containing spaces don't break parsing.
_PANE_FMT = "\t".join(
    [
        "#{session_name}",
        "#{window_index}",
        "#{window_name}",
        "#{pane_index}",
        "#{pane_id}",
        "#{pane_current_command}",
        "#{pane_title}",
    ]
)


@dataclass(frozen=True)
class Pane:
    """Identity of a tmux pane. `id` (e.g. "%3") is the stable target for tmux commands."""

    session: str
    window_index: str
    window_name: str
    pane_index: str
    id: str
    current_command: str
    title: str

    @property
    def label(self) -> str:
        """Human label like "work:0" — session plus window."""
        return f"{self.session}:{self.window_index}"


def _run(args: list[str]) -> str:
    """Run a tmux command, returning stdout. Raises on non-zero exit."""
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=True
    ).stdout


def server_running() -> bool:
    """True if a tmux server is up (avoids noisy errors when nothing is running)."""
    try:
        _run(["list-sessions"])
        return True
    except subprocess.CalledProcessError:
        return False


def list_panes() -> list[Pane]:
    """All panes across all sessions/windows."""
    out = _run(["list-panes", "-a", "-F", _PANE_FMT])
    panes: list[Pane] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        panes.append(Pane(*parts))
    return panes


def find_pane(target: str | None) -> Pane | None:
    """Resolve a target pane. `target` may be a pane id ("%3") or "session:window"
    label; None picks the first pane. Returns None if nothing matches."""
    panes = list_panes()
    if not panes:
        return None
    if target is None:
        return panes[0]
    for p in panes:
        if target in (p.id, p.label, f"{p.label}.{p.pane_index}"):
            return p
    return None


def capture_pane(pane_id: str, lines: int = 200) -> str:
    """Current visible text of a pane, most recent `lines` rows. `-p` prints to
    stdout, `-J` joins wrapped lines so long lines aren't split mid-word."""
    out = _run(["capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{lines}"])
    return out.rstrip("\n")


def send_keys(pane_id: str, keys: str, enter: bool = True, literal: bool = True) -> None:
    """Send `keys` to a pane. When `literal` (default), text is sent with `-l` so it
    isn't interpreted as tmux key names — for typed answers. When not literal, `keys`
    is a tmux key-name like "Escape", "Up", or "C-c", sent as that key. `enter` appends
    a Return (only meaningful for literal text)."""
    _run(["send-keys", "-t", pane_id, *(["-l"] if literal else []), keys])
    if enter and literal:
        _run(["send-keys", "-t", pane_id, "Enter"])
