"""transcript.last_reply: the agent's latest message in its current turn, read from a
Claude or Codex session file located from the pane."""

import json
import os
import time

import pytest

from openbus import transcript
from openbus.tmux import Pane

SID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _write(path, entries, junk=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(junk + "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _registration():
    """The test process, registered as Claude would register it."""
    pid = os.getpid()
    return {"pid": pid, "sessionId": SID, "procStart": transcript._started(pid)}


def _pane(pid):
    return Pane("work", "0", "node", "0", "%0", "node", "t", "/x", pid=pid)


@pytest.fixture
def homes(tmp_path):
    return tmp_path  # conftest points both agent homes under it


def _say(role, content, **extra):
    if role == "user" and isinstance(content, str):
        extra = {"origin": {"kind": "human"}, **extra}  # a person typed it
    return {"type": role, "message": {"role": role, "content": content}, **extra}


def test_claude_reply_from_the_session_running_under_the_pane(homes):
    # This test process stands in for Claude; its parent is the pane's shell.
    _write(homes / "claude/sessions/1.json", [_registration()])
    # Registered before its transcript exists: found once it appears.
    assert transcript.last_reply(_pane(str(os.getppid())), "") is None
    _write(homes / f"claude/projects/-x/{SID}.jsonl", [
        _say("user", "first ask"),
        _say("assistant", [{"type": "text", "text": "old reply"}]),
        _say("user", "second ask"),
        _say("assistant", [{"type": "text", "text": "Run this:"}]),
        _say("assistant", [{"type": "tool_use", "name": "Bash"}]),
        _say("user", [{"type": "tool_result", "content": "ok"}]),
        _say("assistant", [{"type": "text", "text": "sidechain"}], isSidechain=True),
        _say("assistant", [{"type": "thinking"}, {"type": "text", "text": "Done."}]),
        _say("user", "<system-reminder>x</system-reminder>", isMeta=True, origin=None),
        _say("user", "<task-notification>x", origin={"kind": "task-notification"}),
        _say("user", "malformed", origin="human"),
    ])
    assert transcript.last_reply(_pane(str(os.getppid())), "") == "Done."
    # The cached registration is rechecked: once its process is gone, so is its reply.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transcript, "_started", lambda pid: None)
        assert transcript.last_reply(_pane(str(os.getppid())), "") is None
    # The same pid registered by a process that started at another time is stale.
    _write(homes / "claude/sessions/1.json",
           [{"pid": os.getpid(), "sessionId": SID, "procStart": "1"}])
    assert transcript.last_reply(_pane(str(os.getppid())), "") is None
    # A process under some other pane is not this pane's agent.
    assert transcript.last_reply(_pane("999999999"), "") is None


def test_claude_new_user_message_clears_the_reply(homes):
    _write(homes / "claude/sessions/1.json", [_registration()])
    _write(homes / f"claude/projects/-x/{SID}.jsonl", [
        _say("assistant", [{"type": "text", "text": "old reply"}]),
        _say("user", [{"type": "text", "text": "next"}], promptSource="sdk"),
    ])
    assert transcript.last_reply(_pane(str(os.getppid())), "") is None


def test_codex_reply_from_the_thread_named_in_the_status_bar(homes, monkeypatch):
    monkeypatch.setattr(transcript, "_programs", lambda pid: {"codex"} if pid == "7" else set())
    def item(kind, text=None, tag="Text"):
        content = [{"type": tag, "text": text}] if text else []
        return {"type": "event_msg",
                "payload": {"type": "item_completed", "item": {"type": kind, "content": content}}}
    # A line that isn't JSON (a write cut short) is skipped, not fatal.
    _write(homes / f"codex/sessions/2026/10/09/rollout-2026-10-09T09-00-00-{SID}.jsonl",
           [item("UserMessage"), item("AgentMessage", "old"), item("UserMessage"),
            item("AgentMessage", "new", tag="text"), item("Reasoning")], junk='{"cut":')
    status = f"› Ask Codex to do anything\n  {SID} · gpt-6-sol medium · ~/src/app · Ready"
    assert transcript.last_reply(_pane("7"), status) == "new"
    # The same footer printed in a pane not running Codex is just text.
    assert transcript.last_reply(_pane("8"), status) is None
    # Resumed, the thread continues in a segment file, here in the older flat shapes.
    today = time.strftime("%Y/%m/%d")  # a resume writes its segment in today's folder
    resumed = homes / f"codex/sessions/{today}/rollout-2026-10-10T09-00-00-{SID}_seg.jsonl"
    flat = [{"type": "event_msg", "payload": {"type": t, "message": m}}
            for t, m in (("agent_message", "stale"), ("user_message", "go on"),
                         ("agent_message", "resumed"))]
    _write(resumed, flat)
    assert transcript.last_reply(_pane("7"), status) == "resumed"
    _write(resumed, flat[:2])
    assert transcript.last_reply(_pane("7"), status) is None
    assert transcript.last_reply(_pane("7"), "› Ready\n  gpt-6-sol · ~/src/app") is None
