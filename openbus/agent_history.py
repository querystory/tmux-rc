"""Client for agent-history, the sibling Go tool in agent-history/ that indexes past
coding-agent sessions.

Live Mode uses it only when the user asks for past work ("resume the foo feature
session", "where was I on live mode"): it's a routing lookup, never context fed to
an agent. It is optional: without the binary, the Live tools that need it are not
offered at all."""

import json
import os
import shutil
import subprocess
from pathlib import Path

# resolve reads a few MB of index and answers in tens of ms; a slow disk shouldn't
# stall a voice turn for long.
_TIMEOUT = 5

# Harnesses whose resume command the daemon will start. resume_argv comes from the
# index, but it is still data from disk, so argv[0] must be a known agent.
RESUMABLE = frozenset({"claude", "codex", "omp"})


def binary() -> str | None:
    """The agent-history executable: $TMUXRC_AGENT_HISTORY, else PATH, else
    ~/.local/bin (the daemon's unit PATH is minimal)."""
    override = os.environ.get("TMUXRC_AGENT_HISTORY")
    if override:
        return override if os.access(override, os.X_OK) else None
    return shutil.which("agent-history") or shutil.which(
        "agent-history", path=str(Path.home() / ".local" / "bin")
    )


def offered() -> bool:
    """Whether Live offers the history tools, and so whether a call to them may run.
    Not in single-pane mode (TMUXRC_TARGET): the watcher publishes only that pane, so a
    window these tools open or point at could never be typed into."""
    return bool(binary()) and not os.environ.get("TMUXRC_TARGET")


def _run(*args: str) -> dict | None:
    exe = binary()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=_TIMEOUT, check=True
        )
        return json.loads(out.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def resolve(query: str) -> list[dict] | None:
    """Likeliest repos for `query`, each with a few sessions; None if unavailable."""
    # "--" so a query that starts with "-" is never read as a flag.
    result = _run("resolve", "-json", "-projects", "3", "-sessions", "3", "--", query)
    return None if result is None else result.get("projects") or []


def get(session_id: str) -> dict | None:
    """One session by id, with `running` when a process has it open; None if unknown."""
    return _run("get", session_id)


def get_many(session_ids: list[str]) -> dict[str, dict] | None:
    """get for several sessions, sharing one liveness scan: {session_id: entry}, an
    unknown id left out; None if agent-history is unavailable."""
    exe = binary()
    if not exe:
        return None
    if not session_ids:
        return {}
    try:  # exit 1 only means some id was unknown; the rest are still printed
        out = subprocess.run([exe, "get", *session_ids], capture_output=True, text=True,
                             timeout=_TIMEOUT, check=False)
        found = [json.loads(line) for line in out.stdout.splitlines() if line]
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    return {e["session_id"]: e for e in found if isinstance(e, dict) and "session_id" in e}


def reconcile() -> None:
    """Bring the index up to date with every transcript: about a second cold, a fraction
    warm. Without Claude Code's hook installed, nothing else refreshes it for hours, and a
    restore judged on it saw sessions from that morning as days idle, or not at all."""
    exe = binary()
    if exe:
        try:  # a stale index judges worse, but must not fail the offer
            subprocess.run([exe, "reconcile"], capture_output=True, timeout=60, check=False)
        except (subprocess.SubprocessError, OSError):
            pass


def live() -> dict | None:
    """{"sessions": {id: {pid, harness, ...}}, "unknown": [harness]}: every live session,
    and the harnesses that couldn't tell. None if agent-history is unavailable."""
    return _run("live")


def recent_dirs(limit: int = 30) -> list[str]:
    """Directories past agent sessions ran in, most recently active first, that still
    exist. Read straight from the index's front matter (cwd, last_active — its documented
    contract, see agent-history/README.md) rather than via the binary: no `resolve` call
    can list without a query, and reading a few header lines per entry takes milliseconds.
    Only top-level entries: a subagent runs in its parent's directory."""
    root = Path(os.environ.get("AGENT_HISTORY_DIR") or Path.home() / "agent-history")
    latest: dict[str, str] = {}
    for path in (root / "index").glob("*/*.md"):
        meta = {}
        try:
            with path.open() as f:
                for n, line in enumerate(f):
                    if n and line.startswith("---"):
                        break  # end of front matter; the body is the human's messages
                    key, sep, value = line.partition(": ")
                    if key in ("cwd", "last_active") and sep:
                        meta[key] = json.loads(value)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        cwd = meta.get("cwd")
        if isinstance(cwd, str) and cwd:
            latest[cwd] = max(latest.get(cwd, ""), str(meta.get("last_active", "")))
    dirs = sorted(latest, key=latest.__getitem__, reverse=True)
    return [d for d in dirs if os.path.isdir(d)][:limit]
