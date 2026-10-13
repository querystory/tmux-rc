"""Agent accounts: discovery and naming from non-secret files, the profile store, the
add flow's dir and links, and launching a pane on an account. Every config dir here is a
fictional one under tmp_path; no credentials file is ever written or read."""
import json
import stat
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import openbus.server as S
import openbus.tmux as T
from openbus import accounts, plan_usage


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
