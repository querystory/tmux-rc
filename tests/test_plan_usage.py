"""Plan-limit sources, account discovery, and the projection the trend line draws."""
import json
from datetime import UTC, datetime

import pytest

from openbus import plan_usage
from openbus.history import History
from openbus.plan_usage import PlanUsage, claude_samples, codex_samples, project, read_codex

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp()


def _event(rate_limits, stamp="2026-10-08T12:00:00Z"):
    return {"timestamp": stamp, "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": rate_limits}}


def test_codex_names_windows_by_length_not_slot():
    # A weekly-only plan reports its week as `primary`, with no secondary.
    weekly = {"limit_id": "codex", "primary": {"used_percent": 6.0, "window_minutes": 10080,
                                               "resets_at": NOW + 86400}, "secondary": None}
    assert codex_samples(_event(weekly)) == [
        {"window": "7d", "seconds": 604800, "t": NOW, "pct": 6.0, "resets_at": NOW + 86400}]
    # Older Codex builds give seconds-until-reset instead of an epoch.
    both = {"primary": {"used_percent": 40, "window_minutes": 300, "resets_in_seconds": 600},
            "secondary": {"used_percent": 7, "window_minutes": 10080, "resets_at": NOW + 9}}
    assert [(s["window"], s["pct"], s["resets_at"]) for s in codex_samples(_event(both))] == [
        ("5h", 40.0, NOW + 600), ("7d", 7.0, NOW + 9)]
    other = {"limit_id": "premium", "primary": {"used_percent": 1, "window_minutes": 300}}
    assert codex_samples(_event(other)) == []


def test_read_codex_takes_the_newest_logged_limits(tmp_path):
    day = tmp_path / "sessions/2026/10/08"
    day.mkdir(parents=True)
    limits = {"primary": {"used_percent": 3, "window_minutes": 10080, "resets_at": NOW}}
    newer = {"primary": {"used_percent": 5, "window_minutes": 10080, "resets_at": NOW}}
    lines = [_event(limits, "2026-10-08T11:00:00Z"), _event(newer), _event(None),
             {"timestamp": "2026-10-08T12:01:00Z", "type": "response_item", "payload": {}}]
    (day / "rollout.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    assert [s["pct"] for s in read_codex(tmp_path)] == [5.0]
    assert read_codex(tmp_path / "missing") == []


def test_claude_response_windows():
    data = {"five_hour": None, "seven_day": {"utilization": 77.0,
                                             "resets_at": "2026-10-09T04:00:00.071697+00:00"},
            "seven_day_opus": {"utilization": 1}}
    assert claude_samples(data, NOW) == [
        {"window": "5h", "seconds": 18000, "t": NOW, "pct": 0.0, "resets_at": None},
        {"window": "7d", "seconds": 604800, "t": NOW, "pct": 77.0,
         "resets_at": datetime(2026, 10, 9, 4, 0, 0, 71697, tzinfo=UTC).timestamp()}]


def test_expired_token_is_unavailable_without_a_request(tmp_path, monkeypatch):
    (tmp_path / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "x", "expiresAt": (NOW - 1) * 1000}}))
    monkeypatch.setattr(plan_usage.urllib.request, "urlopen", pytest.fail)
    with pytest.raises(PermissionError):
        plan_usage.fetch_claude(tmp_path, NOW)


def test_projection_extends_the_fitted_pace_to_the_reset():
    hour, start = 3600, NOW
    # 10% an hour, steadily: 30% at 3h, so 50% by the 5h reset and no limit hit.
    steady = [(start + h * hour, 10.0 * h) for h in (1, 2, 3)]
    assert project(steady, start + 5 * hour, 5 * hour) == {"projected": 50.0, "limit_at": None}
    # 25% an hour reaches 100% at 4h, an hour before the reset.
    fast = [(start + h * hour, 25.0 * h) for h in (1, 2, 3)]
    assert project(fast, start + 5 * hour, 5 * hour) == {"projected": 125.0,
                                                         "limit_at": start + 4 * hour}
    # Samples from the previous window are not this window's pace.
    assert project([(start - hour, 90.0)], start + 5 * hour, 5 * hour)["projected"] == 0.0


def _claude_home(path, uuid, email):
    path.mkdir(parents=True)
    (path / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": uuid, "emailAddress": email}}))
    return path


def test_two_claude_config_dirs_are_two_accounts_fetched_once_each(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    home = tmp_path / "home"
    work = _claude_home(tmp_path / "claude-work", "u-work", "dev@example.com")
    _claude_home(home / ".claude", "u-home", "me@example.com")
    (home / ".claude.json").write_text((home / ".claude/.claude.json").read_text())
    monkeypatch.setattr(plan_usage.os, "environ", {"HOME": str(home)})  # the daemon's own
    envs = {"1": {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(work)},
            "2": {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(work)},
            "3": {"HOME": str(home)}}
    monkeypatch.setattr(plan_usage, "pane_env", lambda pid, tool: envs[pid])
    fetched = []

    def fetch(config, now):
        fetched.append(config)
        return claude_samples({"five_hour": {"utilization": 10 if config == work else 60,
                                             "resets_at": "2026-10-08T14:00:00+00:00"}}, now)

    usage = PlanUsage(History(tmp_path / "h.sqlite3"), fetch=fetch)
    panes = [{"pane_id": f"%{i}", "tool": "claude"} for i in "123"]
    usage.poll(panes, lambda pane_id: pane_id[1:], now=NOW)
    usage.poll(panes, lambda pane_id: pane_id[1:], now=NOW + 60)  # inside the TTL: no fetch
    assert sorted(fetched) == sorted([work, home / ".claude"])
    report = usage.report(now=NOW)
    assert [(a["label"], a["panes"], a["windows"][0]["pct"]) for a in report] == [
        ("dev", ["%1", "%2"], 10.0), ("me", ["%3"], 60.0)]


def test_report_drops_a_reset_window_and_hides_stale_data_on_error(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    codex = tmp_path / ".codex"
    (codex / "sessions").mkdir(parents=True)
    monkeypatch.setattr(plan_usage.os, "environ", {"HOME": str(tmp_path)})
    sample = {"window": "7d", "seconds": 604800, "t": NOW, "pct": 40.0, "resets_at": NOW + 60}
    usage = PlanUsage(History(tmp_path / "h.sqlite3"), read=lambda home: [sample])
    usage.poll([], lambda _: None, now=NOW)
    [account] = usage.report(now=NOW)
    assert account["label"] is None
    assert account["windows"][0]["pct"] == 40.0
    assert usage.report(now=NOW + 120)[0]["windows"] == [
        {"window": "7d", "pct": 0.0, "resets_at": None, "samples": []}]
    usage.read = lambda home: 1 / 0
    usage.poll([], lambda _: None, now=NOW + 1)
    assert usage.report(now=NOW)[0] | {"panes": []} == {
        "provider": "codex", "label": None, "panes": [], "error": "unavailable", "windows": []}
