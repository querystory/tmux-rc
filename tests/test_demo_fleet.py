"""The demo fleet behind README screenshots must stay fictional and complete.

A cheap guard, not a proof: it catches the ways real data has leaked into screenshots
before (home paths, real project names, accounts) and anything shaped like a credential.
"""
import json
import re

import pytest

from openbus.history import History
from openbus.open_loops import OpenLoops
from openbus.plan_usage import PlanUsage
from scripts import demo_fleet as demo
from tests.test_public_data import IDENTITY

# A stranger's fleet: not even this project's own names or a home path.
PRIVATE = re.compile(rf"/home/|querystory|qs-app|tmux-rc|{IDENTITY}", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _no_keyword_map(monkeypatch):
    """A checkout's own TMUXRC_WORKSTREAMS must not rename the demo's workstreams."""
    monkeypatch.delenv("TMUXRC_WORKSTREAMS", raising=False)


def _loops() -> dict:
    return demo.seed_loops(OpenLoops()).report(demo.fleet(), demo.NOW)


def _everything() -> str:
    fleet = demo.fleet()
    return json.dumps([fleet, demo.events_log(), [demo.capture(p) for p in fleet], _loops(),
                       demo.restore_snapshot(), demo.SCRATCH, demo.SCRATCH_WRITERS])


def test_fleet_contains_nothing_private():
    assert not PRIVATE.findall(_everything())


def test_links_point_only_at_example_org():
    urls = re.findall(r"https?://[^\s\"\\]+", _everything())
    assert urls
    assert all(u.startswith("https://github.com/example-org/") for u in urls)


def test_fleet_covers_every_state_the_ui_draws():
    fleet = demo.fleet()
    assert len({p["pane_id"] for p in fleet}) == len(fleet)
    acts = {(p["activity"], p.get("waiting_on")) for p in fleet}
    assert {("running", None), ("idle", None), ("compacting", None),
            ("waiting", "user"), ("waiting", "external")} <= acts
    assert 1 <= sum(p.get("waiting_on") == "user" for p in fleet) <= 2  # a calm fleet
    assert {p["tool"] for p in fleet} >= {"claude", "codex", "opencode", "omp", "gemini", "shell"}


def test_history_fills_the_chart(tmp_path):
    tmp_path.chmod(0o700)
    history = demo.seed_history(History(tmp_path / "h.sqlite3"))
    day = history.query("24h", now=demo.NOW)["samples"]
    assert all(s["source"] == "daemon" for s in day)
    assert day[-1]["n"][1] == sum(p["activity"] == "running" for p in demo.fleet())


def test_plan_usage_reads_like_a_typical_day(tmp_path):
    tmp_path.chmod(0o700)
    report = demo.seed_usage(PlanUsage(History(tmp_path / "h.sqlite3"))).report(demo.NOW)
    assert not PRIVATE.findall(json.dumps(report))
    by = {(a["provider"], w["window"]): w for a in report for w in a["windows"]}
    assert set(by) == {("claude", "5h"), ("claude", "7d"), ("claude", "7d Fable"),
                       ("codex", "7d")}  # weekly-only Codex
    assert 90 <= by["claude", "7d"]["projected"] < 100  # amber, not out before the reset
    assert by["codex", "7d"]["limit_at"]  # red: out before the reset, which still shows
    assert [a["dirs"] for a in report] == [["~/.claude"], ["~/.codex"]]  # one account each


def test_the_two_account_state_is_fictional_and_splits_the_sessions(tmp_path):
    tmp_path.chmod(0o700)
    report = demo.seed_usage(PlanUsage(History(tmp_path / "h.sqlite3")), two=True).report(demo.NOW)
    profiles = demo.accounts(two=True)
    assert not PRIVATE.findall(json.dumps([report, profiles]))
    dirs = sorted(p["dir"] for p in profiles["profiles"])
    assert sorted(d for a in report for d in a["dirs"]) == dirs
    personal, work = (next(p for p in profiles["profiles"] if p["dir"] == d)
                      for d in ("~/.claude-personal", "~/.claude"))
    assert personal["panes"] and work["panes"] and not set(personal["panes"]) & set(work["panes"])
    assert demo.accounts()["sessions"] == {}  # one account each: no session defaults asked
    # The add-account shots: a stub sign-in screen, and Demo signing in a little later.
    assert not PRIVATE.findall(demo.capture(demo.sign_in_pane()))
    for since, signed_in in ((0, False), (99, True)):
        listed = demo.accounts(two=True, signing_in=since)["profiles"]
        assert [p["signed_in"] for p in listed if p["name"] == "Demo"] == [signed_in]


def test_open_loops_fill_every_lane_and_grouping_signal():
    lanes = _loops()["lanes"]
    reasons = {r for groups in lanes.values() for g in groups for i in g["items"]
               for r in i.get("reasons", [])}
    assert {"approved", "review_requested", "no_reviewer", "checks_failed", "base_merged",
            "conflicts", "stale", "no_pane"} <= reasons
    assert {(g["workstream"] or {}).get("id") for g in lanes["waiting"]} >= {
        "example-org/shop-api#409", "workstream:build-cache"}  # a stack and a label
    assert any(i["panes"] for g in lanes["waiting"] for i in g["items"] if i["kind"] == "pr")


def test_restore_offer_shows_every_default_and_a_refusal(tmp_path, monkeypatch):
    """Through the real offer, as the demo server serves it: agents ticked, a long-idle one
    and a shell not, one already running refused."""
    from openbus import restore
    for name, seam in demo.restore_seams().items():
        monkeypatch.setattr(restore, name, seam)
    offer = restore.offer(demo.seed_restore(History(tmp_path / "h.sqlite3")))
    panes = {p["title"]: (p["default"], p["blocked"])
             for w in offer["windows"].values() for p in w["panes"]}
    assert panes.pop("Tokenizer v2 benchmarks") == (False, None)
    assert panes.pop(None) == (False, None)
    assert panes.pop("Eval harness flakes") == (False, "it's already running")
    assert set(panes.values()) == {(True, None)}
