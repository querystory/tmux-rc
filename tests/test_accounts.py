"""Agent accounts: discovery and naming from non-secret files, the profile store, the
add flow's dir and links, and launching a pane on an account. Every config dir here is a
fictional one under tmp_path; no credentials file is ever written or read."""
import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import openbus.server as S
import openbus.tmux as T
from openbus import accounts, expunge, plan_usage


def _signed_in(path, uuid, **fields):
    """A Claude config dir whose .claude.json names its account (non-secret fields only)."""
    path.mkdir(parents=True, exist_ok=True)
    meta = path.parent / ".claude.json" if path.name == ".claude" else path / ".claude.json"
    meta.write_text(json.dumps({"oauthAccount": {"accountUuid": uuid, **fields}}))
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _signed_in(home / ".claude", "u-work", organizationName="Example Org",
               emailAddress="dev@example.com", organizationType="claude_team")
    _signed_in(home / ".claude-personal", "u-me", emailAddress="sam@example.net",
               organizationName="sam@example.net's Organization", organizationType="claude_max")
    (home / ".claude-old").mkdir()  # exists, nobody signed in: not an account
    (home / ".codex2").mkdir()
    (home / ".codex2" / "auth.json").touch()
    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".codex" / "auth.json").touch()  # present, never read
    return home


def _by_dir(home):
    return {p["tilde"]: p for p in accounts.profiles()}


def test_discovery_names_each_account_from_its_non_secret_info(home):
    found = _by_dir(home)
    assert sorted(found) == ["~/.claude", "~/.claude-personal", "~/.codex", "~/.codex2"]
    assert (found["~/.claude"]["name"], found["~/.claude"]["plan"]) == ("Example Org", "Team")
    # A personal plan's organisation is named for its owner: the email alone, not "dev".
    assert found["~/.claude-personal"]["name"] == "sam@example.net"
    assert found["~/.codex"]["name"] == "Codex"
    assert found["~/.claude"]["default"] and not found["~/.claude-personal"]["default"]
    colors = [p["color"] for p in found.values()]
    assert len(set(colors)) == 4
    assert [p["color"] for p in accounts.profiles()] == colors  # kept, not recomputed


def test_a_label_names_the_account_and_forgetting_only_hides_it(home, monkeypatch):
    shown = {None: {"CLAUDE_CONFIG_DIR": str(home / ".claude-personal")}}
    monkeypatch.setattr(T, "list_panes", lambda: [T.Pane("work", "1", "w", "0", "%9", "bash", "t")])
    monkeypatch.setattr(T, "show_env", lambda session=None: shown.get(session, {}))
    calls = []
    monkeypatch.setattr(T, "_run", lambda argv, **_: calls.append(argv) or "")
    personal = str(home / ".claude-personal")
    accounts.edit(personal, "Personal")
    assert _by_dir(home)["~/.claude-personal"]["name"] == "Personal"
    accounts.edit(personal, forget=True)
    assert "~/.claude-personal" not in _by_dir(home)
    assert (home / ".claude-personal" / ".claude.json").exists()  # nothing deleted
    # A session that defaulted to it falls back to the default dir.
    assert calls == [["set-environment", "-g", "-u", "CLAUDE_CONFIG_DIR"],  # global too
                     ["set-environment", "-t", "=work", "-r", "CLAUDE_CONFIG_DIR"]]
    with pytest.raises(ValueError, match="default"):
        accounts.edit(str(home / ".claude"), forget=True)


def test_adding_creates_a_private_dir_and_links_only_shared_files(home):
    (home / ".claude" / "CLAUDE.md").write_text("instructions")
    (home / ".claude" / "skills").mkdir()
    added = accounts.add("claude", "~/.claude-side", "Side", ["CLAUDE.md", "skills", "hooks"])
    side = home / ".claude-side"
    assert added == {"dir": str(side), "existed": False, "linked": ["CLAUDE.md", "skills"]}
    assert stat.S_IMODE(side.stat().st_mode) == 0o700
    assert (side / "CLAUDE.md").resolve() == home / ".claude" / "CLAUDE.md"
    assert not (side / "hooks").exists()  # nothing to link
    pending = _by_dir(home)["~/.claude-side"]
    assert (pending["name"], pending["signed_in"]) == ("Side", False)
    for bad in ([".credentials.json"], [".claude.json"], ["projects"]):
        with pytest.raises(ValueError, match="not shareable"):
            accounts.add("claude", "~/.claude-other", "", bad)
    with pytest.raises(FileExistsError):
        accounts.add("claude", "~/.claude-personal", "", [])
    with pytest.raises(FileExistsError):  # the same dir, spelled another way
        accounts.add("claude", f"{home}//.claude-side/../.claude-personal/", "", [])
    # An old dir nobody signed in to is reused as it is.
    assert accounts.add("claude", "~/.claude-old", "Old", [])["existed"] is True


def test_a_removed_variable_reads_as_empty(monkeypatch):
    monkeypatch.setattr(T, "_run", lambda argv, **_: "-CLAUDE_CONFIG_DIR\nPATH=/bin\n")
    assert T.show_env("work") == {"CLAUDE_CONFIG_DIR": "", "PATH": "/bin"}


def test_only_a_personal_plans_own_organisation_is_shortened(home):
    _signed_in(home / ".claude-team", "u-team", organizationName="Jane's Organization",
               emailAddress="dev@example.org")
    names = {p["tilde"]: p["name"] for p in accounts.profiles()}
    assert names["~/.claude-team"] == "Jane's Organization"
    assert names["~/.claude-personal"] == "sam@example.net"


def test_a_panes_dir_in_another_spelling_is_one_account(home):
    alias = home / ".claude-personal" / ".." / ".claude-personal"
    listed = [p["tilde"] for p in accounts.profiles([("claude", alias)])]
    assert listed.count("~/.claude-personal") == 1


def test_a_live_pane_says_its_dirs_provider(home):
    (home / ".claude-work").mkdir()
    (home / ".claude-work" / "auth.json").touch()  # a Codex home named like a Claude one
    listed = accounts.profiles([("codex", home / ".claude-work")])
    found = {p["tilde"]: p["provider"] for p in listed}
    assert found["~/.claude-work"] == "codex"


def test_an_added_dirs_provider_is_kept_whatever_its_name(home):
    accounts.add("codex", "~/.claude-work", "Work", [])
    (home / ".claude-work" / "auth.json").touch()
    work = _by_dir(home)["~/.claude-work"]
    assert (work["provider"], work["signed_in"]) == ("codex", True)


def test_two_people_of_one_organisation_are_told_apart(home):
    for name, who in (("a", "Sam"), ("b", "Alex"), ("c", "Sam"), ("d", "Alex")):
        _signed_in(home / f".claude-{name}", f"u-{name}", organizationName="Example Org",
                   displayName=who, emailAddress=f"{name}@example.com")
    accounts.edit(str(home / ".claude-c"), "Mine")
    names = {p["tilde"]: p["name"] for p in accounts.profiles()}
    assert names["~/.claude-a"] == "Example Org · Sam"  # c's label leaves Sam unique
    assert names["~/.claude-c"] == "Mine"
    assert names["~/.claude"] == "Example Org · dev"  # no display name: the email's local part
    # Two Alexes: the full email.
    assert (names["~/.claude-b"], names["~/.claude-d"]) == (
        "Example Org · b@example.com", "Example Org · d@example.com")
    assert "_more" not in accounts.profiles()[0]


def test_a_forgotten_dir_comes_back_when_added_again(home):
    accounts.edit(str(home / ".claude-personal"), forget=True)
    accounts.add("claude", str(home / ".claude-personal"), "Personal", [])
    assert _by_dir(home)["~/.claude-personal"]["name"] == "Personal"


def test_a_command_is_pinned_to_its_account(home):
    assert accounts.command("claude", str(home / ".claude-personal"), "claude") == [
        "env", f"CLAUDE_CONFIG_DIR={home}/.claude-personal", "sh", "-c", "claude"]
    # The default dir's .claude.json lives beside it, so the variable is unset, not set.
    assert accounts.command("claude", str(home / ".claude"), ["claude", "--resume", "x"]) == [
        "env", "-u", "CLAUDE_CONFIG_DIR", "claude", "--resume", "x"]


def test_report_maps_panes_and_asks_session_defaults_only_with_several(home, monkeypatch):
    envs = {"1": {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude-personal")},
            "2": {"HOME": str(home)}}
    monkeypatch.setattr(plan_usage, "pane_env", lambda pid, tool: envs[pid])
    shown = {None: {"CLAUDE_CONFIG_DIR": str(home / ".claude-personal")}, "work": {}}
    monkeypatch.setattr(T, "show_env", lambda session=None: shown.get(session, {}))
    states = [{"pane_id": "%1", "tool": "claude", "session": "work"},
              {"pane_id": "%2", "tool": "claude", "session": "work"}]
    report = accounts.report(states, lambda pane_id: pane_id[1:])
    panes = {p["tilde"]: p["panes"] for p in report["profiles"]}
    assert panes == {"~/.claude": ["%2"], "~/.claude-personal": ["%1"], "~/.codex": [],
                     "~/.codex2": []}
    # The global environment applies where the session sets nothing.
    assert report["sessions"]["work"]["claude"] == str(home / ".claude-personal")
    assert report["sessions"][""]["claude"] == str(home / ".claude-personal")  # a new session
    shown["work"] = {"CLAUDE_CONFIG_DIR": f"{home}/.claude//../.claude-personal/"}  # an alias
    work = accounts.report(states, lambda pane_id: pane_id[1:])["sessions"]["work"]
    assert work["claude"] == str(home / ".claude-personal")
    # A session that removed it (-CLAUDE_CONFIG_DIR) sets nothing, like one that never set it.
    shown["work"] = {"CLAUDE_CONFIG_DIR": ""}  # how show_env reads "-CLAUDE_CONFIG_DIR"
    assert accounts.report(states, lambda pane_id: pane_id[1:])["sessions"]["work"] == {
        "claude": None, "codex": None}
    for extra in (".claude-personal", ".codex2"):
        accounts.edit(str(home / extra), forget=True)
    assert accounts.report(states, lambda pane_id: pane_id[1:])["sessions"] == {}


def test_plan_usage_meters_the_listed_accounts_not_the_daemons_env(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "elsewhere"))
    accounts.edit(str(home / ".claude-personal"), forget=True)
    usage = plan_usage.PlanUsage(None, profiles=accounts.metered)
    found = usage.discover([], lambda _: None)
    assert sorted(k for _, k in found) == [str(home / ".codex"), "u-work"]  # .codex2: no logs


@pytest.fixture
def client(home, monkeypatch):
    monkeypatch.delenv("TMUXRC_LAUNCHERS", raising=False)
    monkeypatch.setattr(S.shutil, "which", lambda c, path=None: c)
    monkeypatch.setattr(T, "server_path", lambda: "/nonexistent")
    monkeypatch.setattr(T, "list_panes", lambda: [T.Pane("work", "1", "w", "0", "%9", "bash", "t")])
    calls = []
    monkeypatch.setattr(T, "_run", lambda argv, **_: (calls.append(argv), "%42")[1])
    c = TestClient(S.app)
    c.calls = calls
    return c


def test_a_new_window_launches_on_the_picked_account(client, home):
    r = client.post("/api/windows", json={"session": "work", "launcher": "Claude",
                                          "account": str(home / ".claude-personal")})
    assert r.status_code == 200
    argv = next(a for a in client.calls if a[0] == "new-window")
    assert argv[argv.index("--") + 1:] == [
        "env", f"CLAUDE_CONFIG_DIR={home}/.claude-personal", "sh", "-c", "claude"]
    for launcher, account in (("Claude", home / ".claude-old"), ("Claude", home / ".codex"),
                              ("Shell", home / ".claude")):  # no account of its provider
        assert client.post("/api/windows", json={"session": "work", "launcher": launcher,
                                                 "account": str(account)}).status_code == 404


def test_the_session_default_and_the_sign_in_window(client, home):
    personal = {"session": "work", "dir": str(home / ".claude-personal")}
    assert client.post("/api/accounts/default", json=personal).status_code == 200
    assert ["set-environment", "-t", "=work", "CLAUDE_CONFIG_DIR",
            str(home / ".claude-personal")] in client.calls
    client.post("/api/accounts/default", json={"session": "work", "dir": str(home / ".claude")})
    assert ["set-environment", "-t", "=work", "-r", "CLAUDE_CONFIG_DIR"] in client.calls
    client.calls.clear()  # "Not set": cleared even with no standard account listed
    assert client.post("/api/accounts/default", json={"session": "work", "dir": "",
                                                      "provider": "codex"}).status_code == 200
    assert ["set-environment", "-t", "=work", "-r", "CODEX_HOME"] in client.calls
    assert client.post("/api/accounts/default", json={"session": "nope", "dir": "",
                                                      "provider": "codex"}).status_code == 404
    added = client.post("/api/accounts", json={"provider": "codex", "dir": "~/.codex-work",
                                               "label": "Work"}).json()
    assert client.post("/api/accounts/login",
                       json={"session": "work", "dir": added["dir"]}).status_code == 200
    argv = next(a for a in client.calls if a[0] == "new-window")
    assert argv[argv.index("-n") + 1] == "sign in: Work"
    assert argv[argv.index("--") + 1:] == [
        "env", f"CODEX_HOME={home}/.codex-work", "codex", "login"]
    assert client.post("/api/accounts", json={"provider": "claude",
                                              "dir": "~/.claude-personal"}).status_code == 409


def test_an_unfinished_sign_in_is_resumed_not_refused(home):
    first = accounts.add("claude", "~/.claude-demo", "Demo", [])
    (home / ".claude-demo").rmdir()  # abandoned, and its dir gone since
    again = accounts.add("claude", "~/.claude-demo", "Demo", [])
    assert (again["dir"], again["existed"]) == (first["dir"], False)
    assert (home / ".claude-demo").is_dir()
    assert [p["name"] for p in accounts.profiles() if p["tilde"] == "~/.claude-demo"] == ["Demo"]


def test_only_the_sign_in_window_it_opened_can_be_closed(client, home, monkeypatch):
    monkeypatch.setattr(T, "pane_pid", lambda pane_id: "4242")
    added = client.post("/api/accounts", json={"provider": "claude", "dir": "~/.claude-demo",
                                               "label": "Demo"}).json()
    opened = client.post("/api/accounts/login",
                         json={"session": "work", "dir": added["dir"]}).json()
    assert (opened["pane_id"], opened["pid"]) == ("%42", "4242")
    client.calls.clear()
    for pane_id, pid in (("%9", "4242"), ("%42", "1"), ("%42", "")):  # not that window
        assert client.post("/api/accounts/login/close",
                           json={"pane_id": pane_id, "pid": pid}).status_code == 404
    assert "kill-window" not in str(client.calls)  # refused before any window is touched
    monkeypatch.setattr(T, "find_pane",
                        lambda pane_id: T.Pane("work", "3", "w", "0", pane_id, "claude", "t"))
    monkeypatch.setattr(S.app.state, "watcher", SimpleNamespace(drop_panes=lambda ids: None),
                        raising=False)
    monkeypatch.setattr(T, "_run", lambda argv, **_: (client.calls.append(argv), "%42 4242")[1])
    assert client.post("/api/accounts/login/close",
                       json={"pane_id": "%42", "pid": "4242"}).status_code == 200
    [kill] = [c for c in client.calls if c[0] == "if-shell"]
    assert kill[:4] == ["if-shell", "-F", "-t", "%42"] and "#{pane_pid},4242" in kill[4]
    assert "kill-session" not in str(kill) and "kill-server" not in str(kill)


def test_a_custom_launcher_names_its_provider(client, home, monkeypatch):
    monkeypatch.setenv("TMUXRC_LAUNCHERS", json.dumps([
        {"label": "Opus", "command": "claude --model opus", "icon": "/img/opus.png",
         "provider": "claude"}]))
    assert client.get("/api/launchers").json()["launchers"][0]["provider"] == "claude"
    r = client.post("/api/windows", json={"session": "work", "launcher": "Opus",
                                          "account": str(home / ".claude-personal")})
    assert r.status_code == 200
    argv = next(a for a in client.calls if a[0] == "new-window")
    assert argv[argv.index("--") + 1:][:2] == ["env", f"CLAUDE_CONFIG_DIR={home}/.claude-personal"]


SID = "0f1e2d3c-4b5a-4968-8776-655443322110"


@pytest.fixture
def moving(client, home, monkeypatch):
    """A fictional Claude session on ~/.claude, as Expunge's identify would find it."""
    src = home / ".claude"
    (src / f"projects/-p/{SID}/subagents").mkdir(parents=True)
    (src / f"projects/-p/{SID}.jsonl").write_text('{"type": "user"}\n')
    (src / f"projects/-p/{SID}/subagents/agent-1.jsonl").write_text("{}\n")
    (src / f"file-history/{SID}").mkdir(parents=True)
    (src / f"session-env/{SID}").mkdir(parents=True)  # rebuilt on start: not copied
    session = expunge.Session("claude", SID, src, home / "agent-history", os.getpid(), "1")
    pane = T.Pane("work", "1", "w", "0", "%9", "claude", "t")
    def identify(_pane, _birth, expected):  # as Expunge's, bound to the session named
        if expected != SID:
            raise S.HTTPException(409, "the pane runs another session now")
        return pane, session, ([], [])
    monkeypatch.setattr(S, "_pane_session", identify)
    return lambda account, **flags: client.post(
        "/api/panes/%9/move",
        json={"birth": "1", "session_id": SID, "dir": str(home / account), **flags})


def test_move_copies_the_session_and_resumes_it_on_the_other_account(client, home, moving):
    r = moving(".claude-personal")
    assert r.status_code == 200, r.text
    dst = home / ".claude-personal"
    assert (dst / f"projects/-p/{SID}.jsonl").read_text() == '{"type": "user"}\n'
    assert (dst / f"projects/-p/{SID}/subagents/agent-1.jsonl").exists()
    assert (dst / f"file-history/{SID}").is_dir() and not (dst / "session-env").exists()
    assert (home / f".claude/projects/-p/{SID}.jsonl").exists()  # a copy: the original stays
    argv = next(a for a in client.calls if a[0] == "new-window")
    assert argv[argv.index("-c") + 1] == os.getcwd() and argv[argv.index("-t") + 1] == "work:"
    assert argv[argv.index("--") + 1:] == [
        "env", f"CLAUDE_CONFIG_DIR={dst}", "claude", "--resume", SID]
    assert "kill" not in str(client.calls)  # the source pane is never touched
    assert moving(".claude-personal").status_code == 409  # already there: nothing overwritten
    for account in (".claude-old", ".codex2"):  # signed out, another provider's
        assert moving(account).status_code == 404
    assert moving(".claude-personal", session_id="another").status_code == 409
    r = moving(".claude")  # its own account
    assert r.status_code == 409 and r.json()["detail"] == "the session is already on that account"


def test_an_older_copy_is_replaced_or_resumed_only_when_asked(client, home, moving):
    dst = home / ".claude-personal"
    (dst / f"projects/-p/{SID}/subagents").mkdir(parents=True)
    (dst / f"projects/-p/{SID}/subagents/stale.jsonl").write_text("{}\n")
    (dst / f"projects/-p/{SID}.jsonl").write_text("old\n")
    (dst / "projects/-p/other.jsonl").write_text("another session\n")
    r = moving(".claude-personal")
    assert r.status_code == 409 and r.json()["detail"]["copied"] > 0  # the sheet's choice
    assert (dst / f"projects/-p/{SID}.jsonl").read_text() == "old\n"
    r = moving(".claude-personal", resume_existing=True)
    assert r.status_code == 200 and r.json()["copied"] == []
    assert (dst / f"projects/-p/{SID}.jsonl").read_text() == "old\n"
    assert moving(".claude-personal", replace=True).status_code == 200
    assert (dst / f"projects/-p/{SID}.jsonl").read_text() == '{"type": "user"}\n'
    assert not (dst / f"projects/-p/{SID}/subagents/stale.jsonl").exists()
    assert (dst / "projects/-p/other.jsonl").exists()  # only that session's files go
    argv = [a for a in client.calls if a[0] == "new-window"][-1]
    assert argv[-2:] == ["--resume", SID]
    _signed_in(home / ".claude-empty", "u-e")
    (home / f".claude-empty/file-history/{SID}").mkdir(parents=True)  # no transcript: no resume
    assert moving(".claude-empty", resume_existing=True).status_code == 409


@pytest.mark.parametrize("sign", ["command line", "registration", "unreadable", "open file"])
def test_a_copy_running_on_the_target_is_never_replaced_or_resumed_twice(home, moving, sign):
    assert moving(".claude-personal").status_code == 200
    dst = home / ".claude-personal"
    code = {"command line": "import time; time.sleep(30)",
            "registration": "import time; time.sleep(30)",
            "unreadable": "import time; time.sleep(30)",
            "open file": f"import time; f = open({str(dst / f'projects/-p/{SID}.jsonl')!r}); "
                         "print(flush=True); time.sleep(30)"}[sign]
    argv = [sys.executable, "-c", code, *([SID] if sign == "command line" else [])]
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(dst)}
    agent = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE)
    try:
        if sign == "registration":  # Claude's own record of the session, under the agent's pid
            start = expunge._stat(agent.pid)[19]
            (dst / "sessions").mkdir()
            (dst / f"sessions/{agent.pid}.json").write_text(json.dumps(
                {"pid": agent.pid, "sessionId": SID, "procStart": start}))
        if sign == "unreadable":  # half written, or whatever: it could be this session's
            (dst / "sessions").mkdir()
            (dst / "sessions/1.json").write_text("{")
        if sign == "open file":
            agent.stdout.readline()  # open by now
        for flags in ({}, {"replace": True}, {"resume_existing": True}):
            r = moving(".claude-personal", **flags)  # the sheet's first tap included
            assert r.status_code == 409 and "running on that account" in r.json()["detail"]
    finally:
        agent.kill()
        agent.wait()
    (dst / "sessions/1.json").unlink(missing_ok=True)
    assert moving(".claude-personal", resume_existing=True).status_code == 200
    assert moving(".claude-personal", replace=True, resume_existing=True).status_code == 400


def test_a_bind_mounted_alias_of_its_own_dir_is_refused(home, moving, monkeypatch):
    monkeypatch.setattr(accounts.Path, "samefile", lambda *_: True)  # one dir, two mounts
    r = moving(".claude-personal", replace=True)
    assert r.status_code == 409 and "already on that account" in r.json()["detail"]
    session = expunge.Session("claude", SID, home / ".claude", home / "agent-history", 0, "")
    with pytest.raises(ValueError, match="inside"):
        accounts.copy_session(session, home / ".claude-personal", replace=True)
    assert (home / f".claude/projects/-p/{SID}.jsonl").exists()


def test_a_move_writes_nothing_through_a_link_out_of_the_account(home, moving, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (home / ".claude-personal/projects").symlink_to(tmp_path / "elsewhere")
    r = moving(".claude-personal")
    assert r.status_code == 409 and "links outside" in r.json()["detail"]
    assert not list((tmp_path / "elsewhere").iterdir())
    (tmp_path / "elsewhere/-p").mkdir()  # another account's copy, seen through the link
    (tmp_path / f"elsewhere/-p/{SID}.jsonl").write_text("theirs\n")
    for flags in ({"replace": True}, {"resume_existing": True}):
        r = moving(".claude-personal", **flags)
        assert r.status_code == 409 and "links outside" in r.json()["detail"]
    assert (tmp_path / f"elsewhere/-p/{SID}.jsonl").read_text() == "theirs\n"


def test_a_move_whose_agent_cannot_start_copies_nothing(home, moving, monkeypatch):
    monkeypatch.setattr(S.shutil, "which", lambda *_a, **_k: None)
    assert moving(".claude-personal").status_code == 400
    assert not (home / ".claude-personal/projects").exists()


def test_exit_types_the_agents_own_exit_into_that_pane_only(client, moving, monkeypatch):
    sent = []
    monkeypatch.setattr(T, "send_keys", lambda *a, **k: sent.append((a, k)))
    def exit_():
        return client.post("/api/panes/%9/exit", json={"birth": "1", "session_id": SID})
    assert exit_().status_code == 200
    (args, kw), = sent
    assert args == ("%9", "/exit") and kw["expected_pid"] == ""
    def gone(*_a):
        raise S.HTTPException(409, "the agent exited")
    def changed(*_a, **k):  # the agent exited while the send waited for the pane's lock
        monkeypatch.setattr(S, "_pane_session", gone)
        k["guard"]()
    monkeypatch.setattr(T, "send_keys", changed)
    assert exit_().status_code == 409
    assert "kill" not in str(client.calls)


def test_a_move_refuses_nested_dirs_and_a_session_with_no_transcript(home, moving):
    nested = _signed_in(home / ".claude/inner", "u-in")
    accounts.add("claude", str(nested), "Inner", [])
    assert moving(".claude/inner").status_code == 409  # copytree would chase its own copy
    (home / f".claude/projects/-p/{SID}.jsonl").unlink()  # registered, nothing written yet
    assert moving(".claude-personal").status_code == 409
    assert not (home / ".claude-personal/projects").exists()


def test_a_record_still_being_written_is_left_out_of_the_copy(home, moving):
    (home / f".claude/projects/-p/{SID}.jsonl").write_text('{"type": "user"}\n{"type": "assi')
    (home / f".claude/projects/-p/{SID}/subagents/agent-1.jsonl").write_text('{}\n{"par')
    assert moving(".claude-personal").status_code == 200
    dst = home / ".claude-personal/projects/-p"
    assert (dst / f"{SID}.jsonl").read_text() == '{"type": "user"}\n'
    assert (dst / f"{SID}/subagents/agent-1.jsonl").read_text() == "{}\n"


def test_a_move_that_fails_partway_leaves_nothing_behind(home, moving, monkeypatch):
    def broken(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(accounts.shutil, "copytree", broken)
    assert moving(".claude-personal").status_code == 500
    assert not list((home / ".claude-personal").glob(f"**/{SID}*"))


def test_a_codex_thread_moves_with_its_resumed_segments(home):
    day = home / ".codex/sessions/2026/10/09"
    day.mkdir(parents=True)
    for name in (f"T09-00-00-{SID}.jsonl", f"T10-00-00-{SID}_2.jsonl"):
        (day / f"rollout-2026-10-09{name}").write_text("{}\n")
    session = expunge.Session("codex", SID, home / ".codex", home / "agent-history", 0, "")
    assert len(accounts.copy_session(session, home / ".codex2")) == 2
    assert len(list((home / ".codex2/sessions/2026/10/09").iterdir())) == 2
