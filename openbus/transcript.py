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
            # A crashed process leaves its file behind and its pid gets reused, so the
            # process must also have started when the registration says (stat field 22).
            started = tmux.proc_read(pid, "stat").rsplit(")", 1)[1].split()[19]
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            continue
        if (pane.pid and int(pane.pid) in tmux.ancestors(pid) and _UUID_RE.fullmatch(sid)
                and str(data.get("procStart", started)) == started):
            return next((home / "projects").glob(f"*/{sid}.jsonl"), None)
    return None


def _codex_file(text: str) -> Path | None:
    """Codex's app-server, not the pane's process, holds the rollout, so the thread id
    comes from the status bar (when configured to show it), as live._codex_pane does."""
    ids = {s.strip() for line in _session_chrome(text) if _codex_model_segments(line)
           for s in line.split("·") if _UUID_RE.fullmatch(s.strip())}
    if len(ids) != 1:
        return None
    # A resumed thread continues in rollout-<start>-<id>_<segment>.jsonl; names sort by
    # date then start time, so the greatest is where the thread writes now.
    home = _home("CODEX_HOME", ".codex")
    return max((home / "sessions").glob(f"*/*/*/rollout-*-{ids.pop()}*.jsonl"), default=None)


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
        elif kind in ("UserMessage", "user_message") or (
                kind == "user" and (isinstance(content, str) or _text(content, "text"))):
            reply = None
    return reply
