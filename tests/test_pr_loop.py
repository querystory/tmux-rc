"""pr_loop's decision graph and its LLM-answer parsing, with gh, git and the LLM stubbed."""
import json

import pytest

from scripts import pr_loop as P

HEAD = "a" * 40


def thread(tid, last_body="Consider guarding this.", last_id=2, resolved=False):
    return {"id": tid, "isResolved": resolved, "path": "src/widget.py", "line": 4,
            "comments": {"nodes": [
                {"databaseId": 1, "author": {"login": P.COPILOT}, "body": "Nit.",
                 "diffHunk": "@@ -1 +1 @@"},
                {"databaseId": last_id, "author": {"login": "someone"}, "body": last_body,
                 "diffHunk": "@@ -1 +1 @@"}]}}


def obs(**kw):
    return {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "ci": "pass",
            "reviewed": True, "threads": [], "head": HEAD, **kw}


def st(**kw):
    return {"rounds": [], "threads": {}, "ci_attempts": 0, "review_requested": None, **kw}


def act(o, s=None, rounds=3, attempts=2):
    return P.next_action(o, s or st(), rounds, attempts)[0]


def test_ci_status_buckets():
    assert P.ci_status([]) == "pending"
    assert P.ci_status([{"bucket": "pass"}, {"bucket": "pending"}]) == "pending"
    assert P.ci_status([{"bucket": "pass"}, {"bucket": "cancel"}]) == "fail"
    assert P.ci_status([{"bucket": "pass"}, {"bucket": "skipping"}]) == "pass"


def test_copilot_reviewed_uses_latest_copilot_review():
    old = {"author": {"login": P.COPILOT}, "commit": {"oid": "b" * 40}}
    new = {"author": {"login": P.COPILOT}, "commit": {"oid": HEAD}}
    human = {"author": {"login": "someone"}, "commit": {"oid": "c" * 40}}
    assert P.copilot_reviewed([old, new, human], HEAD)
    assert not P.copilot_reviewed([new, old], HEAD)
    assert not P.copilot_reviewed([human], HEAD)


def test_pending_threads_skips_resolved_ours_and_already_triaged():
    ours = thread("T2", last_body=f"Done.\n{P.MARKER}")
    done = thread("T3")
    reopened = thread("T4", last_id=9)
    blocked = thread("T5")
    unfinished = thread("T7")
    handled = {"T3": {"last_comment": 2, "step": "replied"},
               "T4": {"last_comment": 2, "step": "replied"},
               "T5": {"last_comment": 2, "step": "blocked"},
               "T7": {"last_comment": 2, "step": "triaged", "action": "fix"}}
    got = P.pending_threads([thread("T1", resolved=True), ours, done, reopened, blocked,
                             thread("T6"), unfinished], handled)
    assert [t["id"] for t in got] == ["T4", "T5", "T6", "T7"]


@pytest.mark.parametrize(("o", "s", "want"), [
    (obs(state="MERGED"), st(), "blocked"),
    (obs(isDraft=True), st(), "blocked"),
    (obs(mergeable="CONFLICTING"), st(), "blocked"),
    (obs(ci="fail"), st(), "fix_ci"),
    (obs(ci="fail"), st(ci_attempts=2), "blocked"),
    (obs(ci="fail", threads=[thread("T")]), st(), "fix_ci"),
    (obs(ci="pending", threads=[thread("T")]), st(), "triage"),
    (obs(threads=[thread("T")]), st(rounds=[{}, {}, {}]), "done"),
    (obs(ci="pending"), st(), "wait"),
    (obs(ci="pending", reviewed=False), st(), "request_review"),
    (obs(ci="pending", reviewed=False), st(review_requested=HEAD), "wait"),
    (obs(reviewed=False), st(), "request_review"),
    (obs(reviewed=False), st(review_requested=HEAD), "wait"),
    (obs(reviewed=False), st(review_requested="b" * 40), "request_review"),
    (obs(), st(), "done"),
])
def test_next_action(o, s, want):
    assert act(o, s) == want


def test_max_rounds_reason_is_reported():
    node, reason = P.next_action(obs(ci="fail"), st(rounds=[{}]), 1, 2)
    assert node == "done"
    assert "max rounds" in reason


def test_parse_llm_json_takes_last_object_and_checks_enums():
    text = 'I looked at {"x": 1} first.\n```json\n{"status": "fixed", "summary": "ok"}\n```'
    assert P.parse_llm_json(text, P.RESULT_SCHEMA) == {"status": "fixed", "summary": "ok"}
    assert P.parse_llm_json({"action": "reply", "reply_text": "a", "rationale": "b"},
                            P.TRIAGE_SCHEMA)["action"] == "reply"
    with pytest.raises(ValueError, match="not in"):
        P.parse_llm_json('{"status": "maybe", "summary": ""}', P.RESULT_SCHEMA)
    with pytest.raises(ValueError, match="lacks"):
        P.parse_llm_json('{"status": "fixed"}', P.RESULT_SCHEMA)
    with pytest.raises(ValueError, match="no JSON"):
        P.parse_llm_json("no json here", P.RESULT_SCHEMA)


def test_reply_body_is_signed_and_marked():
    body = P.reply_body("Good catch. ", "abc123")
    assert body.startswith("Good catch.\n\nFixed in abc123.")
    assert P.SIGNATURE in body
    assert P.MARKER in body


def test_llm_claude_is_read_only_for_triage(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return json.dumps({"structured_output": {"action": "decline", "reply_text": "No.",
                                                 "rationale": "out of scope"}})
    monkeypatch.setattr(P, "run", fake_run)
    ans = P.llm("claude", "/wt", "prompt", P.TRIAGE_SCHEMA, edit=False)
    assert ans["action"] == "decline"
    allowed = seen["cmd"][seen["cmd"].index("--allowedTools") + 1:]
    assert "Edit" not in allowed[:allowed.index("--disallowedTools")]
    assert "Bash(git push*)" in seen["cmd"]


def test_finish_pushes_then_replies_and_resolves_once(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return "deadbeef\n" if cmd[:2] == ["git", "rev-list"] else ""
    monkeypatch.setattr(P, "run", fake_run)
    ctx = P.Ctx.__new__(P.Ctx)
    ctx.repo, ctx.pr, ctx.dry, ctx.wt, ctx.branch = "example-org/widget", 7, True, "/wt", "b"
    ctx.st = st(threads={
        "T1": {"action": "fix", "step": "committed", "sha": "s1", "comment_id": 1,
               "reply_text": "Fixed.", "rationale": "", "path": "a"},
        "T2": {"action": "decline", "step": "triaged", "comment_id": 2,
               "reply_text": "No.", "rationale": "", "path": "b"},
        "T3": {"action": "fix", "step": "blocked", "comment_id": 3,
               "reply_text": "", "rationale": "", "path": "c"}})
    assert P.finish(ctx)
    kinds = [c[1] if c[0] == "git" else c[2][:31] for c in calls]
    assert kinds.index("push") < kinds.index("repos/example-org/widget/pulls/")
    assert sum("graphql" in c for c in calls) == 1  # only the fixed thread is resolved
    assert [t["step"] for t in ctx.st["threads"].values()] == ["resolved", "replied", "blocked"]
    calls.clear()
    P.finish(ctx)  # a rerun posts nothing again
    assert not any(c[0] == "gh" for c in calls)


def test_summary_lists_threads():
    s = st(threads={"T1": {"action": "reply", "path": "a.py", "step": "replied",
                           "rationale": "a question"}})
    md = P.summary_md("example-org/widget", 7, s, obs(), "ready")
    assert "**reply** `a.py`" in md
    assert "never merges" in md


def test_parse_llm_json_prefers_the_outer_object():
    text = 'Answer: {"status": "blocked", "summary": "see", "extra": {"status": "fixed"}}'
    assert P.parse_llm_json(text, P.RESULT_SCHEMA)["status"] == "blocked"
