"""The demo fleet behind README screenshots must stay fictional and complete.

A cheap guard, not a proof: it catches the ways real data has leaked into screenshots
before (home paths, real project names, accounts) and anything shaped like a credential.
"""
import json
import re

from openbus.history import History
from openbus.open_loops import OpenLoops
from openbus.plan_usage import PlanUsage
from scripts import demo_fleet as demo

PRIVATE = re.compile(r"/home/|/Users/|querystory|qs-app|tmux-rc|shapor"
                     r"|[\w.+-]+@[\w-]+\.[\w.-]+|sk-[A-Za-z0-9]|ghp_|gho_|github_pat_"
                     r"|AKIA[0-9A-Z]{8}|AIza[0-9A-Za-z_-]{10}"
                     r"|xox[bp]-|BEGIN [A-Z ]*PRIVATE KEY", re.IGNORECASE)


def _loops() -> dict:
    return demo.seed_loops(OpenLoops()).report(demo.fleet(), demo.NOW)


def _everything() -> str:
    fleet = demo.fleet()
    return json.dumps([fleet, demo.events_log(), [demo.capture(p) for p in fleet], _loops()])


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
    styles = {p["question"]["answer_style"] for p in fleet if p.get("question")}
    assert {"text", "menu"} <= styles
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
    assert {a["label"] for a in report} == {None}  # one account each: provider names


def test_open_loops_fill_every_lane_and_grouping_signal():
    lanes = _loops()["lanes"]
    reasons = {r for groups in lanes.values() for g in groups for i in g["items"]
               for r in i.get("reasons", [])}
    assert {"approved", "review_requested", "no_reviewer", "checks_failed", "base_merged",
            "conflicts", "stale", "no_pane"} <= reasons
    assert {(g["workstream"] or {}).get("id") for g in lanes["waiting"]} >= {
        "example-org/shop-api#409", "workstream:build-cache"}  # a stack and a label
    assert any(i["panes"] for g in lanes["waiting"] for i in g["items"] if i["kind"] == "pr")
