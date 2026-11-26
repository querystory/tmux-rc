"""The last message a Claude or Codex agent wrote, read from its own transcript.

The screen is a lossy rendering of that message: long lines wrap, the top scrolls away,
and a model asked to copy a 40-line script off it reflows it rather than quoting it. The
transcript has the exact text, so code the agent hands the user is lifted from here.
Anything unreadable answers None and the caller falls back to the screen.

The watcher asks on every tick, so a transcript written after the screen settled still
re-parses the card. Each lookup is therefore cached on the stat of what it read, and an
unchanged tick costs a few stat calls."""

import hashlib
import json
import os
import re
import time
from functools import lru_cache
from pathlib import Path

from . import plan_usage, tmux
from .classify import _programs
from .expunge import codex_status_segments
from .tmux import Pane

# The final message sits at the end, but a session's file grows to megabytes (pasted
# images, tool output); reading only its tail keeps this cheap on every parse.
_TAIL = 1 << 20
# Conversation entries: Claude's messages and tool calls, Codex's response items and events
# (all but its token counts).
_SAID = frozenset({"user", "assistant", "response_item", "event_msg"})
_UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def _home(pane: Pane, tool: str) -> Path:
    """The config dir the pane's agent reads: CLAUDE_CONFIG_DIR / CODEX_HOME, else under
    HOME, from the agent's own environment (an inline override sets it only there)."""
    return plan_usage.home(tool, plan_usage.pane_env(pane.pid, tool) if pane.pid else {})


def _stamp(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return 0, 0
    return st.st_mtime_ns, st.st_size


# A pane's reply, turn and model each read the same tail as its file stands: one decode.
@lru_cache(maxsize=8)
def _entries(path: Path, _stat: tuple) -> tuple[dict, ...]:
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
    return tuple(e for e in entries if isinstance(e, dict))


def _started(pid: int) -> str | None:
    """When `pid` started (stat field 22): with the pid, a process's identity."""
    fields = tmux.proc_read(pid, "stat").rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


@lru_cache(maxsize=256)
def _claude_sessions(home: Path, pane_pid: str | None, _registry: tuple) -> tuple:
    """Claude registers each running process in sessions/<pid>.json; those running under
    this pane name its sessions. Answers (pid, start, session id) for each."""
    found = []
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
                and _UUID_RE.fullmatch(sid) and str(data.get("procStart")) == started):
            found.append((pid, started, sid))
    return tuple(found)


@lru_cache(maxsize=256)
def _codex_rollout(home: Path, thread: str, _today: tuple) -> Path | None:
    # A resumed thread continues in rollout-<start>-<id>_<segment>.jsonl, created in
    # today's folder; names sort by date then start time, so the greatest is current.
    return max((home / "sessions").glob(f"*/*/*/rollout-*-{thread}*.jsonl"), default=None)


def _codex_file(pane: Pane, text: str) -> Path | None:
    """Codex's app-server, not the pane's process, holds the rollout, so the thread id
    comes from the status bar (when configured to show it), as live._codex_pane does.
    A shell can print a captured footer, so Codex must also be running in the pane."""
    ids = {s for s in codex_status_segments(text) if _UUID_RE.fullmatch(s)}
    if len(ids) != 1 or not (pane.pid and "codex" in _programs(pane.pid)):
        return None
    home = _home(pane, "codex")
    return _codex_rollout(home, ids.pop(), _stamp(home / "sessions" / time.strftime("%Y/%m/%d")))


def _text(blocks) -> str | None:
    # Claude tags text blocks "text"; Codex has written both "Text" and "text".
    texts = [b["text"] for b in blocks if isinstance(b, dict) and b.get("type") in ("text", "Text")
             and isinstance(b.get("text"), str)] if isinstance(blocks, list) else []
    return "\n\n".join(texts) or None


def _claude_file(pane: Pane) -> Path | None:
    claude = _home(pane, "claude")
    registry = tuple((p.name, _stamp(p)) for p in sorted((claude / "sessions").glob("*.json")))
    # Checked afresh: a process can exit (and its pid be reused) and the transcript
    # appear after its registration, all with the registry unchanged. With several still
    # running under the pane (one suspended behind another), which is on screen is unknown.
    live = [sid for pid, started, sid in _claude_sessions(claude, pane.pid, registry)
            if _started(pid) == started]
    return (len(live) == 1
            and next((claude / "projects").glob(f"*/{live[0]}.jsonl"), None)) or None


def _path(pane: Pane, text: str) -> Path | None:
    return _claude_file(pane) or _codex_file(pane, text)


def last_reply(pane: Pane, text: str) -> str | None:
    """The agent's latest message in its current turn: a new user message clears it, so
    a reply from an earlier turn is never mistaken for what is on screen now."""
    path = _path(pane, text)
    return path and _reply(path, _stamp(path))


def turn(pane: Pane, text: str) -> str | None:
    """Where the agent's conversation stands: its transcript's last message or tool call.
    Only the conversation moving on changes it; bookkeeping (token counts, hook summaries),
    a restart, a resize or a new reading of the same screen leave it be."""
    path = _path(pane, text)
    return path and _turn(path, _stamp(path))


@lru_cache(maxsize=256)
def _turn(path: Path, _stat: tuple) -> str | None:
    said = [e for e in _entries(path, _stat) if e.get("type") in _SAID and not e.get("isSidechain")
            and not (isinstance(p := e.get("payload"), dict) and p.get("type") == "token_count")]
    last = json.dumps(said[-1], sort_keys=True).encode() if said else None
    return last and f"{path.name}:{hashlib.sha256(last).hexdigest()}"


# rollout -> (file identity, bytes read, turn open, model)
_reads: dict[Path, tuple[int, int, bool | None, str | None]] = {}


def _follow(path: Path) -> tuple[bool | None, str | None]:
    """Whether Codex's rollout has a turn open, and the model its latest turn_context
    names. Read incrementally, since one long turn writes megabytes, far past `_TAIL`."""
    try:
        with path.open("rb") as f:
            ino, size = (st := os.fstat(f.fileno())).st_ino, st.st_size
            seen, pos, live, model = _reads.get(path, (ino, 0, None, None))
            if seen != ino or size < pos:  # replaced (a session copied over it): start over
                pos, live, model = 0, None, None
            f.seek(pos)
            for line in f:  # one record at a time: a rollout can reach hundreds of MB
                if not line.endswith(b"\n"):
                    break  # still being written: read it whole next time
                pos += len(line)
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                event = e.get("payload") if isinstance(e, dict) else None
                if not isinstance(event, dict):
                    continue
                live = {"task_started": True, "task_complete": False,
                        "turn_aborted": False}.get(event.get("type"), live)
                if e.get("type") == "turn_context" and isinstance(event.get("model"), str):
                    model = event["model"] or model
    except OSError:
        return None, None
    _reads[path] = (ino, pos, live, model)
    return live, model


def turn_live(pane: Pane, text: str) -> bool | None:
    """Whether Codex's rollout has a turn open (True), closed (False), or cannot say
    (None): open from task_started to task_complete or turn_aborted. Codex hides its
    "Working" row while a message prints mid-turn, so the screen alone can show a live turn
    as over. Claude's transcript is not read for this: a turn it closes can leave background
    agents working, and whether the pane is busy is the model's read of the screen."""
    path = _codex_file(pane, text)
    return _follow(path)[0] if path else None


def model(pane: Pane, text: str) -> str | None:
    """The model the agent last ran, named as its status line names it. Exact where the
    screen's is a reading, and there when a narrow pane cuts the status line short."""
    claude, codex = _claude_file(pane), None
    ran = (_claude_model(claude, _stamp(claude)) if claude
           else _follow(codex)[1] if (codex := _codex_file(pane, text)) else None)
    if not ran or not ran.startswith("claude-"):
        return ran  # Codex shows its model's id as is
    # Claude Code shows claude-opus-5-5 (or a dated claude-haiku-4-5-20251001) as Opus 5.5.
    words: list[str] = []
    for part in ran.split("-")[1:]:
        if part.isdigit() and len(part) == 8:  # a release date, YYYYMMDD
            continue
        if part.isdigit() and words and words[-1][-1].isdigit():
            words[-1] += f".{part}"
        else:
            words.append(part.capitalize())
    return " ".join(words) or None


@lru_cache(maxsize=256)
def _claude_model(path: Path, _stat: tuple) -> str | None:
    """The latest assistant record's model, from the tail `_reply` reads too: a reply is
    rarely a megabyte from the end, and none read falls back to the screen."""
    for e in reversed(_entries(path, _stat)):
        msg = e.get("message")
        ran = msg.get("model") if isinstance(msg, dict) else None
        # Claude stamps messages it wrote itself (an interrupt, an API error)
        # "<synthetic>": no model ran them. A subagent's model is not the pane's.
        if (e.get("type") == "assistant" and not e.get("isSidechain")
                and isinstance(ran, str) and ran and not ran.startswith("<")):
            return ran
    return None


@lru_cache(maxsize=256)
def _reply(path: Path, _stat: tuple) -> str | None:
    reply = None
    for e in _entries(path, _stat):
        msg = e.get("message") if not e.get("isSidechain") else None
        content = msg.get("content") if isinstance(msg, dict) else None
        # Codex has written both {item_completed, item: {type: AgentMessage, ...}} and
        # the older flat {type: agent_message, message: "..."} event shapes.
        event = e.get("payload") if e.get("type") == "event_msg" else None
        event = event if isinstance(event, dict) else {}
        item = event.get("item") if isinstance(event.get("item"), dict) else event
        kind = item.get("type") or e.get("type")
        if kind == "assistant":
            reply = _text(content) or reply
        elif kind == "AgentMessage":
            reply = _text(item.get("content")) or reply
        elif kind == "agent_message" and isinstance(item.get("message"), str):
            reply = item["message"] or reply
        elif kind in ("UserMessage", "user_message") or (kind == "user" and (
                # Only a person's (or a headless run's) prompt starts a turn, as in
                # agent-history; reminders and task notifications are user-role too.
                (isinstance(e.get("origin"), dict) and e["origin"].get("kind") == "human")
                or e.get("promptSource") == "sdk")):
            reply = None
    return reply
