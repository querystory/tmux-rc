"""The last message a Claude or Codex agent wrote, read from its own transcript.

The screen is a lossy rendering of that message: long lines wrap, the top scrolls away,
and a model asked to copy a 40-line script off it reflows it rather than quoting it. The
transcript has the exact text, so code the agent hands the user is lifted from here.
Anything unreadable answers None and the caller falls back to the screen."""

import json
import os
import re
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


def _entries(path: Path | None) -> list[dict]:
    if path is None:
        return []
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


def _claude_file(pane: Pane) -> Path | None:
    """Claude registers each running process in sessions/<pid>.json; the one running
    under this pane names the session, whose transcript lives under projects/."""
    home = _home("CLAUDE_CONFIG_DIR", ".claude")
    for reg in (home / "sessions").glob("*.json"):
        try:
            data = json.loads(reg.read_text(encoding="utf-8"))
            pid, sid = int(data["pid"]), str(data["sessionId"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if pane.pid and int(pane.pid) in tmux.ancestors(pid) and _UUID_RE.fullmatch(sid):
            return next((home / "projects").glob(f"*/{sid}.jsonl"), None)
    return None


def _codex_file(text: str) -> Path | None:
    """Codex's app-server, not the pane's process, holds the rollout, so the thread id
    comes from the status bar (when configured to show it), as live._codex_pane does."""
    ids = {s.strip() for line in _session_chrome(text) if _codex_model_segments(line)
           for s in line.split("·") if _UUID_RE.fullmatch(s.strip())}
    if len(ids) != 1:
        return None
    home = _home("CODEX_HOME", ".codex")
    return next((home / "sessions").glob(f"*/*/*/rollout-*-{ids.pop()}.jsonl"), None)


def _text(blocks, kind: str) -> str | None:
    texts = [b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == kind
             and isinstance(b.get("text"), str)] if isinstance(blocks, list) else []
    return "\n\n".join(texts) or None


def last_reply(pane: Pane, text: str) -> str | None:
    """The agent's latest message in its current turn: a new user message clears it, so
    a reply from an earlier turn is never mistaken for what is on screen now."""
    reply = None
    for e in [*_entries(_claude_file(pane)), *_entries(_codex_file(text))]:
        msg = e.get("message") if not e.get("isSidechain") else None
        content = msg.get("content") if isinstance(msg, dict) else None
        item = (e.get("payload") or {}).get("item") if e.get("type") == "event_msg" else None
        kind = item.get("type") if isinstance(item, dict) else e.get("type")
        if kind == "assistant":
            reply = _text(content, "text") or reply
        elif kind == "AgentMessage":
            reply = _text(item.get("content"), "Text") or reply
        elif kind == "UserMessage" or (
                kind == "user" and (isinstance(content, str) or _text(content, "text"))):
            reply = None
    return reply
