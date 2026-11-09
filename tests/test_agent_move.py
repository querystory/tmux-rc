"""Move to another agent: Codex -> Claude Code by writing a transcript, Claude Code -> Codex
through Codex's importer (a stand-in app-server here). Every conversation is invented."""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from openbus import agent_move, expunge

TID = "01a10000-0000-7000-8000-00000000000a"
IDLE = lambda _sid: False  # noqa: E731 - no thread runs on the target


def _rollout(path, *items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(i) + "\n" for i in items) + "{half a line")
    return path


def _item(payload, kind="response_item"):
    return {"timestamp": "2026-10-09T10:00:00Z", "type": kind, "payload": payload}


def _said(role, text):
    return _item({"type": "message", "role": role,
                  "content": [{"type": "input_text" if role == "user" else "output_text",
                               "text": text}]})


CONVERSATION = [
    _item({"id": TID, "cwd": "/srv/garden"}, "session_meta"),
    _said("user", "<environment_context>\n  <cwd>/srv/garden</cwd>\n</environment_context>"),
    _said("user", "Water the tomatoes"),  # the model's copy of the ask: the event is the record
    _item({"type": "item_completed", "item": {
        "type": "UserMessage", "content": [{"type": "text", "text": "Water the tomatoes"}]}},
        "event_msg"),
    _item({"type": "reasoning", "summary": [], "encrypted_content": "sealed"}),
    _item({"type": "function_call", "name": "exec_command", "call_id": "c1",
           "arguments": json.dumps({"cmd": "water --bed tomatoes"})}),
    _item({"type": "function_call_output", "call_id": "c1", "output": "x" * 5000}),
    _item({"type": "agent_message", "author": "helper",
           "content": [{"type": "text", "text": "aside"}]}),
    _said("assistant", "Watered bed 3."),
]


def test_a_codex_thread_reads_as_the_importer_would_write_it(tmp_path):
    patch = _item({"type": "custom_tool_call", "name": "apply_patch", "input": "*** basil"})
    basil = _item({"type": "user_message", "message": "Now the basil"}, "event_msg")
    later = _rollout(tmp_path / f"rollout-2026-10-09T11-00-00-{TID}_2.jsonl", basil, patch)
    first = _rollout(tmp_path / f"rollout-2026-10-09T10-00-{TID}.jsonl", *CONVERSATION)
    turns = agent_move.codex_turns([first, later])
    assert [t[0] for t in turns] == ["user", "assistant", "user", "assistant"]
    assert turns[0][1] == "Water the tomatoes"  # injected context and the model's copy left out
    reply = turns[1][1]
    assert reply.startswith('[external_agent_tool_call: exec_command]\n{"cmd": "water')
    assert f"[external_agent_tool_result]\n{'x' * 4000}\n[/external_agent_tool_result]" in reply
    assert reply.endswith("Watered bed 3.") and "sealed" not in reply and "aside" not in reply
    note = "[external_agent_tool_call: apply_patch]\n*** basil\n[/external_agent_tool_call]"
    assert turns[2:] == [["user", "Now the basil"], ["assistant", note]]  # the older event too


def test_a_long_thread_keeps_its_first_ask_and_the_newest_turns():
    turns = [["user", "first ask"], *([["assistant", "a" * 10], ["user", "u" * 10]] * 5)]
    kept = agent_move.budget(turns, limit=45)
    assert kept[0] == ["user", "first ask"] and kept[-3:] == turns[-3:]
    assert kept[1][1].startswith("[Earlier turns were left out") and len(kept) == 5
    assert agent_move.budget(turns) == turns and agent_move.budget([]) == []
    huge = agent_move.budget([["user", "q" * 900], ["assistant", "w" * 900]], limit=400)
    assert [t[:100] for _, t in huge] == ["q" * 100, "w" * 100] and len(huge[1][1]) < 200
    assert "cut to fit" in huge[1][1]  # the newest turn kept, its start


def test_a_codex_session_becomes_a_claude_transcript_it_can_resume(tmp_path):
    codex, claude = tmp_path / ".codex", tmp_path / ".claude"
    _rollout(codex / f"sessions/2026/10/09/rollout-2026-10-09T10-00-00-{TID}.jsonl", *CONVERSATION)
    s = expunge.Session("codex", TID, codex, tmp_path / "agent-history", 0, "")
    sid = agent_move.move(s, "claude", claude, "/srv/garden", "claude", None, IDLE)
    out = claude / f"projects/-srv-garden/{sid}.jsonl"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["type"] for r in rows] == ["user", "assistant"]
    assert [r["parentUuid"] for r in rows] == [None, rows[0]["uuid"]]  # one chain
    assert {r["sessionId"] for r in rows} == {sid} and rows[0]["cwd"] == "/srv/garden"
    assert rows[0]["message"] == {"role": "user", "content": "Water the tomatoes"}
    assert rows[0]["origin"] == {"kind": "human"} and "origin" not in rows[1]
    with pytest.raises(LookupError, match="too long"):
        agent_move.move(s, "claude", claude, "/srv/" + "g" * 300, "claude", None, IDLE)
    assert agent_move._text([{"type": "input_image"}, {"type": "text", "text": "this"}]) == \
        "[An image was here; it did not carry over.]\nthis"
    assert rows[1]["message"]["content"][0]["text"].endswith("Watered bed 3.")


# Codex's app-server, as far as the import goes: it answers from $CODEX_HOME/reply.json,
# and records the environment and the transcript it was handed.
FAKE = """#!{python}
import json, os, sys
home = os.environ["CODEX_HOME"]
reply = json.load(open(os.path.join(home, "reply.json")))
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") != "externalAgentConfig/import":
        continue
    path = m["params"]["migrationItems"][0]["details"]["sessions"][0]["path"]
    json.dump({{"env": sorted(os.environ), "path": path, "under_home": path.startswith(
        os.environ["HOME"] + "/.claude/projects/"), "text": open(path).read()}},
        open(os.path.join(home, "seen.json"), "w"))
    print(json.dumps({{"method": "externalAgentConfig/import/completed", "params": {{
        "itemTypeResults": [{{"itemType": "SESSIONS", "successes": reply}}]}}}}), flush=True)
"""


def test_a_claude_session_goes_through_codexs_own_importer(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "never passed on")
    fake = tmp_path / "codex"
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(0o755)
    claude, codex = tmp_path / ".claude-personal", tmp_path / ".codex"
    log = claude / "projects/-srv-garden/ab12.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"type": "user"}\n{"type": "assist')  # mid-append: the copy ends whole
    codex.mkdir()
    (codex / "reply.json").write_text(json.dumps([{"target": TID}]))
    s = expunge.Session("claude", "ab12", claude, tmp_path / "agent-history", 0, "")
    assert agent_move.move(s, "codex", codex, "/srv/garden", str(fake), os.defpath, IDLE) == TID
    seen = json.loads((codex / "seen.json").read_text())
    assert seen["under_home"] and seen["text"] == '{"type": "user"}\n'
    # Nothing of the daemon's (LC_CTYPE is the stand-in's own Python coercing its locale).
    assert set(seen["env"]) - {"LC_CTYPE"} == {"CODEX_HOME", "HOME", "PATH"}
    assert not list((tmp_path / "state").rglob("*.jsonl"))  # the copy is gone
    # Unchanged since, it imports nothing new: the thread is the one its ledger names.
    (codex / "reply.json").write_text("[]")
    (codex / "external_agent_session_imports.json").write_text(json.dumps(
        {"records": [{"source_path": seen["path"], "imported_thread_id": TID}]}))
    Path(seen["path"]).write_text("left by a crash")  # under _MOVES, any copy here is stale
    assert agent_move.move(s, "codex", codex, "/srv/garden", str(fake), os.defpath, IDLE) == TID
    seen["path"] = "untouched"  # an append to a running thread is refused before any import
    with pytest.raises(LookupError, match="running"):
        agent_move.move(s, "codex", codex, "/srv/garden", str(fake), os.defpath, {TID}.__contains__)
    assert json.loads((codex / "seen.json").read_text())["path"] != "untouched"
    (codex / "external_agent_session_imports.json").unlink()
    with pytest.raises(LookupError, match="nothing to import"):
        agent_move.move(s, "codex", codex, "/srv/garden", str(fake), os.defpath, IDLE)
