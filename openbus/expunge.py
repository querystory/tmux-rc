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
from .classify import _codex_model_segments, _session_chrome

# Claude's session ids are uuid4s and Codex's thread ids uuid7s. Nothing else is accepted,
# so an id can never carry a path separator or a glob character into the patterns below.
_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
# The status bar cut to a phone's width: "<thread> · GPT…  ⚠ 2 warnings · f2 to view".
_CLIPPED_BAR = re.compile(rf"\s+{_ID.pattern}(?: · [^·]+)+")
# The state Codex puts before a pane's title while it needs you: "[ ! ] Action Required | ".
_CODEX_STATE = re.compile(r"^\[ ! \] Action Required \| ")
_ENTRY = re.compile(r"[A-Za-z0-9_-]{1,128}")  # any agent-history entry id (agent-history validID)

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
_LINES = {"claude": {"history.jsonl": "sessionId"},
          "codex": {"history.jsonl": "session_id", "session_index.jsonl": "id"}}


# One expunge at a time: each rewrites shared logs by read, filter and replace, and two at
# once would each restore the lines the other removed.
_lock = threading.Lock()


def codex_status_segments(text: str) -> list[str]:
    """The segments, in order, of the Codex status line the parser validates as live
    chrome. One is the thread id when the status line is configured with `session-id`.
    At phone width the bar is cut short (its model to "GPT…", or crowded by a warnings
    segment) and no longer reads as chrome, but in a Codex pane its bottom row, indented,
    a thread and `·`-separated segments, is the bar; a bare id or unindented line there is
    output (`ps` listing `codex resume <id>`). Callers check Codex is running."""
    lines = ([line for line in _session_chrome(text) if _codex_model_segments(line)]
             or [line for line in tmux.strip_dim(text).rstrip().splitlines()[-1:]
                 if _CLIPPED_BAR.fullmatch(line)])
    return [s.strip() for s in lines[-1].split("·")] if lines else []


def codex_status_line(home: Path) -> list:
    """The items Codex's config under `home` puts on its status line, in order."""
    try:
        return tomllib.loads((home / "config.toml").read_text()).get("tui", {}).get(
            "status_line", [])
    except (OSError, ValueError):
        return []


def codex_status_item(shown: list, screen: str, item: str) -> str:
    """The segment at `item`'s place in the configured order (`shown`), "" for none. Codex
    leaves out an empty item (no branch outside a repo), so that place is certain only when
    every item shows, or when it comes first; anything else is ""."""
    if item not in shown:
        return ""
    segments, at = codex_status_segments(screen), shown.index(item)
    return segments[at] if segments and (len(segments) == len(shown) or at == 0) else ""


def codex_thread_shown(shown: list, screen: str) -> str | None:
    """The thread id on the status line, from the segment at session-id's place: never
    just any UUID-shaped one, which a branch can be."""
    sid = codex_status_item(shown, screen, "session-id")
    return sid if _ID.fullmatch(sid) else None


def codex_thread(home: Path, title: str, shown: list, screen: str) -> str | None:
    """The one thread a Codex pane on `home` shows: its id on the status line (at
    session-id's place), else the thread whose name the pane shows. Codex titles the pane
    "<thread name> | <project>", after "[ ! ] Action Required | " while it needs you, and a
    status bar configured with `thread-title` shows the name at that item's place (never
    another segment: a branch can be "main" too). Names come from Codex's index
    (session_index.jsonl, beside the sessions dir: the last line for an id wins, a rename).
    Every reading of the title counts, and the pane names a thread only when exactly one
    thread there has any of them: two threads can share a name, and a name can look like a
    state and another name."""
    if sid := codex_thread_shown(shown, screen):
        return sid
    head = title.rpartition(" | ")[0]
    shows = {head, _CODEX_STATE.sub("", head), codex_status_item(shown, screen, "thread-title")}
    try:
        with open(home / "session_index.jsonl") as index:
            names = {r.get("id"): r.get("thread_name") for r in map(json.loads, index)}
    except (OSError, ValueError, AttributeError):
        return None
    named = [t for t, n in names.items() if n and n in shows]
    return named[0] if len(named) == 1 else None


class Refused(Exception):  # noqa: N818 - a refusal, not an error: nothing was touched
    """Why this pane's session can't be expunged. A fixed sentence, never a path: it is
    audited."""


class Empty(Refused):
    """The pane has no saved conversation to expunge: a sign-in window, or an agent that
    has not started one. Not a fault, so the client offers to close the window instead."""


@dataclass(frozen=True)
class Session:
    harness: str
    session_id: str
    root: Path  # the harness's config dir, as the agent's own environment set it
    index: Path  # agent-history's entries for the harness, likewise (agent-history/README.md)
    pid: int
    start: str  # kernel start time: tells the agent apart from a later reuse of its pid


def _home(pid: int, var: str, default: str) -> Path:
    """The dir the agent's environment names in `var`, else `default` under the agent's
    own HOME, resolved against the agent's own cwd: never the daemon's."""
    env = dict(kv.split("=", 1) for kv in tmux.proc_read(pid, "environ").split("\0") if "=" in kv)
    value = Path(env.get(var) or Path(env.get("HOME") or Path.home(), default))
    if value.is_absolute():
        return value
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"), value)
    except OSError as e:
        raise Refused("the agent's working directory can't be read") from e


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


def _rollout_since(home: Path, start: str) -> bool:
    """Whether any Codex rollout under `home` was written since a process with kernel start
    time `start` (clock ticks after boot) began. Unknown counts as yes."""
    if not start:
        return True
    began = tmux.started_at(start)
    return any(p.stat().st_mtime >= began for p in home.glob("sessions/*/*/*/rollout-*.jsonl"))


def _agent(pid: int, screen: str, title: str) -> Session | None:
    """The Claude or Codex session this process is, if it is one."""
    start = "".join(tmux.proc_stat(pid)[19:20])
    home = _home(pid, "CLAUDE_CONFIG_DIR", ".claude")
    # Claude Code registers each running session as sessions/<pid>.json. A file left by an
    # earlier process with this pid has a different start time.
    reg = _registration(home / "sessions" / f"{pid}.json")
    if reg.get("pid") == pid and start and reg.get("procStart") == start:
        return Session("claude", str(reg.get("sessionId")), home, _index(pid, "claude"), pid,
                       start)
    # Codex's sign-in, `codex login` (accounts.LOGIN), is no session: the search goes on
    # past it. Under npm, argv starts node and the wrapper script before the subcommand.
    if tmux.proc_read(pid, "comm").strip() != "codex":
        return None
    args = [a for a in tmux.proc_read(pid, "cmdline").split("\0")[1:] if not a.endswith("/codex")]
    if args[:1] == ["login"]:
        return None
    # Codex keeps no registry, and its shared app-server, not this client, holds the
    # rollout open: the pane names the thread, by its id on the status line, else by its
    # name in the pane's title or on the status line (codex_thread, as live._codex_pane).
    home = _home(pid, "CODEX_HOME", ".codex")
    if not (sid := codex_thread(home, title, codex_status_line(home), screen)):
        # A fresh Codex (not a resume) that has written no rollout since it started has no
        # thread to name: its first turn creates one.
        if not {"resume", "fork"} & set(args) and not _rollout_since(home, start):
            raise Empty("this agent has not saved a conversation yet")
        raise Refused("can't tell which Codex conversation this pane is running: neither "
                      "its title nor Codex's status line names it. Adding \"session-id\" "
                      "to status_line under [tui] in Codex's config.toml shows its id there.")
    return Session("codex", sid, home, _index(pid, "codex"), pid, start)


def identify(pane_pid: str, screen: str, expected: str | None = None,
             title: str = "") -> Session:
    """The one agent session running in the pane (`screen` and `title` are its own), and
    `expected` if given (the session a confirmation named). The search stops at the first
    agent down each branch, so an agent's own subprocesses never count as a second
    session."""
    found, fresh, todo = [], [], [int(pane_pid)]
    while todo:
        pid = todo.pop()
        try:
            agent = _agent(pid, screen, title)
        except Empty as e:  # still an agent: the walk goes on, and it counts toward "more than one"
            fresh.append(e)
            continue
        if agent:
            found.append(agent)
        else:
            todo += _children(pid)
    if len(found) + len(fresh) > 1:
        raise Refused("more than one agent session is running in this pane")
    if fresh:
        raise fresh[0]
    if not found:
        raise Empty("no Claude Code or Codex session is running in this pane")
    if not _ID.fullmatch(found[0].session_id):
        raise Refused("the session id is not one this daemon recognizes")
    if expected not in (None, found[0].session_id):
        raise Refused("this pane is running a different session now")
    # Each harness writes its transcript (the first two of _FILES) only once there is a turn;
    # agent-history's entry outlives the transcript's retention, so it counts as saved too.
    s = found[0]
    if not (any(True for g in _FILES[s.harness][:2] for _ in s.root.glob(g.format(id=s.session_id)))
            or (s.index / f"{s.session_id}.md").exists() or (s.index / s.session_id).is_dir()):
        raise Empty("this agent has not saved a conversation yet")
    return s


def targets(s: Session) -> tuple[list[Path], list[Path]]:
    """(the session's own files and dirs, the shared files to filter), all checked to
    resolve inside their root. Raises Refused, before anything is deleted, if one doesn't."""
    found = [(s.root, p) for g in _FILES[s.harness] for p in s.root.glob(g.format(id=s.session_id))]
    reg = s.root / "sessions" / f"{s.pid}.json"  # normally gone once Claude exits
    if s.harness == "claude" and _registration(reg).get("sessionId") == s.session_id:
        found.append((s.root, reg))
    # agent-history's copy of what was typed goes last, after the transcript it is rebuilt
    # from: the entry, and the dir of its subagents' entries. A subagent's own subagents sit
    # in a dir named for it (agent-history/index.go), so the walk follows each one down.
    found += [(s.index, p) for p in s.index.glob(f"{s.session_id}.md")]
    todo, seen = [s.session_id], set()
    while todo:
        if (sid := todo.pop()) not in seen and (s.index / sid).is_dir():
            seen.add(sid)
            found.append((s.index, s.index / sid))
            todo += [p.stem for p in (s.index / sid).glob("*.md") if _ENTRY.fullmatch(p.stem)]
    shared = [(s.root, s.root / name) for name in _LINES[s.harness]]
    for root, path in found + shared:
        if not os.path.realpath(path).startswith(os.path.realpath(root) + os.sep):
            raise Refused("a session file resolves outside its config dir")
    # Unlinking a symlink would report its target deleted while the target stays.
    if any(p.is_symlink() for _, p in found):
        raise Refused("a session file is a symlink, not the file itself")
    return [p for _, p in found], [p for _, p in shared if p.exists()]


def alive(s: Session) -> bool:
    """Whether the identified agent itself (pid and start time; a zombie is gone) still runs."""
    st = tmux.proc_stat(s.pid)
    return st[19:20] == [s.start] and st[0] != "Z"


def wait_gone(s: Session, timeout: float = 5.0) -> bool:
    """Whether the agent exited within `timeout` of its window closing."""
    deadline = time.monotonic() + timeout
    while alive(s):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def _drop_lines(path: Path, key: str, sid: str) -> int:
    """Rewrite a JSONL log without the lines whose `key` is `sid`, streamed and atomic,
    keeping its mode. The harnesses append without a lock, so whatever lands in the old
    file while the new one is written, or as it is renamed into place, is carried over."""
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
    def copy(src, dst) -> int:  # the lines not ours, src to dst; how many were ours
        removed = 0
        for line in src:
            if ours(line):
                removed += 1
            else:
                dst.write(line)
        return removed
    with path.open("rb") as old:
        # A fresh, exclusive temp file: a fixed name could be a planted symlink.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".expunge")
        try:
            with os.fdopen(fd, "wb") as f:
                removed = copy(old, f)
            if not removed:
                return 0
            shutil.copymode(path, tmp)
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
        with path.open("ab") as new:  # appended to the old file since it was read
            return removed + copy(old, new)


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
