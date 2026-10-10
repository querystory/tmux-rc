"""Open loops: the GitHub query and its parse, the worktree scan, and the lane join."""
import json
import subprocess
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from openbus import open_loops, server
from openbus.open_loops import OpenLoops, build, fetch_github, requests, scan_worktrees

NOW = datetime(2026, 6, 16, 9, tzinfo=UTC).timestamp()
H = 3600
REPO = "example-org/shop-api"


def node(number, *, base="main", head=None, author="dev", state="OPEN",  # noqa: PLR0913
         updated=H,
         decision="REVIEW_REQUIRED", checks="SUCCESS", requested=(), reviews=(),
         base_ref="exists", merged=None, draft=False, mergeable="MERGEABLE", labels=(),
         repo=REPO):
    """One PullRequest as the GraphQL fragment returns it, a knob per field a rule reads."""
    stamp = lambda ago: ago is not None and datetime.fromtimestamp(NOW - ago, UTC).isoformat()  # noqa: E731
    return {
        "number": number, "title": f"Change {number}",
        "url": f"https://github.com/{REPO}/pull/{number}",
        "isDraft": draft, "state": state, "updatedAt": stamp(updated),
        "mergedAt": stamp(merged) or None,
        "author": {"login": author}, "baseRefName": base, "headRefName": head or f"feat/{number}",
        "mergeable": mergeable, "reviewDecision": decision,
        "repository": {"nameWithOwner": repo, "defaultBranchRef": {"name": "main"}},
        "labels": {"nodes": [{"name": name} for name in labels]},
        "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": t}} for t in requested]},
        "latestReviews": {"nodes": [{"author": {"__typename": t, "login": who}, "state": s,
                                     "submittedAt": stamp(ago)} for t, who, s, ago in reviews]},
        "commits": {"nodes": [{"commit": {"committedDate": stamp(updated),
                                          "statusCheckRollup": checks and {"state": checks}}}]},
        "baseRef": None if base_ref is None else {
            "associatedPullRequests": {"totalCount": int(base_ref == "merged")}},
    }


def github(*nodes, asked=()):
    prs = [open_loops._pr(n) for n in nodes]  # the parse is the fixture
    for pr in prs:
        pr["asked"] = pr["number"] in asked
    return {"viewer": "dev", "older": 0, "prs": prs}


def pane(pane_id, *, cwd="/src/shop-api", prs=(), activity="idle", since=NOW - 60, **extra):
    return {"pane_id": pane_id, "title": f"agent {pane_id}", "session": "work", "window_index": 1,
            "cwd": cwd, "activity": activity, "state_since": since,
            "prs": [{"repo": REPO, "number": n} for n in prs], **extra}


def wt(path, branch, *, dirty=0, unpushed=0, active=H):
    return {"path": path, "repo": REPO, "branch": branch, "dirty": dirty, "unpushed": unpushed,
            "active_at": NOW - active}


def rows(lanes, lane):
    return {(i.get("number") or i.get("path") or i["pane"]["pane_id"]): i
            for g in lanes[lane] for i in g["items"]}


def test_requests_are_small_and_cover_everything():
    reqs = list(requests([("example-org/shop-web", 7), ('evil"org/x', 1)], NOW))
    assert [tag for tag, _ in reqs] == ["mine", "asked", "merged", "ref", "older", "older"]
    body = dict(reqs)
    assert "author:@me archived:false updated:>=2026-06-02T09:00:00Z" in body["mine"]
    assert "review-requested:@me" in body["asked"]
    assert "merged:>=2026-06-15T09:00:00Z" in body["merged"]
    assert ('r0: repository(owner: "example-org", name: "shop-web")'
            " { issueOrPullRequest(number: 7)") in body["ref"]
    assert 'owner: "evil\\"org"' in body["ref"]  # a string from the classifier cannot break out
    many = [tag for tag, _ in requests([(REPO, n) for n in range(1, 80)], NOW)]
    assert many.count("ref") == 3  # 50 references, 20 to a request


def test_fetch_pages_retries_and_keeps_partial_data(monkeypatch):
    calls = []

    def gh(args, _timeout):
        q = args[-1]
        calls.append(q)
        if "author:@me archived:false updated:>=" in q and len(calls) == 1:
            return "<html>502 Bad Gateway</html>"  # GitHub's answer to a slow search
        s = ({"issueCount": 4} if "updated:<" in q
             else {"pageInfo": {"hasNextPage": False}, "nodes": [node(3)]} if 'after: "c1"' in q
             else {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [node(1), {}]}
             if "author:@me archived:false updated:>=" in q
             else {"pageInfo": {}, "nodes": [node(1, author="lee")]} if "review-requested" in q
             else {"pageInfo": {}, "nodes": []} if "is:merged" in q
             else None)
        refs = {"r0": None, "r1": {"issueOrPullRequest": None},
                "r2": {"issueOrPullRequest": node(2, author="lee")}} if s is None else {"s": s}
        assert ("fragment P" in q) == ("...P" in q)  # an unused fragment is a GraphQL error
        return json.dumps({"data": {"viewer": {"login": "dev"}, **refs},
                           "errors": [{"type": "NOT_FOUND"}]})

    monkeypatch.setattr(open_loops, "run_gh", gh)
    got = fetch_github([(REPO, 9)], NOW)
    assert got["older"] == 8 and sorted(p["number"] for p in got["prs"]) == [1, 2, 3]
    assert next(p for p in got["prs"] if p["number"] == 1)["asked"]
    assert len(calls) == 8  # mine twice (a retry), page 2, asked, merged, refs, two counts


def test_fetch_failure_keeps_the_last_good_answer(monkeypatch):
    loops = OpenLoops()
    loops.github = {"viewer": "dev", "older": 2, "prs": []}
    monkeypatch.setattr(open_loops, "run_gh", lambda *a: None)
    monkeypatch.setattr(open_loops, "scan_worktrees", lambda cwds: [])
    loops.refresh([], NOW)
    assert loops.error == "unavailable" and loops.github["older"] == 2


def test_lanes():
    gh = github(
        node(10, decision="APPROVED"),                                   # ready: click merge
        node(11, base="feat/10", decision=None),                         # stacked behind 10
        node(12, author="lee"),                                          # asked to review
        node(13),                                                        # nobody asked
        node(14, requested=["Bot"], reviews=[("Bot", "copilot", "COMMENTED", H)]),  # bots only
        node(15, checks="FAILURE", requested=["User"], draft=True),
        node(16, base="feat/gone", base_ref=None, requested=["Team"]),
        node(17, mergeable="CONFLICTING", requested=["User"], updated=4 * 86400),
        node(18, state="MERGED", merged=2 * H, head="feat/18"),
        node(19, base="feat/18", requested=["User"],
             reviews=[("User", "lee", "APPROVED", 2 * H)]),             # moving: reviewed
        node(20, author="lee", updated=30 * 86400, checks="FAILURE"),   # a pane's old reference
        asked=[12])
    panes = [pane("%1", prs=[10, 20]),
             pane("%2", activity="waiting", waiting_on="user", question={"prompt": "Ship it?"}),
             pane("%3", cwd="/wt/solo", since=NOW - 2 * 86400),
             pane("%4", cwd="/src/shop-api/sub", since=NOW - 2 * 86400)]
    trees = [wt("/src/shop-api", "main", dirty=3),
             wt("/wt/solo", "spike", dirty=1),
             wt("/wt/a13", "feat/13", dirty=2),
             wt("/wt/landed", "feat/18", unpushed=2),
             wt("/wt/drift", "spike-2", unpushed=1),
             wt("/wt/old", "spike-3", dirty=1, active=30 * 86400),
             wt("/wt/a10", "feat/10", dirty=1)]
    lanes = build(gh, trees, panes, NOW)
    waiting, dropped = rows(lanes, "waiting"), rows(lanes, "dropped")

    assert waiting[10]["reasons"] == ["approved"]
    assert [p["pane_id"] for p in waiting[10]["panes"]] == ["%1"]
    assert waiting[12]["reasons"] == ["review_requested"]
    assert waiting["%2"]["reasons"] == ["needs_you"] and waiting["%2"]["text"] == "Ship it?"
    assert 11 not in waiting and 11 not in dropped  # waits on its base, not on you

    assert dropped[13]["reasons"] == ["no_reviewer"]
    assert dropped[14]["reasons"] == ["no_reviewer"]
    assert dropped[15]["reasons"] == ["checks_failed"]
    assert dropped[16]["reasons"] == ["base_merged"]
    assert dropped[17]["reasons"] == ["conflicts", "stale"]
    assert 20 not in dropped
    assert dropped["%3"]["reasons"] == ["idle_dirty"]
    assert "%4" not in dropped and "%1" not in dropped  # a shared checkout's dirt is no one's
    assert dropped["/wt/a13"]["reasons"] == ["no_pane"]
    assert dropped["/wt/drift"]["unpushed"] == 1
    for quiet in ("/wt/landed", "/wt/old", "/wt/a10", "/src/shop-api", "/wt/solo"):
        assert quiet not in dropped

    # Stacks group themselves under their root, merged links included.
    by_ws = {g["workstream"]["id"] if g["workstream"] else None: g for g in lanes["moving"]}
    assert [(i["kind"], i["number"]) for i in by_ws[f"{REPO}#18"]["items"]] == [
        ("committed", 19), ("merged", 18), ("reviewed", 19)]
    group = next(g for g in lanes["dropped"] if any(i.get("path") == "/wt/a13" for i in g["items"]))
    assert group["workstream"] == {"id": f"{REPO}#13", "name": "Change 13"}
    assert next(g for g in lanes["dropped"] if g["workstream"] is None)


def test_labels_then_keywords_join_what_the_stack_cannot():
    gh = github(node(30, checks="FAILURE", labels=["workstream:checkout", "bug"]),
                node(31, base="feat/30", checks="FAILURE"),  # rides its base's label
                node(40, checks="FAILURE", labels=["workstream:checkout"],
                     repo="example-org/shop-web"),
                node(50, checks="FAILURE", head="fix/coupon-rounding"),
                node(60, checks="FAILURE"))
    lanes = build(gh, [], [], NOW, {"pricing": ["Coupon"], "never": [""]})
    groups = {g["workstream"]["id"]: sorted(i["number"] for i in g["items"])
              for g in lanes["dropped"]}
    assert groups == {"workstream:checkout": [30, 31, 40], "workstream:pricing": [50],
                      f"{REPO}#60": [60]}


def test_keyword_map_from_the_environment(monkeypatch):
    monkeypatch.setenv("TMUXRC_WORKSTREAMS", '{"pricing": ["coupon"], "bad": "x"}')
    assert open_loops.keywords() == {"pricing": ["coupon"]}
    monkeypatch.setenv("TMUXRC_WORKSTREAMS", "[not json")
    assert open_loops.keywords() == {}


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


def test_scan_finds_every_worktree_and_what_it_holds(tmp_path):
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    _git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    _git("clone", str(remote), str(repo), cwd=tmp_path)
    _git("remote", "set-url", "origin", "git@github.com:example-org/shop-api.git", cwd=repo)
    _git("remote", "set-url", "--push", "origin", str(remote), cwd=repo)
    _git("commit", "--allow-empty", "-m", "base", cwd=repo)
    _git("push", str(remote), "HEAD:main", cwd=repo)
    _git("fetch", str(remote), "+refs/heads/*:refs/remotes/origin/*", cwd=repo)
    _git("worktree", "add", "-b", "spike", str(tmp_path / "spike"), cwd=repo)
    _git("commit", "--allow-empty", "-m", "local", cwd=tmp_path / "spike")
    _git("worktree", "add", "--detach", str(tmp_path / "scratch"), "spike", cwd=repo)
    (repo / "kept").write_text("x")
    _git("add", "kept", cwd=repo)
    _git("commit", "-m", "kept", cwd=repo)
    _git("mv", "kept", "moved", cwd=repo)  # a rename carries its old name too: one change
    (repo / "f").write_text("x")
    _git("add", "f", cwd=repo)
    (tmp_path / "spike" / "new").write_text("untracked work is work")

    found = {w["path"].rsplit("/", 1)[1]: w for w in scan_worktrees([str(repo), str(repo)])}
    assert set(found) == {"repo", "spike", "scratch"}
    assert found["repo"]["dirty"] == 2 and found["repo"]["repo"] == "example-org/shop-api"
    assert found["spike"]["unpushed"] == 1 and found["spike"]["branch"] == "spike"
    assert found["spike"]["dirty"] == 1
    assert found["scratch"]["unpushed"] == 0  # detached: not a branch of work
    assert found["spike"]["active_at"] > 0


def test_endpoint_renders_from_cache(monkeypatch):
    loops = OpenLoops()
    loops.github, loops.fetched_at = github(node(13)), NOW - 60

    class Watcher:
        def __init__(self):
            self.states = [pane("%1", prs=[13])]

    monkeypatch.setattr(server.app.state, "loops", loops, raising=False)
    monkeypatch.setattr(server.app.state, "watcher", Watcher(), raising=False)
    monkeypatch.setattr(open_loops, "run_gh", lambda *a: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(open_loops.time, "time", lambda: NOW)
    body = TestClient(server.app).get("/api/open-loops").json()
    assert body["fetched_at"] == NOW - 60
    assert body["lanes"]["dropped"][0]["items"][0]["panes"][0]["pane_id"] == "%1"
