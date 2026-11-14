"""Teleport: a cloud session's link becomes one `claude --teleport` window, in the right
checkout, under the session's own name. The API reply here is invented, in both shapes the
service answers with."""

import io
import json
import subprocess
import urllib.error

import pytest
from fastapi.testclient import TestClient

import openbus.server as S
import openbus.tmux as T
from openbus import plan_usage, teleport

SID = "session_01ExampleSeedPlanner"
SESSION = {"id": "cse_01ExampleSeedPlanner", "title": "Seed  planner\nfrost dates", "config": {
    "outcomes": [
        {"type": "git_repository", "git_info": {
            "repo": "example-org/seed-planner", "branches": ["claude/frost-dates-a1b2c3"]}},
        {"type": "git_repository", "git_info": {
            "repo": "example-org/garden-site", "branches": ["claude/frost-dates-a1b2c3"]}}]}}
INFO = {"title": "Seed planner frost dates", "repo": "example-org/seed-planner",
        "branch": "claude/frost-dates-a1b2c3", "also": ["example-org/garden-site"]}


def test_the_id_is_read_from_a_link_or_given_bare():
    assert teleport.session_id(f"https://claude.ai/code/{SID}?tab=diff") == SID
    assert teleport.session_id("  cse_01Abc ") == "cse_01Abc"
    assert teleport.session_id("https://claude.ai/code/artifacts") is None


def test_describe_reads_both_reply_shapes():
    assert teleport.describe(SESSION) == INFO
    bare = {"title": SESSION["title"], "session_context": SESSION["config"]}
    assert teleport.describe({"response_shape": SESSION}) == teleport.describe(bare) == INFO
    assert teleport.describe({"title": None}) == {"title": "", "repo": "", "branch": "", "also": []}


def test_a_branch_that_is_not_a_plain_ref_is_never_fetched():
    odd = json.loads(json.dumps(SESSION))
    odd["config"]["outcomes"][0]["git_info"]["branches"] = ["--upload-pack=touch /tmp/x"]
    info = teleport.describe(odd)
    assert info["branch"] == "" and teleport.command(SID, info)[0] == "claude"


def test_the_command_fetches_the_branch_then_teleports_into_a_worktree():
    assert teleport.command(SID, INFO) == [
        "sh", "-c", teleport._FETCH, "claude/frost-dates-a1b2c3",
        "claude", "--teleport", SID, "--name", "Seed planner frost dates",
        "--worktree", "seed-planner-frost-dates-lanner"]
    # No repository: nothing to check out, so it runs where the window opens.
    assert teleport.command(SID, {**INFO, "repo": "", "branch": ""}) == [
        "claude", "--teleport", SID, "--name", "Seed planner frost dates"]


def _repo(path, origin):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return str(path)


def test_checkout_matches_origin_whatever_its_scheme(tmp_path):
    ssh = _repo(tmp_path / "planner", "git@github.com:Example-Org/seed-planner.git")
    web = _repo(tmp_path / "site", "https://github.com/example-org/garden-site")
    (tmp_path / "planner" / "docs").mkdir()
    dirs = [str(tmp_path), web, str(tmp_path / "planner" / "docs")]
    assert teleport.checkout("example-org/seed-planner", dirs) == ssh
    assert teleport.checkout("example-org/garden-site", dirs) == web
    assert teleport.checkout("example-org/planner", dirs) is None
    # The same owner/name on another host is another repository.
    _repo(tmp_path / "fork", "https://github.com.example.net/example-org/tide-tables.git")
    assert teleport.checkout("example-org/tide-tables", [str(tmp_path / "fork")]) is None


def _signed_in(tmp_path, monkeypatch, reply):
    """A config dir holding a token, and the API answering `reply` (or raising it)."""
    (tmp_path / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "t0ken", "expiresAt": 4e12}}))
    calls = []

    def open_(request, timeout):
        calls.append(request)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(json.dumps(reply).encode())
    monkeypatch.setattr(plan_usage.OPENER, "open", open_)
    return calls


def test_fetch_asks_for_the_session_with_the_accounts_token(tmp_path, monkeypatch):
    calls = _signed_in(tmp_path, monkeypatch, SESSION)
    assert teleport.fetch(tmp_path, SID, 0) == SESSION
    assert calls[0].full_url == teleport.API + SID
    assert calls[0].get_header("Authorization") == "Bearer t0ken"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "server_path", lambda: "/nonexistent")
    monkeypatch.setattr(S.shutil, "which", lambda c, path=None: c)
    monkeypatch.setattr(T, "list_panes", lambda: [T.Pane("work", "1", "w", "0", "%9", "bash", "t")])
    monkeypatch.setattr(S.accounts, "session_defaults",
                        lambda s: {"work": {"claude": str(tmp_path)}})
    monkeypatch.setattr(S.agent_history, "recent_dirs", lambda: [str(tmp_path / "planner")])
    monkeypatch.setattr(S, "_audit", lambda *a, **k: None)
    return TestClient(S.app)


def test_teleport_opens_the_window_in_the_sessions_checkout(client, tmp_path, monkeypatch):
    _signed_in(tmp_path, monkeypatch, SESSION)
    here = _repo(tmp_path / "planner", "https://github.com/example-org/seed-planner.git")
    calls = []
    monkeypatch.setattr(T, "_run", lambda argv: (calls.append(argv), "%42")[1])
    r = client.post("/api/teleport", json={"session": "work", "cloud": f"claude.ai/code/{SID}"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "pane_id": "%42", "repo": "example-org/seed-planner",
                        "also": ["example-org/garden-site"]}
    argv = calls[0]
    assert argv[argv.index("-c") + 1] == here and argv[argv.index("-t") + 1] == "work:"
    assert argv[argv.index("--") + 1:] == teleport.command(SID, INFO)


def test_a_title_ending_in_a_semicolon_does_not_end_the_tmux_command(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_run", lambda argv: (calls.append(argv), "%1")[1])
    T.new_window("work", "Sort the seed list;", ["claude"])
    assert calls[0][calls[0].index("-n") + 1] == "Sort the seed list\\;"


@pytest.mark.parametrize(("body", "reply", "status", "says"), [
    ({"cloud": "the one from this morning"}, SESSION, 422, "link"),
    ({"cloud": SID, "session": "nope"}, SESSION, 404, "session not found"),
    ({"cloud": SID, "account": "/no/such/account"}, SESSION, 404, "unknown account"),
    ({"cloud": SID}, urllib.error.HTTPError("u", 404, "", {}, None), 404, "no cloud session"),
    ({"cloud": SID}, urllib.error.HTTPError("u", 500, "", {}, None), 502, "500"),
    ({"cloud": SID}, urllib.error.HTTPError("u", 401, "", {}, None), 409, "sign-in"),
    ({"cloud": SID}, urllib.error.URLError("down"), 502, "reached"),
    ({"cloud": SID}, PermissionError("token expired"), 409, "sign-in"),
    ({"cloud": SID}, SESSION, 409, "No checkout of example-org/seed-planner"),  # none cloned
])
def test_a_refusal_opens_no_window(client, tmp_path, monkeypatch, body, reply, status, says):
    _signed_in(tmp_path, monkeypatch, reply)
    monkeypatch.setattr(T, "_run", lambda argv: pytest.fail("must not run"))
    r = client.post("/api/teleport", json={"session": "work", **body})
    assert r.status_code == status and says in r.json()["detail"], r.text
