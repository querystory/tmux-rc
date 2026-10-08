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
import tomllib
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
    """Why this pane's session can't be expunged. A fixed sentence, never a path: it is
    audited."""


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
    """The dir the agent's environment names in `var`, relative to the agent's own cwd."""
    for kv in tmux.proc_read(pid, "environ").split("\0"):
        if kv.startswith(var + "=") and kv != var + "=":
            value = Path(kv[len(var) + 1:])
            if value.is_absolute():
                return value
            try:
                return Path(os.readlink(f"/proc/{pid}/cwd"), value)
            except OSError as e:
                raise Refused("the agent's working directory can't be read") from e
    return Path.home() / default


def _index(pid: int, harness: str) -> Path:
    return _home(pid, "AGENT_HISTORY_DIR", "agent-history") / "index" / harness


def _children(pid: int) -> list[int]:
    with contextlib.suppress(OSError):
        return [int(c) for t in os.listdir(f"/proc/{pid}/task")
                for c in tmux.proc_read(pid, f"task/{t}/children").split()]
    return []


def _registration(path: Path) -> dict:
    """A Claude registration, or {} if there is none. One that can't be read could be the
    session in question, so it refuses rather than be looked past."""
    try:
        reg = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        reg = None
    # Only a complete registration can be told stale; anything less is unreadable.
    if not (isinstance(reg, dict) and isinstance(reg.get("pid"), int)
            and reg.get("sessionId") and reg.get("procStart")):
        raise Refused("an agent registration in this pane can't be read")
    return reg


def _agent(pid: int, screen: str) -> Session | None:
    """The Claude or Codex session this process is, if it is one."""
    start = "".join(_stat(pid)[19:20])
    home = _home(pid, "CLAUDE_CONFIG_DIR", ".claude")
    # Claude Code registers each running session as sessions/<pid>.json. A file left by an
    # earlier process with this pid has a different start time.
    reg = _registration(home / "sessions" / f"{pid}.json")
    if reg.get("pid") == pid and start and reg.get("procStart") == start:
        return Session("claude", str(reg.get("sessionId")), home, _index(pid, "claude"), pid,
                       start)
    if tmux.proc_read(pid, "comm").strip() != "codex":
        return None
    # Codex keeps no registry, and its shared app-server, not this client, holds the
    # rollout open; the thread id is only on the pane's status line (see live._codex_pane).
    # A UUID-shaped segment counts only if a rollout here is named for it, so another
    # segment that happens to look like one (a branch, say) is never taken for the thread.
    home = _home(pid, "CODEX_HOME", ".codex")
    try:
        tui = tomllib.loads((home / "config.toml").read_text()).get("tui", {})
    except (OSError, ValueError):
        tui = {}
    if "session-id" not in tui.get("status_line", []):
        raise Refused("Codex's status line does not show the thread id "
                      "(add session-id to [tui] status_line)")
    ids = {i for i in codex_status_segments(screen)
           if _ID.fullmatch(i) and any(home.glob(f"sessions/*/*/*/rollout-*-{i}*.jsonl"))}
    if len(ids) != 1:
        raise Refused("Codex's status line does not show exactly one thread id")
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
    reg = s.root / "sessions" / f"{s.pid}.json"  # normally gone once Claude exits
    if s.harness == "claude" and _registration(reg).get("sessionId") == s.session_id:
        found.append((s.root, reg))
    shared = [(s.root, s.root / name) for name in _LINES[s.harness]]
    for root, path in found + shared:
        if not os.path.realpath(path).startswith(os.path.realpath(root) + os.sep):
            raise Refused("a session file resolves outside its config dir")
    # Unlinking a symlink would report its target deleted while the target stays.
    if any(p.is_symlink() for _, p in found):
        raise Refused("a session file is a symlink, not the file itself")
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
    mode. The harnesses append without a lock, so whatever lands in the old file while the
    new one is written, or as it is renamed into place, is carried over to it."""
    path = Path(os.path.realpath(path))
    def ours(line: bytes) -> bool:
        if sid.encode() not in line:
            return False
        try:
            return json.loads(line).get(key) == sid
        except (ValueError, AttributeError):
            # One a crash cut short goes too if it still shows the id as its own key: it is
            # already corrupt, and keeping it could keep this session's words.
            return re.search(rb'"%b"\s*:\s*"%b"' % (key.encode(), sid.encode()), line) is not None
    def kept(data: bytes) -> bytes:
        return b"".join(line for line in data.splitlines(keepends=True) if not ours(line))
    with path.open("rb") as old:
        data = old.read()
        if (keep := kept(data)) == data:
            return 0
        # A fresh, exclusive temp file: a fixed name could be a planted symlink.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".expunge")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(keep)
            shutil.copymode(path, tmp)
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
        late = old.read()  # appended to the old file since the first read
        if late:
            with path.open("ab") as f:
                f.write(kept(late))
    return sum(len(d.splitlines()) - len(kept(d).splitlines()) for d in (data, late))


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
