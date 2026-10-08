"""The demo fleet behind README screenshots must stay fictional and complete.

A cheap guard, not a proof: it catches the ways real data has leaked into screenshots
before (home paths, real project names, accounts) and anything shaped like a credential.
"""
import json
import re

from openbus.history import History
from openbus.plan_usage import PlanUsage
from scripts import demo_fleet as demo

PRIVATE = re.compile(r"/home/|/Users/|querystory|qs-app|tmux-rc|shapor"
                     r"|[\w.+-]+@[\w-]+\.[\w.-]+|sk-[A-Za-z0-9]|ghp_|gho_|github_pat_"
                     r"|AKIA[0-9A-Z]{8}|AIza[0-9A-Za-z_-]{10}"
                     r"|xox[bp]-|BEGIN [A-Z ]*PRIVATE KEY", re.IGNORECASE)


def _everything() -> str:
    fleet = demo.fleet()
    return json.dumps([fleet, demo.events_log(), [demo.capture(p) for p in fleet]])


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


def test_plan_usage_draws_every_state(tmp_path):
    tmp_path.chmod(0o700)
    report = demo.seed_usage(PlanUsage(History(tmp_path / "h.sqlite3"))).report(demo.NOW)
    assert not PRIVATE.findall(json.dumps(report))
    windows = [w for a in report for w in a["windows"]]
    assert any(w["limit_at"] for w in windows)  # projected past 100% before the reset
    assert any(w["pct"] >= 90 for w in windows)
    assert {a["label"] for a in report} == {"dev", "ops", None}  # two Claudes, one Codex
    assert any(a["windows"] and all(w["window"] != "5h" for w in a["windows"]) for a in report)
