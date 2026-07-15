"""Thin wrappers over the tmux CLI.

We observe panes with `capture-pane` (non-disruptive, works from an unrelated
process) and inject input with `send-keys`. Nothing here holds a pty or attaches
to the session, so a human can stay attached at the same time.
"""

from __future__ import annotations

import os
import shutil
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
        "#{pane_current_path}",
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
    cwd: str = ""

    @property
    def label(self) -> str:
        """Human label, best identity first. A window the user named (e.g. "Resolve PR
        38") wins. Otherwise tmux auto-named the window after its command (bash/node),
        which is noise — prefer the SESSION name the user deliberately set (e.g.
        "termiphone-dev", shown in the tmux status bar), then the cwd basename, then
        session:window."""
        if _meaningful(self.window_name):
            return self.window_name
        if _meaningful(self.session):
            return self.session
        if self.cwd:
            base = self.cwd.rstrip("/").rsplit("/", 1)[-1]
            if base:
                return base
        return f"{self.session}:{self.window_index}"


# tmux auto-assigns these as window names from the running command — not user intent.
_GENERIC_NAMES = {"bash", "zsh", "sh", "fish", "node", "python", "python3", "tmux", "ssh"}


def _meaningful(name: str) -> bool:
    """True if a session/window name looks user-chosen (not a tmux default). Rejects
    empties, generic command names, and pure numbers (tmux's default "0","1",…)."""
    return bool(name) and name.lower() not in _GENERIC_NAMES and not name.isdigit()


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
        if len(parts) != 8:
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


def select_pane(pane_id: str) -> None:
    """Make this pane the active one in tmux (focus its window + the pane within it),
    so tapping a card on the phone focuses the same pane on the host."""
    _run(["select-window", "-t", pane_id])
    _run(["select-pane", "-t", pane_id])


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


def set_clipboard_image(data: bytes, mime: str = "image/png") -> list[str]:
    """Put image bytes on the host's system clipboard so a terminal app can paste them
    (Claude Code embeds an image on Ctrl-V from the clipboard). Writes to both the
    Wayland (wl-copy) and X11/XWayland (xclip) clipboards when available, since which
    one the terminal reads depends on the session. Returns the tools that succeeded."""
    ok: list[str] = []
    env_x = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")}
    attempts = [
        ("wl-copy", ["wl-copy", "-t", mime]),
        ("xclip", ["xclip", "-selection", "clipboard", "-t", mime]),
    ]
    for name, cmd in attempts:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            # These tools do NOT exit — they persist to own/serve the clipboard
            # selection (X11) or hold it (Wayland). So we must feed stdin and detach,
            # NOT wait for completion (which would hang). Write bytes, close stdin,
            # and leave the process running in the background.
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env_x if name == "xclip" else None,
            )
            proc.stdin.write(data)
            proc.stdin.close()
            ok.append(name)
        except Exception:  # noqa: BLE001 - try the next tool
            continue
    return ok
