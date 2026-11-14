"""Move a conversation to the other agent, Claude Code <-> Codex, as one continuous history.

Claude Code -> Codex runs Codex's own importer (its `/import`) headless, through a short-lived
`codex app-server`. Codex -> Claude Code writes a Claude transcript by the rules that importer
follows, so both directions carry the same kind of history: messages verbatim, each tool call
and result as a text note (cut to NOTE_CHARS), reasoning dropped (signed or encrypted, it reads
back only on its own vendor). See docs/design/agent-accounts.md, "Move to another agent".
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .accounts import _whole_records, confined, files, store_path
from .expunge import Session

NOTE_CHARS = 4000  # a tool note's cut, as Codex's importer cuts a tool result
BUDGET = 400_000  # characters a written Claude transcript keeps: about 100k tokens
TURN = BUDGET // 4  # and any one turn: the first ask and the newest turn always fit
IMPORT_S = 60.0  # how long Codex's importer may take
PROJECT_NAME = 200  # the longest project folder name Claude Code spells out whole
TOOL = {"function_call": "arguments", "custom_tool_call": "input"}  # a Codex call's input field


def move(s: Session, to: str, target: Path, cwd: str, agent: str, path: str | None,
         busy: Callable[[str], bool]) -> str:
    """Carry session `s` to agent `to` on the account at `target`; the new session's id.
    `agent` is the target's executable, run on `path` (the tmux session's PATH); `busy`
    says whether a thread id is running on the target (an earlier move's, appended to)."""
    logs = sorted((p for p in files(s, s.root) if p.suffix == ".jsonl" and p.is_file()),
                  key=lambda p: p.name)  # a Codex thread's rollout, then its later segments
    if not logs:
        raise LookupError("this session has no transcript to move yet")
    if to == "codex":
        return to_codex(logs[0], agent, target, cwd, path, busy)
    return to_claude(logs, target, cwd)


def to_codex(transcript: Path, codex: str, home: Path, cwd: str, path: str | None,
             busy: Callable[[str], bool]) -> str:
    """Import a Claude transcript into the Codex account at `home` with Codex's importer; the
    new thread's id. The importer reads only $HOME/.claude/projects and resolves links first,
    so the app-server runs with HOME on a staging dir holding a copy. The copy's path is the
    same every time for one source account (a copied session keeps its id on another), which
    Codex's import ledger keys on: a session moved again appends its new turns to the same
    thread, so that thread must not be running. Its environment is just these three: never
    the daemon's keys. Only the copy is removed, before and after: nothing else is ever
    deleted there."""
    account = hashlib.sha256(str(transcript.parents[2].resolve()).encode()).hexdigest()[:16]
    stage = store_path().parent / "codex-import" / account
    copy = stage / ".claude/projects" / transcript.parent.name / transcript.name
    if any(busy(t) for t in _imported(home, str(copy))):
        raise LookupError("it is running on that account, or may be: go to that window")
    confined([copy], stage)  # no link out of the staging dir, checked before anything is made
    copy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Moves run one at a time (server._MOVES), so a copy already here is a crashed move's.
    copy.unlink(missing_ok=True)
    fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    env = {"HOME": str(stage), "CODEX_HOME": str(home), "PATH": path or os.defpath}
    items = [{"itemType": "SESSIONS", "description": "tmux-rc move", "cwd": None,
              "details": {"sessions": [{"path": str(copy), "cwd": cwd, "title": None}]}}]
    try:
        with os.fdopen(fd, "wb") as f, transcript.open("rb") as src:
            shutil.copyfileobj(src, f)
        _whole_records(copy)  # the agent may be mid-append
        with subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env=env, cwd=stage, text=True,
                              start_new_session=True) as p:
            def kill() -> None:  # the whole group: a wrapper's child may hold stdout open
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)
            timer = threading.Timer(IMPORT_S, kill)  # ends the read below with EOF
            timer.start()
            try:
                for msg in ({"id": 0, "method": "initialize",
                             "params": {"clientInfo": {"name": "tmux-rc", "version": "0"}}},
                            {"method": "initialized"},
                            {"id": 1, "method": "externalAgentConfig/import",
                             "params": {"migrationItems": items}}):
                    p.stdin.write(json.dumps(msg) + "\n")
                p.stdin.flush()
                for line in p.stdout:
                    m = json.loads(line)
                    if "error" in m:
                        raise LookupError("Codex's importer refused it")  # not its words: a path
                    if m.get("method") == "externalAgentConfig/import/completed":
                        done = [d["target"] for r in m["params"]["itemTypeResults"]
                                for d in r["successes"]] or _imported(home, str(copy))
                        if not done:
                            raise LookupError("Codex's importer found nothing to import in it")
                        return done[-1]
            finally:
                timer.cancel()
                kill()
        raise TimeoutError("Codex's importer did not finish")
    finally:
        copy.unlink(missing_ok=True)


def _imported(home: Path, source: str) -> list[str]:
    """The threads Codex's import ledger holds for `source`: a transcript unchanged since its
    last import imports nothing, and the completion then names no thread."""
    try:
        records = json.loads((home / "external_agent_session_imports.json").read_text())["records"]
        return [r["imported_thread_id"] for r in records if r.get("source_path") == source]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _text(content) -> str:
    """A message's text: a string, or the text of its parts, an image's place marked."""
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text") or ("[An image was here; it did not carry over.]"
                                       if "image" in str(c.get("type")) else "")
                     for c in content if isinstance(c, dict)) if isinstance(content, list) else ""


def _note(kind: str, body: str, name: str = "") -> str:
    """A tool call or result as text, in Codex's importer's own markup."""
    head = f"{kind}: {name}" if name else kind
    return f"[external_agent_tool_{head}]\n{body[:NOTE_CHARS]}\n[/external_agent_tool_{kind}]"


def codex_turns(rollouts: list[Path]) -> list[list[str]]:
    """[role, text] per turn of a Codex thread, a role's consecutive items joined. The person's
    messages come from the events (the user-role items also carry Codex's injected context);
    the rest from the response items, minus reasoning and other agents' messages."""
    turns: list[tuple[str, list[str]]] = []

    def add(role: str, text) -> None:
        if not text:
            return
        if not turns or turns[-1][0] != role:
            turns.append((role, []))
        turns[-1][1].append(text)

    for log in rollouts:
        with log.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                p = d.get("payload") if isinstance(d, dict) else None
                if not isinstance(p, dict):
                    continue
                item = p["item"] if isinstance(p.get("item"), dict) else p
                kind, t = d.get("type"), item.get("type")
                if kind == "event_msg" and t in ("UserMessage", "user_message"):
                    add("user", _text(item.get("content")) or _text(item.get("message")))
                elif kind != "response_item":
                    continue
                elif t == "message" and p.get("role") == "assistant":
                    add("assistant", _text(p.get("content")))
                elif t in TOOL:
                    add("assistant", _note("call", _text(p.get(TOOL[t])), str(p.get("name"))))
                elif t in ("function_call_output", "custom_tool_call_output"):
                    add("assistant", _note("result", _text(p.get("output"))))
    return [[role, "\n\n".join(parts)] for role, parts in turns]


def budget(turns: list[list[str]], limit: int = BUDGET) -> list[list[str]]:
    """The first turn (the person's first ask) and the newest that fit in `limit` characters,
    with a note where turns were left out; a turn over a quarter of it keeps its start."""
    cap = limit // 4
    turns = [[r, t if len(t) <= cap else t[:cap] + "\n[The rest of this turn was cut to fit.]"]
             for r, t in turns]
    kept, size = [], len(turns[0][1]) if turns else 0
    for turn in reversed(turns[1:]):
        size += len(turn[1])
        if size > limit:
            kept.append(["assistant", "[Earlier turns were left out of this move to fit.]"])
            break
        kept.append(turn)
    return turns[:1] + kept[::-1]


def to_claude(rollouts: list[Path], home: Path, cwd: str) -> str:
    """Write a Codex thread as a new Claude transcript under the account at `home`, in the
    project dir Claude Code keeps for `cwd`; the new session's id."""
    turns = budget(codex_turns(rollouts))
    if not turns:
        raise LookupError("this session has no messages to move yet")
    sid, parent, now = str(uuid.uuid4()), None, datetime.now(UTC).isoformat()
    out = home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{sid}.jsonl"
    if len(out.parent.name) > PROJECT_NAME:  # Claude Code shortens these its own way
        raise LookupError("this folder's path is too long to write a Claude Code session for")
    confined([out], home)
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rows = []
    for role, text in turns:  # the person's, marked as such for agent-history and resume
        rows.append({"type": role, "uuid": (u := str(uuid.uuid4())), "parentUuid": parent,
                     **({"origin": {"kind": "human"}} if role == "user" else {}),
                     "sessionId": sid, "cwd": cwd, "isSidechain": False, "userType": "external",
                     "timestamp": now, "message": {"role": role, "content": text if role == "user"
                                                   else [{"type": "text", "text": text}]}})
        parent = u
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
    except OSError:
        out.unlink()  # this move's own file: a half transcript must not stay
        raise
    return sid
