"""Expunge: end a pane's agent and delete that one session's local files.

Everything is found by the session id, and only the files named for it (or the lines
carrying it in the harness's shared logs) are touched. See docs/design/expunge.md."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import tmux
from .classify import codex_status_segments

# Claude's session ids are uuid4s and Codex's thread ids uuid7s. Nothing else is accepted,
# so an id can never carry a path separator or a glob character into the patterns below.
_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")

# Per harness: the session's own files and dirs, relative to its config dir, and the
# shared JSONL logs whose lines name it (file -> the key holding the id).
_FILES = {
    "claude": ("projects/*/{id}.jsonl", "projects/*/{id}", "file-history/{id}",
               "session-env/{id}", "tasks/{id}", "todos/{id}-*", "debug/{id}.txt"),
    # A rollout is rollout-<time>-<id>[_<segment>].jsonl (agent-history/codex.go).
    "codex": ("sessions/*/*/*/rollout-*-{id}.jsonl", "sessions/*/*/*/rollout-*-{id}_*.jsonl",
              "archived_sessions/rollout-*-{id}.jsonl", "archived_sessions/rollout-*-{id}_*.jsonl",
              "shell_snapshots/{id}.*"),
}
_INDEX = ("{id}.md", "{id}")  # agent-history's entry, and its subagents' entries
_LINES = {"claude": {"history.jsonl": "sessionId"},
          "codex": {"history.jsonl": "session_id", "session_index.jsonl": "id"}}


# One expunge at a time: each rewrites shared logs by read, filter and replace, and two at
# once would each restore the lines the other removed.
_lock = threading.Lock()


class Refused(Exception):  # noqa: N818 - a refusal, not an error: nothing was touched
    """Why this pane's session can't be expunged."""


@dataclass(frozen=True)
class Session:
    harness: str
    session_id: str
    root: Path  # the harness's config dir, as the agent's own environment set it
    index: Path  # agent-history's entries for the harness, likewise (agent-history/README.md)
    pid: int
    start: str  # kernel start time: tells the agent apart from a later reuse of its pid


def _stat(pid: int) -> list[str]:
    """/proc/<pid>/stat fields from the state on (field 3), so [19] is the start time."""
    return tmux.proc_read(pid, "stat").rpartition(")")[2].split()


def _home(pid: int, var: str, default: str) -> Path:
    for kv in tmux.proc_read(pid, "environ").split("\0"):
        if kv.startswith(var + "=") and kv != var + "=":
            return Path(kv[len(var) + 1:])
    return Path.home() / default


def _index(pid: int, harness: str) -> Path:
    return _home(pid, "AGENT_HISTORY_DIR", "agent-history") / "index" / harness


def _children(pid: int) -> list[int]:
    with contextlib.suppress(OSError):
        return [int(c) for t in os.listdir(f"/proc/{pid}/task")
                for c in tmux.proc_read(pid, f"task/{t}/children").split()]
    return []


def _agent(pid: int, screen: str) -> Session | None:
    """The Claude or Codex session this process is, if it is one."""
    start = "".join(_stat(pid)[19:20])
    home = _home(pid, "CLAUDE_CONFIG_DIR", ".claude")
    # Claude Code registers each running session as sessions/<pid>.json. A file left by an
    # earlier process with this pid has a different start time; one that can't be read
    # could be this process's, so it refuses rather than look past it.
    try:
        reg = json.loads((home / "sessions" / f"{pid}.json").read_text())
    except FileNotFoundError:
        reg = {}
    except (OSError, ValueError) as e:
        raise Refused("an agent registration in this pane can't be read") from e
    if not isinstance(reg, dict):
        raise Refused("an agent registration in this pane can't be read")
    if reg and reg.get("pid") == pid and start and reg.get("procStart") == start:
        return Session("claude", str(reg.get("sessionId")), home, _index(pid, "claude"), pid,
                       start)
    if tmux.proc_read(pid, "comm").strip() != "codex":
        return None
    # Codex keeps no registry, and its shared app-server, not this client, holds the
    # rollout open; the thread id is only on the pane's status line (see live._codex_pane).
    # A UUID-shaped segment counts only if a rollout here is named for it, so another
    # segment that happens to look like one (a branch, say) is never taken for the thread.
    home = _home(pid, "CODEX_HOME", ".codex")
    ids = {i for i in codex_status_segments(screen)
           if _ID.fullmatch(i) and any(home.glob(f"sessions/*/*/*/rollout-*-{i}*.jsonl"))}
    if len(ids) != 1:
        raise Refused("Codex's status line does not show this thread's id (add session-id to it)")
    return Session("codex", ids.pop(), home, _index(pid, "codex"), pid, start)


def identify(pane_pid: str, screen: str, expected: str | None = None) -> Session:
    """The one agent session running in the pane, and `expected` if given (the session a
    confirmation named). The search stops at the first agent down each branch, so an
    agent's own subprocesses never count as a second session."""
    found, todo = [], [int(pane_pid)]
    while todo:
        pid = todo.pop()
        if agent := _agent(pid, screen):
            found.append(agent)
        else:
            todo += _children(pid)
    if not found:
        raise Refused("no Claude Code or Codex session is running in this pane")
    if len(found) > 1:
        raise Refused("more than one agent session is running in this pane")
    if not _ID.fullmatch(found[0].session_id):
        raise Refused("the session id is not one this daemon recognizes")
    if expected not in (None, found[0].session_id):
        raise Refused("this pane is running a different session now")
    return found[0]


def targets(s: Session) -> tuple[list[Path], list[Path]]:
    """(the session's own files and dirs, the shared files to filter), all checked to
    resolve inside their root. Raises Refused, before anything is deleted, if one doesn't."""
    # agent-history's copy of what was typed goes last, after the transcript it is rebuilt from.
    found = [(root, p) for root, globs in ((s.root, _FILES[s.harness]), (s.index, _INDEX))
             for g in globs for p in root.glob(g.format(id=s.session_id))]
    shared = [(s.root, s.root / name) for name in _LINES[s.harness]]
    for root, path in found + shared:
        if not os.path.realpath(path).startswith(os.path.realpath(root) + os.sep):
            raise Refused(f"{path.name} resolves outside {root.name}")
    # Unlinking a symlink would report its target deleted while the target stays.
    if links := [p.name for _, p in found if p.is_symlink()]:
        raise Refused(f"{links[0]} is a symlink, not the session's own file")
    return [p for _, p in found], [p for _, p in shared if p.exists()]


def wait_gone(s: Session, timeout: float = 5.0) -> bool:
    """Whether the agent exited (a zombie counts) within `timeout` of its window closing."""
    deadline = time.monotonic() + timeout
    while (st := _stat(s.pid))[19:20] == [s.start] and st[0] != "Z":
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def _drop_lines(path: Path, key: str, sid: str) -> int:
    """Rewrite a JSONL log without the lines whose `key` is `sid`, atomically, keeping its
    mode. The harnesses append to these logs without a lock, so a rewrite that the log
    outgrew while it was being written is thrown away and done again."""
    path = Path(os.path.realpath(path))
    def ours(line: bytes) -> bool:
        # A line that names the id but can't be parsed (one a crash cut short) goes too:
        # it is already corrupt, and keeping it could keep this session's words.
        if sid.encode() not in line:
            return False
        try:
            return json.loads(line).get(key) == sid
        except (ValueError, AttributeError):
            return True
    for _ in range(5):
        data = path.read_bytes()
        lines = data.splitlines(keepends=True)
        keep = [line for line in lines if not ours(line)]
        if len(keep) == len(lines):
            return 0
        # A fresh, exclusive temp file: a fixed name could be a planted symlink.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".expunge")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(b"".join(keep))
            shutil.copymode(path, tmp)
            if path.stat().st_size == len(data):  # nothing was appended meanwhile
                os.replace(tmp, path)
                return len(lines) - len(keep)
        finally:
            Path(tmp).unlink(missing_ok=True)
    raise OSError(f"{path.name} kept growing; left as it was")


def expunge(s: Session) -> dict:
    """Delete everything targets() names. Call only once the agent is gone (wait_gone)."""
    with _lock:
        return _expunge(s)


def _expunge(s: Session) -> dict:
    own, shared = targets(s)
    for path in own:  # never a symlink: targets() refuses those
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    lines = sum(_drop_lines(p, _LINES[s.harness][p.name], s.session_id) for p in shared)
    return {"files": [p.name for p in own], "lines": lines}
