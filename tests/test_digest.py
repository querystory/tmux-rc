"""The digest's code side: stage rules, the effort join, cleanup gates, theme identity, the
pages' surface and the scorer, over the invented fixture in research/eval/digest."""
import copy
import json
import os
from pathlib import Path

from scripts import digest, digest_eval

FIXTURE = Path(__file__).parents[1] / "research/eval/digest"
FACTS, BASELINE = (json.loads((FIXTURE / f).read_text()) for f in ("facts.json", "baseline.json"))
NOW = FACTS["now"]


def base():
    return {**BASELINE, **digest.lay(copy.deepcopy(BASELINE), FACTS), "leaks": 0}


def test_stage_and_need_are_rules(monkeypatch):
    def node(**over):
        checks = {"state": over.pop("checks", "SUCCESS")}
        return {"number": 1, "title": "t", "url": "u", "state": "OPEN", "isDraft": False,
                "updatedAt": None, "mergedAt": None, "closedAt": None, "mergeable": "MERGEABLE",
                "reviewDecision": None, "headRefName": "h", "headRefOid": "c", "bodyText": "",
                "reviews": {"nodes": []}, "repository": {"nameWithOwner": "o/r"},
                "commits": {"nodes": [{"commit": {"statusCheckRollup": checks}}]}, **over}

    def rule(held=False, **over):
        c = digest.change(node(**over), NOW, held)
        return c["stage"], c["need"]

    assert rule(state="MERGED") == ("main", "")
    monkeypatch.setenv("TMUXRC_DIGEST_HIDE", "r")  # a hidden repository's words go nowhere
    assert digest.change(node(), NOW, held=False)["title"] == "Private work"
    assert rule(held=True, mergeable="CONFLICTING") == ("held", "rebase")
    assert rule(held=True) == ("held", "")
    assert rule(reviewDecision="APPROVED") == ("ready", "merge")
    assert rule(reviewDecision="APPROVED", mergeable="CONFLICTING") == ("open", "")
    assert rule(reviewDecision="APPROVED", checks="FAILURE") == ("stuck", "")


def test_effort_joins_by_tool_paths_not_the_root_branch(tmp_path):
    """The session sits at the repo root on an unrelated branch; its agent works with git -C."""
    def turn(n, command, **more):
        message = {"id": f"m{n}", "model": "claude-opus", "usage": {"output_tokens": 1_000_000},
                   "content": [{"type": "tool_use", "input": {"command": command}}]}
        return json.dumps({"type": "assistant", "cwd": "/w/repo", "gitBranch": "unrelated",
                           "timestamp": "2027-01-15T07:00:00Z", "message": message, **more})

    project = tmp_path / "projects/p"
    project.mkdir(parents=True)
    (project / "a.jsonl").write_text(turn(1, "git -C /w/wt/reader status") + "\n" + turn(2, "ls"))
    (project / "b.jsonl").write_text(turn(3, "ls /w/repo"))
    for f in project.iterdir():
        os.utime(f, (NOW, NOW))
    rows = digest.spend([str(tmp_path)], ["/w/wt/reader"], NOW)
    assert sorted((r["d"], r["wt"]) for r in rows) == [(20.0, None), (40.0, "/w/wt/reader")]
    worktrees = [{"path": "/w/wt/reader", "repo": "o/r", "branch": "reader"}]
    changes = [{"repo": "o/r", "head": "reader", "key": "o/r#5"},
               {"repo": "o/r", "head": "unrelated", "key": "o/r#9"}]
    assert {k: v["d"] for k, v in digest.join(rows, worktrees, changes).items()} == {
        "o/r#5": 40.0, "": 20.0}


def test_cleanup_is_gated_by_code():
    wt = {"path": "/w/wt/a", "common": "/w/repo/.git", "repo": "o/r", "branch": "a", "oid": "c1",
          "dirty": False, "active_at": NOW - 2 * digest.DAY}
    merged = [{"repo": "o/r", "head": "a", "oid": "c1", "stage": "main"}]

    def safe(changes=merged, cwds=(), **over):
        return bool(digest.prunable([{**wt, **over}], changes, cwds, NOW))

    assert safe()
    assert not safe(dirty=True) and not safe(oid="c2") and not safe(active_at=NOW - 60)
    assert not safe(cwds=["/w/wt/a/src"]) and not safe(path="/w/repo")
    assert not safe(changes=[]) and not safe(changes=[{**merged[0], "stage": "open"}])


def test_theme_identity_is_stable():
    reg = copy.deepcopy(BASELINE)
    new = {c["title"]: n for n, c in enumerate(digest.lay(reg, FACTS)["new"])}
    reply = {"daily": "Fine. See example-org/harbor#412.", "weekly": "Search led.",
             "asks": [{"n": 0, "text": "Highlight sync needs a rebase"}, {"n": 9, "text": "x"}],
             "themes": [
        {"id": "search", "name": "Finding books", "short": "Search", "outcome": "Renamed.",
         "add": [new["chore: bump the linter"]]},
        {"id": "solo", "name": "Upkeep", "short": "Upkeep", "outcome": "One change.",
         "add": [new["chore: bump the linter"]]}]}
    d = digest.build(FACTS, reg, lambda system, text: reply)
    assert d["daily"] == "Fine" and d["leaks"] == 1  # the sentence naming a change is dropped
    assert [a["text"] for a in d["actions"][:2]] == [  # the model's words, else the rule's
        "Highlight sync needs a rebase", "Reminders can merge"]
    assert "solo" not in reg["themes"]  # one change is not a theme, and it was already placed
    assert reg["themes"]["search"]["name"] == "Finding books"  # a rename keeps the id
    assert "example-org/harbor#425" in reg["themes"]["search"]["members"]
    again = digest.build(FACTS, reg, lambda system, text: None)  # the model is down
    assert again["daily"] == digest.FALLBACK and again["themes"] == d["themes"]


def test_pages_keep_change_numbers_behind_a_tap(tmp_path):
    d = base()
    assert [t["state"] for t in d["themes"]] == ["held", "stuck", "open", "open"]
    assert [a["text"] for a in d["actions"]] == [
        "Offline reading needs a rebase", "Reminders can merge", "Search ranking",
        "A teammate asks for your review"]
    digest.publish(d, tmp_path)
    for name in ("digest-daily", "digest-weekly"):
        page = (tmp_path / name / "index.html").read_text()
        body = page[page.index("<main>"):]
        for c in FACTS["changes"]:  # a change's link is behind the tap; nothing else names it
            assert c["key"].split("#")[1] not in body.replace(c["url"], "")
        assert "Reading without a network" in body and "harbor" not in body.replace("/harbor/", "")
        assert json.loads((tmp_path / name / "meta.json").read_text())["description"]
    summary = json.loads((tmp_path / "digest-daily/summary.json").read_text())  # the dashboard's
    assert set(summary) == {"generated", "verdict", "needs_you", "more", "themes", "pace"}
    assert summary["verdict"] == BASELINE["daily"] and summary["pace"] == "On pace"
    assert summary["more"] == 1 and summary["needs_you"][0] == {
        "text": "Offline reading needs a rebase", "href": d["actions"][0]["url"]}
    assert summary["themes"][0] == {"name": "Reading without a network", "share": 0.52,
                                    "state": "Live on your machine, not on main"}


def test_scorer_fails_unsafe_advice_and_counts_regrouping():
    assert digest_eval.score(base(), base()) == {
        "pass": True, "lost": 0, "agreement": 1.0, "unsafe": [], "notes": []}
    unsafe = base()
    unsafe["themes"][0]["outcome"] = "The highlight work is ready to merge."  # it conflicts
    assert not digest_eval.score(unsafe, base())["pass"]
    unsafe = base()
    unsafe["weekly"] += " Delete the old worktrees."
    assert not digest_eval.score(unsafe, base())["pass"]
    mixed = copy.deepcopy(BASELINE)
    mixed["themes"]["desk"]["members"] += mixed["themes"].pop("search")["members"]
    result = digest_eval.score({**BASELINE, **digest.lay(mixed, FACTS), "leaks": 0}, base())
    assert not result["pass"] and result["agreement"] < .85 and "-2 3 themes" in result["notes"]
