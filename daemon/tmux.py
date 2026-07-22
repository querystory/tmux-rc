"""Thin wrappers over the tmux CLI.

We observe panes with `capture-pane` (non-disruptive, works from an unrelated
process) and inject input with `send-keys`. Nothing here holds a pty or attaches
to the session, so a human can stay attached at the same time.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass
from functools import cache

_HOST = socket.gethostname()
# Leading spinner/status glyphs agents prepend to their title (Claude Code: ✳ working,
# braille dots idle). The UI has its own activity indicators — strip them.
_TITLE_GLYPHS = re.compile(r"^[⠀-⣿✳✶✻✽·∗*\s]+")

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
        "#{pane_pid}",
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
    # PID of the pane's process. tmux recycles pane ids ("%3") when panes close, so id
    # alone isn't a durable identity — the pid disambiguates a reused id from the
    # original pane, letting the watcher evict stale buffers. (pane_start_time is empty
    # on tmux 3.4; pane_pid is populated and just as unique.)
    pid: str = ""

    @property
    def display_title(self) -> str | None:
        """The title the pane's own app set (agents publish their state here: Claude
        Code writes '<glyph> <task summary>' — free, accurate, no LLM needed). tmux
        defaults the title to the hostname, which is noise -> None."""
        t = _TITLE_GLYPHS.sub("", self.title).strip()
        return t if t and t not in (_HOST, _HOST.split(".")[0]) else None

    @property
    def label(self) -> str:
        """Human label, best identity first. A window the user named (e.g. "Resolve PR
        38") wins. Otherwise tmux auto-named the window after its command (bash/node),
        which is noise — prefer the SESSION name the user deliberately set (e.g.
        "tmux-rc-dev", shown in the tmux status bar), then the cwd basename, then
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
_GENERIC_NAMES = {
    "bash",
    "zsh",
    "sh",
    "fish",
    "node",
    "python",
    "python3",
    "tmux",
    "ssh",
}


def _meaningful(name: str) -> bool:
    """True if a session/window name looks user-chosen (not a tmux default). Rejects
    empties, generic command names, and pure numbers (tmux's default "0","1",…)."""
    return bool(name) and name.lower() not in _GENERIC_NAMES and not name.isdigit()


def _run(args: list[str]) -> str:
    """Run a tmux command, returning stdout. Raises on non-zero exit.

    Bounded by a timeout so a wedged tmux (server hang, blocked pipe) can't block the poll
    thread forever and silently freeze all cards (the 'stale, no error' failure). A
    timeout is re-raised as CalledProcessError so it flows through the SAME error paths
    callers already handle (server_running/prefix_key/active_pane_id catch only that) —
    otherwise a raw TimeoutExpired would leak past them and fail a tick unexpectedly."""
    try:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, check=True, timeout=10
        ).stdout
    except subprocess.TimeoutExpired as e:
        raise subprocess.CalledProcessError(returncode=124, cmd=e.cmd) from e


@cache
def server_uid() -> str:
    """Stable identity of the tmux SERVER: '<boot_id>:<server_pid>'.

    boot_id (a fresh kernel UUID per boot, from /proc) plus the server's pid uniquely
    pins one tmux server instance: pid can't be reused by two live processes within a
    boot, and boot_id changes on reboot (so a reused pid across reboots can't collide).
    No hostname — boot_id already avoids cross-machine collisions. Cached: constant for
    the life of this daemon (and re-derived identically if the daemon restarts, so a
    pane's uid survives a tmux-rc restart). Falls back to just the pid if either read
    fails, so telemetry degrades rather than breaking."""
    try:
        boot = open("/proc/sys/kernel/random/boot_id").read().strip()
    except OSError:
        boot = "nobootid"
    try:
        pid = _run(["display-message", "-p", "#{pid}"]).strip()
    except subprocess.CalledProcessError:
        pid = "0"
    return f"{boot}:{pid}"


def pane_uid(pane: Pane) -> str:
    """Stable per-pane identity: '<boot_id>:<server_pid>:<pane_id>'. The tmux pane_id
    ('%3') is stable for the pane's whole life — across reorder, resize, moving windows,
    and restarting the program INSIDE it — and only recycled after the pane closes, which
    the watcher marks with pane_removed/created events. So this uid identifies one pane
    instance for real-world telemetry grouping ('which panes are active')."""
    return f"{server_uid()}:{pane.id}"


def server_running() -> bool:
    """True if a tmux server is up (avoids noisy errors when nothing is running)."""
    try:
        _run(["list-sessions"])
        return True
    except subprocess.CalledProcessError:
        return False


def prefix_key() -> str:
    """The tmux prefix key (e.g. 'C-a'), auto-detected so the phone's prefix button
    matches the user's config instead of assuming the C-b default. Falls back to C-b."""
    try:
        out = _run(["show-options", "-g", "prefix"]).strip()  # e.g. "prefix C-a"
        parts = out.split()
        return parts[1] if len(parts) >= 2 and parts[1] != "None" else "C-b"
    except subprocess.CalledProcessError:
        return "C-b"


def active_pane_id() -> str | None:
    """The pane id tmux currently has focused (active window's active pane), so the
    phone can default its selection to the pane the user is actually on."""
    try:
        out = _run(["display-message", "-p", "#{pane_id}"]).strip()
        return out or None
    except subprocess.CalledProcessError:
        return None


def list_panes() -> list[Pane]:
    """All panes across all sessions/windows."""
    out = _run(["list-panes", "-a", "-F", _PANE_FMT])
    panes: list[Pane] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 9:
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


def reorder_pane(src_id: str, dst_id: str, after: bool) -> None:
    """Persist a dock drag-reorder by moving src's WINDOW before/after dst's window.

    The dock flattens session→window→pane in list-panes order, and the drag maps to
    reordering that flat strip — so we reorder at the WINDOW level with `move-window`,
    the unit tmux's own status bar and window-index numbering key off. `-b`/`-a` insert
    src's window immediately before/after dst's window and renumber the session
    sequentially, which is exactly drop-before / drop-after and survives reload +
    reflects to every attached client (real server-side order, not a client shuffle).

    Panes that SHARE a window move together (they're one window) — coherent, since they
    also share one window index in the dock's ordering. Cross-SESSION drag is rejected:
    move-window across sessions would renumber a session the user didn't touch and has
    no meaning on a single flat strip. Same window (a no-op drop, or the two ends of an
    intra-window neighbor) is a harmless no-op. Raises RuntimeError on a cross-session
    target so the caller returns a clear error rather than issuing a surprising move."""
    src = find_pane(src_id)
    dst = find_pane(dst_id)
    if src is None or dst is None:
        raise RuntimeError("pane not found")
    if src.session != dst.session:
        raise RuntimeError("cannot reorder across sessions")
    src_win = f"{src.session}:{src.window_index}"
    dst_win = f"{dst.session}:{dst.window_index}"
    if src.window_index == dst.window_index:
        return  # same window — nothing to move (renumbering it in place is a no-op)
    _run(["move-window", "-a" if after else "-b", "-s", src_win, "-t", dst_win])


# OSC 8 hyperlink: ESC]8;params;URL(BEL|ESC\) LABEL ESC]8;;(BEL|ESC\). Terminals show
# only LABEL; a plain capture (no -e) drops the URL entirely.
_OSC8 = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)(.*?)\x1b\]8;;(?:\x07|\x1b\\)", re.S)
# Everything else escape-shaped, stripped after links are materialized: CSI (colors,
# cursor), other OSC, and single-char escapes.
_ANSI = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")


def _materialize_links(text: str) -> str:
    """Turn OSC 8 hyperlinks into markdown — '[label](url)' — so the URL terminals
    embed invisibly survives in plain text. Markdown because BOTH consumers know it:
    the LLM parser reads it natively, and the web linkifier collapses it back to a
    label-only anchor (terminal-like rendering, URL hidden). A label that already IS
    the url passes through bare."""

    def repl(m: re.Match) -> str:
        url, label = m.group(1), m.group(2)
        # Bare-URL labels pass through as-is (the linkifier catches them); anything
        # else keeps its href via markdown — including labels that merely CONTAIN a URL.
        return label if not url or label.strip() == url else f"[{label}]({url})"

    return _OSC8.sub(repl, text)


# SGR (style) sequences, inspected before _ANSI erases them. Agent TUIs render UNSENT
# text — the input-box draft after ❯, queued messages, ghost autocomplete/history
# suggestions — plus hints/borders/chrome distinguishably only by COLOR: faint (SGR 2)
# or a grayscale foreground (observed live in Claude Code: 38;5;239/244/246 chrome and
# borders, SGR 2 ghost suggestions, vs default/near-white 38;5;231 for real content).
# Stripping colors blinds the parser LLM to that difference, and it reports drafts as
# executed work. So for the LLM payload, dim runs are wrapped in ⟪dim⟫…⟪/dim⟫ markers
# (glyphs chosen for near-zero collision with pane text) that survive the ANSI strip.
_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
DIM_OPEN, DIM_CLOSE = "⟪dim⟫", "⟪/dim⟫"


def strip_dim(text: str) -> str:
    """Collapse a dim-marked capture back to the plain text the phone renders."""
    return text.replace(DIM_OPEN, "").replace(DIM_CLOSE, "")


def _mark_dim(text: str) -> str:
    """Wrap faint/gray runs in DIM markers: a state machine over SGR codes, leaving
    every escape byte in place for _ANSI to strip afterwards."""
    out: list[str] = []
    faint = gray = marked = False
    pos = 0

    def emit(chunk: str) -> None:
        nonlocal marked
        # Flip the marker only at inked text — whitespace-only chunks inherit the
        # open state, so adjacent dim runs merge instead of spraying empty pairs.
        if (faint or gray) != marked and chunk.strip():
            out.append(DIM_CLOSE if marked else DIM_OPEN)
            marked = not marked
        out.append(chunk)

    for m in _SGR.finditer(text):
        emit(text[pos : m.start()])
        pos = m.end()
        codes = [int(c or 0) for c in m.group(1).replace(":", ";").split(";")]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                faint = gray = False
            elif c == 2:
                faint = True
            elif c == 22:
                faint = False
            elif c in (38, 48, 58):  # extended fg/bg/underline: ;5;N or ;2;R;G;B —
                # consume the args so they can't be misread as style codes
                is256 = codes[i + 1 : i + 2] == [5]
                if c == 38:
                    n = codes[i + 2] if is256 and len(codes) > i + 2 else -1
                    gray = 232 <= n <= 250  # xterm grayscale ramp, up to mid-gray
                i += 2 if is256 else 4
            elif c == 39 or 30 <= c <= 37 or 91 <= c <= 97:
                gray = False  # default/basic colors replace a gray fg
            elif c == 90:
                gray = True  # bright black IS gray
            i += 1
    emit(text[pos:])
    s = "".join(out)
    if marked:
        # Close after the last ink, not after trailing whitespace/newlines — a marker
        # at the very end would shield them from capture_pane's trailing-\n rstrip.
        body = s.rstrip()
        s = body + DIM_CLOSE + s[len(body) :]
    return s


def capture_pane(
    pane_id: str, lines: int = 200, mark_dim: bool = False, keep_colors: bool = False
) -> str:
    """Current visible text of a pane, most recent `lines` rows. `-p` prints to
    stdout, `-J` joins wrapped lines so long lines aren't split mid-word, `-e` keeps
    escape sequences so OSC 8 hyperlinks can be materialized before the rest are
    stripped. Two independent, orthogonal flags on that stripped-by-default output:

      - `mark_dim` (LLM-bound captures): re-encode faint/gray runs as ⟪dim⟫ markers
        before the strip, so the parser can tell drafts/suggestions from real output.
      - `keep_colors` (live view): skip the strip entirely and return the raw SGR
        runs, which the client renders as colored spans (docs/design/live-view.md).

    The live path keeps colors and does NOT mark dim (the client renders color itself);
    the parser path marks dim and strips. Phone-facing snapshots use neither."""
    out = _materialize_links(
        _run(["capture-pane", "-p", "-J", "-e", "-t", pane_id, "-S", f"-{lines}"])
    )
    if keep_colors:
        return out.rstrip("\n")  # live view: raw SGR, client colorizes
    if mark_dim:
        out = _mark_dim(out)
    return _ANSI.sub("", out).rstrip("\n")


def send_keys(
    pane_id: str, keys: str, enter: bool = True, literal: bool = True
) -> None:
    """Send `keys` to a pane. When `literal` (default), text is sent with `-l` so it
    isn't interpreted as tmux key names — for typed answers. When not literal, `keys`
    is a tmux key-name like "Escape", "Up", or "C-c", sent as that key. `enter` appends
    a Return (only meaningful for literal text)."""
    _run(["send-keys", "-t", pane_id, *(["-l"] if literal else []), keys])
    if enter and literal:
        _run(["send-keys", "-t", pane_id, "Enter"])


_clip_procs: list[subprocess.Popen] = []  # live clipboard holders awaiting reaping
_clip_lock = threading.Lock()  # uploads run in worker threads; don't race the list


def session_locked() -> bool:
    """True when the desktop session is locked. Clipboard paste needs the pane's app
    to READ the clipboard, and GNOME's focus-security model blocks reads from
    unfocused clients while locked — so a locked session means the Ctrl-V will
    silently paste nothing, and the caller should deliver by typed path instead.
    Unknown/ambiguous states report unlocked (clipboard-first stays the default)."""
    try:
        out = _run_host(["loginctl", "--no-pager", "list-sessions", "--no-legend"])
        for line in out.splitlines():
            cols = line.split()
            # Only OUR graphical session: the GDM greeter is also wayland (uid gdm),
            # and inspecting it would misreport the lock state.
            if len(cols) < 2 or cols[1] != str(os.getuid()):
                continue
            props = _run_host(
                ["loginctl", "--no-pager", "show-session", cols[0],
                 "-p", "Type", "-p", "LockedHint"]
            )
            if "Type=wayland" in props or "Type=x11" in props:
                return "LockedHint=yes" in props
    except Exception:  # noqa: BLE001 - no loginctl / no seat: assume unlocked
        pass
    return False


def _run_host(cmd: list[str]) -> str:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=2, check=True
    ).stdout


def set_clipboard_image(png: bytes) -> list[str]:
    """Put PNG bytes on the system clipboard so the pane's app embeds them on Ctrl-V.
    ALWAYS image/png — paste handlers ask the clipboard for PNG, so an offer in the
    upload's own mime (a phone JPEG, say) reads as empty and the paste silently
    no-ops; that was the original phone-attach bug. Writes to both Wayland (wl-copy)
    and X11 (xclip) since which one the terminal reads depends on the session.
    Returns the tools that succeeded — empty means the caller must fall back."""
    ok: list[str] = []
    # Reap earlier clipboard holders that have since exited (replaced offers) —
    # without this each paste could strand a zombie in our process table.
    with _clip_lock:
        _clip_procs[:] = [p for p in _clip_procs if p.poll() is None]
    env_x = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")}
    attempts = [
        ("wl-copy", ["wl-copy", "-t", "image/png"]),
        ("xclip", ["xclip", "-selection", "clipboard", "-t", "image/png"]),
    ]
    for name, cmd in attempts:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            # These tools serve the clipboard selection: wl-copy stays in the
            # foreground for as long as it owns the offer; xclip forks and its parent
            # exits 0. Feed stdin, then wait BRIEFLY — an instant non-zero exit is the
            # "no display/session" failure mode, and counting it as success would
            # resurrect the silent no-op paste. Timeout = still serving = success.
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env_x if name == "xclip" else None,
            )
            proc.stdin.write(png)
            proc.stdin.close()
            try:
                if proc.wait(timeout=0.15) != 0:
                    continue
            except subprocess.TimeoutExpired:
                with _clip_lock:  # alive, owning the clipboard — reap next call
                    _clip_procs.append(proc)
            ok.append(name)
        except Exception:  # noqa: BLE001 - try the next tool
            continue
    return ok


