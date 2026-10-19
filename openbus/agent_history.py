"""Client for agent-history, the sibling Go tool in agent-history/ that indexes past
coding-agent sessions.

Live Mode uses it only when the user asks for past work ("resume the foo feature
session", "where was I on live mode"): it's a routing lookup, never context fed to
an agent. It is optional: without the binary, the Live tools that need it are not
offered at all. The scratch gallery reads the same index to say which session made a
folder (openbus/scratch.py)."""

import functools
import glob
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

# resolve reads a few MB of index and answers in tens of ms; a slow disk shouldn't
# stall a voice turn for long.
_TIMEOUT = 5
logger = logging.getLogger(__name__)

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
    unknown id left out; None if agent-history is unavailable or failed."""
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
    if out.returncode not in (0, 1):  # a failure (an outdated binary), not "every id unknown"
        logger.warning("agent-history get exited %d: %s", out.returncode, out.stderr.strip())
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


@functools.cache  # once per path, not every snapshot: the fix is a rebuild, not a retry
def _warn_unavailable(exe: str | None) -> None:
    logger.warning("agent-history (%s) can't report live sessions, so restore snapshots "
                   "bind no pane to its conversation: the binary is missing or older than "
                   "the daemon; rebuild it from agent-history/ (see its README)",
                   exe or "not installed")


def live() -> dict | None:
    """{"sessions": {id: {pid, harness, ...}}, "unknown": [harness]}: every live session,
    and the harnesses that couldn't tell. None if agent-history is unavailable, which is
    logged once: a binary built before this subcommand existed answered "unknown command"
    with exit 0, and for weeks every restore snapshot silently bound nothing."""
    result = _run("live")
    if result is None:
        _warn_unavailable(binary())
    return result


def _entries():
    """Every index entry's front matter, read straight from the index (its documented
    contract, see agent-history/README.md) rather than via the binary: no `resolve` call
    can list without a query, and reading a few header lines per entry takes milliseconds."""
    root = Path(os.environ.get("AGENT_HISTORY_DIR") or Path.home() / "agent-history")
    for path in (root / "index").glob("*/**/*.md"):
        meta = {}
        try:
            with path.open() as f:
                for n, line in enumerate(f):
                    if n and line.startswith("---"):
                        break  # end of front matter; the body is the human's messages
                    key, sep, value = line.partition(": ")
                    if sep:
                        meta[key] = json.loads(value)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        yield meta


def _when(stamp) -> float:
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return 0.0


def recent_dirs(limit: int = 30) -> list[str]:
    """Directories past agent sessions ran in, most recently active first, that still
    exist. Only top-level entries: a subagent runs in its parent's directory."""
    latest: dict[str, str] = {}
    for meta in _entries():
        cwd = meta.get("cwd")
        if isinstance(cwd, str) and cwd and not meta.get("parent_session"):
            latest[cwd] = max(latest.get(cwd, ""), str(meta.get("last_active", "")))
    dirs = sorted(latest, key=latest.__getitem__, reverse=True)
    return [d for d in dirs if os.path.isdir(d)][:limit]


def _transcripts(e: dict) -> list[str]:
    """Every file of an entry's conversation. A resumed Codex thread continues in a new
    rollout under the same ID, sessions/<y>/<m>/<d>/rollout-<time>-<id>[_<n>].jsonl, and its
    entry's source is only the latest one (agent-history/codex.go)."""
    source, sid = Path(e["source"]), glob.escape(str(e.get("session_id")))
    if e.get("harness") != "codex" or len(source.parents) < 4:
        return [e["source"]]
    return sorted({e["source"], *map(str, source.parents[3].glob(f"*/*/*/*-{sid}*.jsonl"))})


def _root(e: dict, by_id: dict) -> dict:
    """The top-level session a (sub)agent's entry belongs to, as far as the index knows."""
    seen = set()
    while (parent := by_id.get(e.get("parent_session"))) and id(parent) not in seen:
        seen.add(id(parent))
        e = parent
    return e


def writers(pattern: str, due: dict[str, float], slack: float = 600) -> dict:
    """{name: the top-level session that wrote it} for each name in `due` (name -> when it
    appeared): the latest-started session active around that time whose transcript has a
    match of `pattern` (an ERE) ending in "/<name>". Latest, because a long-lived session
    that was merely open then (and passed the link on) started before the one that wrote
    it. One grep reads every transcript active at some name's time, and no other: Python's
    re took seconds per gigabyte."""
    entries = sorted(_entries(), key=lambda e: -_when(e.get("started")))
    by_id = {e.get("session_id"): e for e in entries}
    wanted = {}
    for e in entries:
        start, end = _when(e.get("started")) - slack, _when(e.get("last_active")) + slack
        if isinstance(e.get("source"), str) and (
                names := {n for n, t in due.items() if start <= t <= end}):
            for source in _transcripts(e):
                wanted.setdefault(source, (e, names))
    if not wanted:
        return {}
    try:  # -Z: a NUL after the file name, which may itself hold a colon
        out = subprocess.run(["grep", "-oaHZE", "--", pattern, *wanted], capture_output=True,
                             timeout=60, check=False, env={**os.environ, "LC_ALL": "C"}).stdout
    except (subprocess.SubprocessError, OSError):
        return {}
    hits = set()
    for line in out.splitlines():
        source, _, match = line.partition(b"\0")
        name = unquote(match.rpartition(b"/")[2].decode(errors="replace"))  # a URL's %20
        hits.add((source.decode(errors="replace"), name))
    found: dict[str, dict] = {}
    for source, (e, names) in wanted.items():  # the latest started wins
        for name in names - found.keys():
            if (source, name) in hits:
                found[name] = _root(e, by_id)
    return found
