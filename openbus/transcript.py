"""The last message a Claude or Codex agent wrote, read from its own transcript.

The screen is a lossy rendering of that message: long lines wrap, the top scrolls away,
and a model asked to copy a 40-line script off it reflows it rather than quoting it. The
transcript has the exact text, so code the agent hands the user is lifted from here.
Anything unreadable answers None and the caller falls back to the screen.

The watcher asks on every tick, so a transcript written after the screen settled still
re-parses the card. Each lookup is therefore cached on the stat of what it read, and an
unchanged tick costs a few stat calls."""

import json
import os
import re
import time
from functools import lru_cache
from pathlib import Path

from . import tmux
from .classify import _codex_model_segments, _session_chrome
from .tmux import Pane

# The final message sits at the end, but a session's file grows to megabytes (pasted
# images, tool output); reading only its tail keeps this cheap on every parse.
_TAIL = 1 << 20
_UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def _home(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def _stamp(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return 0, 0
    return st.st_mtime_ns, st.st_size


def _entries(path: Path) -> list[dict]:
    try:
        with path.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - _TAIL))
            lines = f.read().splitlines()[size > _TAIL:]  # a cut-short first line goes
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return [e for e in entries if isinstance(e, dict)]


def _started(pid: int) -> str | None:
    """When `pid` started (stat field 22): with the pid, a process's identity."""
    fields = tmux.proc_read(pid, "stat").rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


@lru_cache(maxsize=256)
def _claude_session(home: Path, pane_pid: str | None, _registry: tuple) -> tuple | None:
    """Claude registers each running process in sessions/<pid>.json; the one running
    under this pane names the session. Answers (pid, start, session id)."""
    for reg in (home / "sessions").glob("*.json"):
        try:
            data = json.loads(reg.read_text(encoding="utf-8"))
            pid, sid = int(data["pid"]), str(data["sessionId"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        # A crashed process leaves its file behind and its pid gets reused, so the
        # process must also have started when the registration says.
        started = _started(pid)
        if (started and pane_pid and int(pane_pid) in tmux.ancestors(pid)
                and _UUID_RE.fullmatch(sid) and str(data.get("procStart", started)) == started):
            return pid, started, sid
    return None


@lru_cache(maxsize=256)
def _codex_rollout(home: Path, thread: str, _today: tuple) -> Path | None:
    # A resumed thread continues in rollout-<start>-<id>_<segment>.jsonl, created in
    # today's folder; names sort by date then start time, so the greatest is current.
    return max((home / "sessions").glob(f"*/*/*/rollout-*-{thread}*.jsonl"), default=None)


def _codex_file(text: str) -> Path | None:
    """Codex's app-server, not the pane's process, holds the rollout, so the thread id
    comes from the status bar (when configured to show it), as live._codex_pane does."""
    ids = {s.strip() for line in _session_chrome(text) if _codex_model_segments(line)
           for s in line.split("·") if _UUID_RE.fullmatch(s.strip())}
    if len(ids) != 1:
        return None
    home = _home("CODEX_HOME", ".codex")
    return _codex_rollout(home, ids.pop(), _stamp(home / "sessions" / time.strftime("%Y/%m/%d")))


def _text(blocks, kind: str) -> str | None:
    texts = [b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == kind
             and isinstance(b.get("text"), str)] if isinstance(blocks, list) else []
    return "\n\n".join(texts) or None


def last_reply(pane: Pane, text: str) -> str | None:
    """The agent's latest message in its current turn: a new user message clears it, so
    a reply from an earlier turn is never mistaken for what is on screen now."""
    claude = _home("CLAUDE_CONFIG_DIR", ".claude")
    registry = tuple((p.name, _stamp(p)) for p in sorted((claude / "sessions").glob("*.json")))
    # Checked afresh: the process can exit (and its pid be reused) and the transcript
    # appear after its registration, all with the registry unchanged.
    pid, started, sid = _claude_session(claude, pane.pid, registry) or (0, None, None)
    path = ((sid and _started(pid) == started
             and next((claude / "projects").glob(f"*/{sid}.jsonl"), None))
            or _codex_file(text))
    return path and _reply(path, _stamp(path))


@lru_cache(maxsize=256)
def _reply(path: Path, _stat: tuple) -> str | None:
    reply = None
    for e in _entries(path):
        msg = e.get("message") if not e.get("isSidechain") else None
        content = msg.get("content") if isinstance(msg, dict) else None
        # Codex has written both {item_completed, item: {type: AgentMessage, ...}} and
        # the older flat {type: agent_message, message: "..."} event shapes.
        event = e.get("payload") if e.get("type") == "event_msg" else None
        event = event if isinstance(event, dict) else {}
        item = event.get("item") if isinstance(event.get("item"), dict) else event
        kind = item.get("type") or e.get("type")
        if kind == "assistant":
            reply = _text(content, "text") or reply
        elif kind == "AgentMessage":
            reply = _text(item.get("content"), "Text") or reply
        elif kind == "agent_message" and isinstance(item.get("message"), str):
            reply = item["message"] or reply
        elif kind in ("UserMessage", "user_message") or (kind == "user" and (
                # Only a person's (or a headless run's) prompt starts a turn, as in
                # agent-history; reminders and task notifications are user-role too.
                (isinstance(e.get("origin"), dict) and e["origin"].get("kind") == "human")
                or e.get("promptSource") == "sdk")):
            reply = None
    return reply
