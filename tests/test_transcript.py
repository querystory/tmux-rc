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
    # The turn stands still with the conversation, and moves on with it.
    pane, path = _pane(str(os.getppid())), homes / f"claude/projects/-x/{SID}.jsonl"
    turn = transcript.turn(pane, "")
    assert turn and transcript.turn(pane, "") == turn
    with path.open("a") as f:  # bookkeeping is not the conversation moving on
        f.write(json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")
        f.write(json.dumps(_say("assistant", [{"type": "text", "text": "x"}], isSidechain=True))
                + "\n")
    assert transcript.turn(pane, "") == turn
    with path.open("a") as f:
        f.write(json.dumps(_say("user", "third ask")) + "\n")
    assert transcript.turn(pane, "") not in (turn, None)
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
    # Two under the pane (one suspended behind the other): which is on screen is unknown.
    _write(homes / "claude/sessions/1.json", [_registration()])
    _write(homes / "claude/sessions/2.json", [_registration()])
    assert transcript._claude_file(_pane(str(os.getppid()))) is None
    # Once one of them has exited, the other is the pane's again.
    real = transcript._started
    with pytest.MonkeyPatch.context() as mp:
        calls = iter([real(os.getpid()), None])
        mp.setattr(transcript, "_started", lambda pid: next(calls, real(pid)))
        assert transcript._claude_file(_pane(str(os.getppid()))) is not None


def test_claude_new_user_message_clears_the_reply(homes):
    _write(homes / "claude/sessions/1.json", [_registration()])
    _write(homes / f"claude/projects/-x/{SID}.jsonl", [
        _say("assistant", [{"type": "text", "text": "old reply"}]),
        _say("user", [{"type": "text", "text": "next"}], promptSource="sdk"),
    ])
    assert transcript.last_reply(_pane(str(os.getppid())), "") is None


def test_claude_turn_is_closed_by_its_turn_duration(homes):
    # The "Worked for" stamp's record closes the turn; bookkeeping after it keeps it closed,
    # and a new message (a prompt, a task notification) leaves it unknown, as an interrupt
    # writes no record of its own.
    _write(homes / "claude/sessions/1.json", [_registration()])
    pane, path = _pane(str(os.getppid())), homes / f"claude/projects/-x/{SID}.jsonl"
    _write(path, [_say("user", "go"), _say("assistant", [{"type": "text", "text": "Done."}])])
    assert transcript.turn_live(pane, "") is None
    with path.open("a") as f:
        for e in ({"type": "system", "subtype": "turn_duration"},
                  {"type": "system", "subtype": "stop_hook_summary"},
                  _say("assistant", [{"type": "text", "text": "x"}], isSidechain=True)):
            f.write(json.dumps(e) + "\n")
    assert transcript.turn_live(pane, "") is False
    with path.open("a") as f:  # a new turn's first record, still being written
        f.write('{"type": "user", "mess')
    assert transcript.turn_live(pane, "") is None
    with path.open("a") as f:
        f.write('age": {"role": "user", "content": "<task-notification>x"}}\n')
    assert transcript.turn_live(pane, "") is None
    # A Claude run under Codex is not the pane's turn: the thread Codex names is.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transcript, "_codex_file", lambda _p, _t: path.with_name("rollout.jsonl"))
        path.with_name("rollout.jsonl").write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        assert transcript.turn_live(pane, "") is True
        mp.setattr(transcript, "_codex_file", lambda _p, _t: None)  # no thread on screen
        mp.setattr(transcript, "_programs", lambda _pid: {"codex", "claude"})
        assert transcript.turn_live(pane, "") is None


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
    turn = transcript.turn(_pane("7"), status)  # these shapes move the turn on, too
    _write(resumed, flat[:2])
    assert transcript.turn(_pane("7"), status) not in (turn, None)
    assert transcript.last_reply(_pane("7"), status) is None
    assert transcript.last_reply(_pane("7"), "› Ready\n  gpt-6-sol · ~/src/app") is None


def test_codex_turn_open_from_its_rollout(homes, monkeypatch):
    monkeypatch.setattr(transcript, "_programs", lambda pid: {"codex"})
    path = homes / f"codex/sessions/2026/10/09/rollout-2026-10-09T09-00-00-{SID}.jsonl"
    status = f"› Ask Codex to do anything\n  {SID} · gpt-6-sol medium · ~/src/app · Ready"
    def event(kind):
        return json.dumps({"type": "event_msg", "payload": {"type": kind}}) + "\n"
    _write(path, [])
    assert transcript.turn_live(_pane("7"), status) is None  # no turn yet
    with path.open("a") as f:
        f.write(event("task_started") + event("item_completed") + '{"cut":')
    assert transcript.turn_live(_pane("7"), status) is True
    # Read on from where it stopped: the cut line, finished, counts once it is whole.
    with path.open("a") as f:
        f.write("1}\n" + event("task_complete"))
    assert transcript.turn_live(_pane("7"), status) is False
    with path.open("a") as f:
        f.write(event("task_started") + event("turn_aborted"))
    assert transcript.turn_live(_pane("7"), status) is False
    assert transcript.turn_live(_pane("7"), "› Ask Codex to do anything") is None
    # Fit to a phone, the bar is cut to its thread and a clipped model, dimmed.
    with path.open("a") as f:
        f.write(event("task_started"))
    narrow = f"⟪/dim⟫› Ask Codex to do anything\n\n  ⟪/dim⟫{SID} · GPT-6.1-…"
    assert transcript.turn_live(_pane("7"), narrow) is True
    assert transcript.turn_live(_pane("7"), f"›\n  {SID} · gpt-6.1-sol high") is True
    # A model cut to its first letters, or crowded out by a warnings segment, still counts.
    assert transcript.turn_live(_pane("7"), f"›\n  {SID} · GPT-6-So…") is True
    warned = f"›\n  ⟪/dim⟫{SID} · GPT…  ⚠ 2 warnings · f2 to view"
    assert transcript.turn_live(_pane("7"), warned) is True
    # A bare id on the bottom row is output naming some other thread, not the bar.
    decoy = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    path.with_name(f"rollout-2026-10-09T08-00-00-{decoy}.jsonl").write_text(event("task_started"))
    assert transcript.turn_live(_pane("7"), f"› ps\n{decoy}") is None
    assert transcript.turn_live(_pane("7"), f"› ps\n  41 codex resume {decoy}") is None
    # Copied over by a shorter session whose turn is closed: read afresh, not from the
    # old offset.
    path.unlink()
    path.write_text(event("task_started") + event("task_complete"))
    assert transcript.turn_live(_pane("7"), status) is False
