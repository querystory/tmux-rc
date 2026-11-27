"""pr_loop's decision graph and its LLM-answer parsing, with gh, git and the LLM stubbed."""
import json
import re

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


SHA1, SHA2 = "1" * 40, "2" * 40


def gh_thread(tid, resolved=True, replied=True):
    nodes = [{"author": {"login": P.COPILOT}, "url": f"https://gh.test/{tid}#c1",
              "body": "A `;` inside quotes splits the command. The guard then misses it."}]
    if replied:
        nodes.append({"author": {"login": "someone"}, "url": f"https://gh.test/{tid}#c2",
                      "body": f"Fixed.\n{P.MARKER}"})
    return {"id": tid, "isResolved": resolved, "path": "src/guard.py", "line": None,
            "comments": {"nodes": nodes}}


def raw_facts(**kw):
    pr = {"title": "Guard: block <kill> in one call", "url": "https://gh.test/pr/7",
          "body": "## Why\n\nThe guard let a **quoted** `;` through.\n\n## What\n\n- x",
          "state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "additions": 40,
          "deletions": 2, "baseRefName": "main", "headRefOid": HEAD,
          "commits": {"nodes": [{"commit": {"oid": o}} for o in (SHA1, SHA2, HEAD)]},
          "reviews": {"nodes": [{"author": {"login": P.COPILOT}, "commit": {"oid": HEAD}}]},
          "reviewThreads": {"nodes": [gh_thread("T1"), gh_thread("T2")]}}
    pr.update(kw.pop("pr", {}))
    files = [{"name": "src/guard.py", "add": 5, "del": 1}]
    return {"pr": pr, "checks": [{"name": "test", "bucket": "pass", "link": "https://gh.test/c"},
                                 {"name": "deploy", "bucket": "skipping", "link": None}],
            "commits": {SHA1: {"subject": "Review: Split only where the shell would", "url": "u1",
                               "files": [*files, {"name": "tests/test_guard.py", "add": 3,
                                                  "del": 0}]},
                        SHA2: {"subject": "Review: " + "x" * 50 + " yyyyyyyyyy",
                               "url": "u2", "files": files}},
            "recipe": ["uv run pytest -q tests/", "node --test tests/*.mjs"],
            "workflows": "- run: uv run pytest -q tests/\n- run: node --test tests/*.mjs",
            **kw}


def loop_state():
    """A finished run whose triage rationale says the opposite of what happened."""
    lie = "The edits were denied permission in this session, so they are not applied yet."
    return st(rounds=[{"n": 1, "head": "c" * 40, "action": "triage", "triage": ["T1"]},
                      {"n": 2, "head": SHA1, "action": "triage", "triage": ["T2"]}],
              threads={t: {"action": "fix", "reply_text": "Fixed.", "rationale": lie,
                           "path": "src/guard.py", "comment_id": 1, "last_comment": 1,
                           "step": "resolved", "sha": sha}
                       for t, sha in (("T1", SHA1), ("T2", SHA2))})


def test_decision_comes_from_observed_facts_not_llm_prose():
    m = P.decide("example-org/widget", 7, loop_state(), raw_facts(), ["uv run pytest -q tests/"],
                 "ready")
    page = P.render_page(m)
    for text in (page, P.summary_text(m), P.meta_description(m)):
        assert "denied" not in text
    assert m["verdict"] == "Merge"
    assert m["confidence"] == "medium"  # SHA2 adds no test
    assert [c["resolved"] for c in m["cards"]] == [True, True]
    assert m["cards"][0]["subject"] == "Split only where the shell would"
    assert m["cards"][1]["subject"].endswith("…")  # an old run's subject cut mid-word
    assert m["cards"][0]["risk"] == "A `;` inside quotes splits the command."
    assert m["cards"][0]["reply_url"] == "https://gh.test/T1#c2"
    assert [p["n"] for p in m["series"]] == [1, 1, 0]
    s = m["stats"]
    assert (s["fixed"], s["commits"], s["tests"], s["adds"], s["dels"]) == (2, 2, 3, 13, 2)
    assert "The guard let a <strong>quoted</strong> <code>;</code> through." in page
    assert "&lt;kill&gt;" in page  # GitHub text is escaped
    assert "gh pr merge 7 --squash -R example-org/widget" in page


def test_caveats_name_only_what_the_run_skipped():
    verify = ["make lint", "uv run pytest -q tests/ --deselect tests/test_slow.py"]
    raw = raw_facts(stash=["stash@{0}: On b: pr-loop rejected: x"])
    cav = " | ".join(P.decide("example-org/widget", 7, loop_state(), raw, verify, "ok")["caveats"])
    assert "`node --test tests/*.mjs` (part of `make test`) was not run locally" in cav
    assert "CI runs it, and CI passed" in cav
    assert "Skipped locally: `tests/test_slow.py`; CI's full test run includes it" in cav
    assert f"`{SHA2[:8]}` (src/guard.py) adds no test" in cav
    assert f"`{SHA1[:8]}`" not in cav
    assert "still stashed" in cav
    assert "CI skipped: deploy." in cav
    every = P.decide("example-org/widget", 7, loop_state(), raw_facts(), ["make test"], "ok")
    assert not any("not run locally" in c for c in every["caveats"])
    unknown = P.decide("example-org/widget", 7, loop_state(), raw_facts(), None, "ok")
    assert any("did not record" in c for c in unknown["caveats"])


@pytest.mark.parametrize(("raw", "outcome", "blocker"), [
    (raw_facts(pr={"reviewThreads": {"nodes": [gh_thread("T1", resolved=False)]}}), "ok",
     "0/1 review threads resolved"),
    (raw_facts(checks=[{"name": "test", "bucket": "fail"}]), "ok", "CI is fail"),
    (raw_facts(pr={"reviews": {"nodes": []}}), "ok", "Copilot has not reviewed"),
    (raw_facts(pr={"mergeable": "CONFLICTING"}), "ok", "Mergeable: CONFLICTING"),
    (raw_facts(pr={"commits": {"nodes": []}}), "ok", "Not on the PR: 2 fix commits"),
    (raw_facts(), "BLOCKED: tests failed", "BLOCKED: tests failed"),
    ({}, "BLOCKED: gh auth", "BLOCKED: gh auth"),
])
def test_decision_holds_when_a_gate_fails(raw, outcome, blocker):
    m = P.decide("example-org/widget", 7, loop_state(), raw, ["make test"], outcome)
    assert m["verdict"] == "Hold"
    assert any(blocker in b for b in m["blockers"])
    page = P.render_page(m)
    assert "rerun pr-loop 7" in page
    assert "gh pr merge" not in page


def test_page_is_self_contained():
    page = P.render_page(P.decide("example-org/widget", 7, loop_state(), raw_facts(),
                                  ["make test"], "ok"))
    assert "<script" not in page
    assert not re.search(r"(src=|url\(|@import)\s*[\"']?https?:", page)
    assert page.count("<svg") == 2
    assert "<details>" in page


def test_short_subject_and_why_text():
    assert P.short_subject("Fix it") == "Fix it"
    cut = P.short_subject("word " * 30)
    assert cut.endswith("…")
    assert len(cut) <= P.SUBJECT_MAX + 1
    assert P.why_text("Intro para.\n\n## Why\n\nBecause [docs](http://x) lied.") == (
        "Because docs lied.")
    assert P.why_text("# Title\n\nFirst para\nwraps.\n\nSecond.") == "First para wraps."
    assert P.plural(1, "fix") == "1 fix"
    assert P.plural(2, "fix") == "2 fixes"


def test_summary_only_rebuilds_the_page_and_changes_nothing_else(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TMUXRC_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("TMUXRC_SCRATCH_URL", "https://scratch.test")
    monkeypatch.setenv("PR_LOOP_BRAND_DIR", str(tmp_path / "no-brand"))
    path = P.state_path("example-org/widget", 7)
    P.save_state(path, {**loop_state(), "status": "CI green"})
    before = path.read_text()
    folder = tmp_path / "scratch" / "pr-loop-example-org-widget-7"
    folder.mkdir(parents=True)
    (folder / "index.md").write_text("old page")
    (folder / "meta.json").write_text('{"updating": "x"}')
    monkeypatch.setattr(P, "observe", lambda *a: obs(branch="b"))
    monkeypatch.setattr(P, "worktrees", lambda cwd: ({}, cwd))
    monkeypatch.setattr(P, "gather", lambda *a: raw_facts())

    def fake_run(cmd, **kw):
        assert cmd[0] == "date", cmd  # no gh, git, push or LLM call
        return "2026-01-01T00:00:00+00:00\n"
    monkeypatch.setattr(P, "run", fake_run)
    assert P.main(["7", "--repo", "example-org/widget", "--summary-only"]) == 0
    assert path.read_text() == before
    assert not (folder / "index.md").exists()
    assert "Merge <em>#7</em>?" in (folder / "index.html").read_text()
    meta = json.loads((folder / "meta.json").read_text())
    assert "updating" not in meta
    assert meta["description"].startswith("pr-loop recommends merging widget #7")


def test_parse_llm_json_prefers_the_outer_object():
    text = 'Answer: {"status": "blocked", "summary": "see", "extra": {"status": "fixed"}}'
    assert P.parse_llm_json(text, P.RESULT_SCHEMA)["status"] == "blocked"
