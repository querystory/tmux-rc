"""Plan-limit sources, account discovery, and the projection the trend line draws."""
import io
import json
from datetime import UTC, datetime

import pytest

from openbus import plan_usage
from openbus.history import History
from openbus.plan_usage import PlanUsage, claude_samples, codex_samples, ended, project, read_codex

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
    # A log touched later but holding an older event does not win.
    older = tmp_path / "sessions/2026/10/07"
    older.mkdir(parents=True)
    (older / "rollout.jsonl").write_text(json.dumps(_event(limits, "2026-10-07T09:00:00Z")))
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


def test_per_model_weekly_limits_come_from_the_scoped_limits():
    resets = "2026-10-09T04:00:00+00:00"
    model = lambda name: {"model": {"id": None, "display_name": name}}  # noqa: E731
    data = {"five_hour": None, "seven_day": None, "limits": [
        {"kind": "weekly_all", "percent": 22, "resets_at": resets, "scope": None},
        {"kind": "weekly_scoped", "percent": 13, "resets_at": resets, "scope": model("Fable")},
        {"kind": "weekly_scoped", "percent": None, "resets_at": None, "scope": model("Opus")},
        {"kind": "weekly_scoped", "percent": 5, "resets_at": resets, "scope": {"model": None}}]}
    assert [(s["window"], s["seconds"], s["pct"], s["resets_at"])
            for s in claude_samples(data, NOW)[2:]] == [
        ("7d Fable", 604800, 13.0, datetime(2026, 10, 9, 4, tzinfo=UTC).timestamp()),
        ("7d Opus", 604800, 0.0, None)]  # null: not yet opened, like a null window
    assert len(claude_samples({**data, "limits": None}, NOW)) == 2


def test_a_changed_claude_shape_is_an_error_not_zero():
    with pytest.raises(KeyError):
        claude_samples({"seven_day": {"utilization": 5}}, NOW)


def test_expired_token_is_unavailable_without_a_request(tmp_path, monkeypatch):
    (tmp_path / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "x", "expiresAt": (NOW - 1) * 1000}}))
    monkeypatch.setattr(plan_usage._OPENER, "open", pytest.fail)
    with pytest.raises(PermissionError):
        plan_usage.fetch_claude(tmp_path, NOW)


def _token(config):
    (config / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "x", "expiresAt": (NOW + 3600) * 1000}}))


def _opener(monkeypatch, *errors):
    """An opener raising each error in turn, then answering; returns the calls made."""
    calls, body = [], json.dumps({"five_hour": None, "seven_day": None}).encode()

    def open_(request, timeout):
        calls.append(request)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return io.BytesIO(body)
    monkeypatch.setattr(plan_usage._OPENER, "open", open_)
    monkeypatch.setattr(plan_usage.time, "sleep", lambda s: None)
    return calls


def test_a_network_blip_is_retried_once(tmp_path, monkeypatch):
    _token(tmp_path)
    calls = _opener(monkeypatch, TimeoutError())
    assert [s["window"] for s in plan_usage.fetch_claude(tmp_path, NOW)] == ["5h", "7d"]
    calls = _opener(monkeypatch, plan_usage.urllib.error.URLError("down"), TimeoutError())
    with pytest.raises(TimeoutError):
        plan_usage.fetch_claude(tmp_path, NOW)
    assert len(calls) == 2


def test_an_http_status_is_not_retried(tmp_path, monkeypatch):
    _token(tmp_path)
    calls = _opener(monkeypatch, plan_usage.urllib.error.HTTPError(
        plan_usage.USAGE_URL, 401, "Unauthorized", {}, None))
    with pytest.raises(plan_usage.urllib.error.HTTPError):
        plan_usage.fetch_claude(tmp_path, NOW)
    assert len(calls) == 1


def test_projection_extends_the_fitted_pace_to_the_reset():
    hour, start = 3600, NOW
    # 10% an hour, steadily: 30% at 3h, so 50% by the 5h reset and no limit hit.
    steady = [(start + h * hour, 10.0 * h) for h in (1, 2, 3)]
    assert project(steady, start + 5 * hour, 5 * hour, start + 3 * hour) == {
        "projected": 50.0, "limit_at": None}
    # 25% an hour reaches 100% at 4h, an hour before the reset.
    fast = [(start + h * hour, 25.0 * h) for h in (1, 2, 3)]
    assert project(fast, start + 5 * hour, 5 * hour, start + 3 * hour) == {
        "projected": 125.0, "limit_at": start + 4 * hour}
    # 20% an hour lands on exactly 100% at the reset: not before it, so no warning.
    even = [(start + h * hour, 20.0 * h) for h in (1, 2, 3)]
    assert project(even, start + 5 * hour, 5 * hour, start + 3 * hour)["limit_at"] is None
    # Quiet since: the pace slows, so the forecast moves out instead of into the past.
    later = project(fast, start + 5 * hour, 5 * hour, start + 3.5 * hour)
    assert start + 4 * hour < later["limit_at"] < start + 5 * hour
    assert project(fast, start + 5 * hour, 5 * hour, start + 4.5 * hour)["limit_at"] is None
    # Samples from the previous window are not this window's pace.
    assert project([(start - hour, 90.0)], start + 5 * hour, 5 * hour, start)["projected"] == 0.0


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
            "2": {"HOME": str(home), "CLAUDE_CONFIG_DIR": f"{work}/../{work.name}/"},  # an alias
            "3": {"HOME": f"{home}/../{home.name}"}}  # an alias too
    monkeypatch.setattr(plan_usage, "pane_env", lambda pid, tool: envs[pid])
    fetched = []

    def fetch(config, now):
        fetched.append(config)
        return claude_samples({"five_hour": {"utilization": 10 if config == work else 60,
                                             "resets_at": "2026-10-08T14:00:00+00:00"},
                               "seven_day": None}, now)

    usage = PlanUsage(History(tmp_path / "h.sqlite3"), fetch=fetch)
    panes = [{"pane_id": f"%{i}", "tool": "claude"} for i in "123"]
    usage.poll(panes, lambda pane_id: pane_id[1:], now=NOW)
    usage.poll(panes, lambda pane_id: pane_id[1:], now=NOW + 60)  # inside the TTL: no fetch
    assert sorted(fetched) == sorted([work, home / ".claude"])
    report = usage.report(now=NOW)
    assert [(a["dirs"], a["panes"], a["windows"][0]["pct"]) for a in report] == [
        ([str(work)], ["%1", "%2"], 10.0), ([str(home / ".claude")], ["%3"], 60.0)]


def test_report_drops_a_reset_window_and_keeps_a_recent_reading_on_error(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    codex = tmp_path / ".codex"
    (codex / "sessions").mkdir(parents=True)
    monkeypatch.setattr(plan_usage.os, "environ", {"HOME": str(tmp_path)})
    sample = {"window": "7d", "seconds": 604800, "t": NOW, "pct": 40.0, "resets_at": NOW + 60}
    usage = PlanUsage(History(tmp_path / "h.sqlite3"), read=lambda home: [sample])
    usage.poll([], lambda _: None, now=NOW)
    [account] = usage.report(now=NOW)
    assert account["windows"][0]["pct"] == 40.0
    assert usage.report(now=NOW + 120)[0]["windows"] == [
        {"window": "7d", "pct": 0.0, "resets_at": None, "samples": []}]
    assert account["as_of"] is None
    usage.read = lambda home: 1 / 0
    usage.poll([], lambda _: None, now=NOW + 1)
    # A failed read keeps the last reading, dated, while it is recent ...
    [stale] = usage.report(now=NOW + 30)
    assert (stale["error"], stale["as_of"], stale["windows"][0]["pct"]) == (None, NOW * 1000, 40.0)
    # ... and reads unavailable once it is too old to trust.
    assert usage.report(now=NOW + plan_usage.STALE_OK + 1)[0] | {"panes": []} == {
        "provider": "codex", "dirs": [str(codex)], "panes": [], "error": "unavailable",
        "as_of": None, "windows": []}


def test_without_history_an_account_reads_unavailable_not_limitless(tmp_path, monkeypatch):
    (tmp_path / ".codex/sessions").mkdir(parents=True)
    monkeypatch.setattr(plan_usage.os, "environ", {"HOME": str(tmp_path)})
    usage = PlanUsage(None, read=lambda home: [{"window": "7d", "seconds": 604800, "t": NOW,
                                                "pct": 40.0, "resets_at": NOW + 60}])
    usage.poll([], lambda _: None, now=NOW)
    assert usage.report(now=NOW)[0]["error"] == "unavailable"


def test_an_account_with_no_samples_yet_is_not_limitless(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    (tmp_path / ".codex/sessions").mkdir(parents=True)
    monkeypatch.setattr(plan_usage.os, "environ", {"HOME": str(tmp_path)})
    usage = PlanUsage(History(tmp_path / "h.sqlite3"), read=lambda home: [])
    usage.poll([], lambda _: None, now=NOW)
    assert usage.report(now=NOW)[0]["error"] == "no data yet"


def test_a_redirect_never_carries_the_token():
    request = plan_usage.urllib.request.Request(plan_usage.USAGE_URL)
    assert plan_usage._NoRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://elsewhere.example/") is None


def test_a_window_the_plan_dropped_is_not_shown(tmp_path):
    tmp_path.chmod(0o700)
    history = History(tmp_path / "h.sqlite3")
    history.record_usage([("codex", "a", "5h", 18000, NOW, 10, NOW + 60),
                          ("codex", "a", "7d", 604800, NOW, 20, NOW + 60),
                          ("codex", "a", "7d", 604800, NOW + 1, 21, NOW + 60)])
    assert [r[0] for r in history.latest_usage("codex", "a")] == ["7d"]


def test_a_stale_config_dir_does_not_shadow_a_working_one_for_the_same_account(tmp_path):
    usage = PlanUsage(None)
    usage.fetch = lambda config, now: [config.name] if config.name == "good" else 1 / 0
    assert usage._fetch_any([tmp_path / "stale", tmp_path / "good"], NOW) == ["good"]


def test_a_failed_account_stays_unavailable_when_it_reappears_within_the_ttl(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    work = _claude_home(tmp_path / "claude-work", "u-work", "dev@example.com")
    monkeypatch.setattr(plan_usage.os, "environ", {"HOME": str(tmp_path / "nobody")})
    monkeypatch.setattr(plan_usage, "pane_env",
                        lambda pid, tool: {"CLAUDE_CONFIG_DIR": str(work)})
    usage = PlanUsage(History(tmp_path / "h.sqlite3"), fetch=lambda config, now: 1 / 0)
    pane = [{"pane_id": "%1", "tool": "claude"}]
    usage.poll(pane, lambda _: "1", now=NOW)
    usage.poll([], lambda _: "1", now=NOW + 60)  # its last pane closed
    usage.poll(pane, lambda _: "1", now=NOW + 120)  # back inside the TTL: no new fetch
    assert usage.report(now=NOW)[0]["error"] == "unavailable"


def test_samples_past_eight_days_are_thinned_to_one_an_hour_not_deleted(tmp_path):
    tmp_path.chmod(0o700)
    history = History(tmp_path / "h.sqlite3")
    old, reset = NOW - 10 * 86400, NOW - 10 * 86400 + 5400  # a window resets mid-hour
    # 5-minute samples over two hours: one window climbs to its limit at old + 4800 and
    # resets at old + 5400, mid-hour; the next one idles at 1%.
    thinned = [("claude", "a", "5h", 18000, t, min(100, (t - old) / 48), reset) if t < reset
               else ("claude", "a", "5h", 18000, t, 1, reset + 18000)
               for t in range(int(old), int(old) + 7200, 300)]
    unopened = [("claude", "a", "5h", 18000, old - m, 0, None) for m in (60, 120)]
    raw = [("claude", "a", "5h", 18000, NOW - m, 1, NOW + 60) for m in (120, 60, 0)]
    history.record_usage(thinned + unopened + raw)
    assert history.usage_between("claude", "a", "5h", 0, NOW) == [
        (old + 3300, 3300 / 48, reset),  # the hour's last sample
        (old + 4500, 4500 / 48, reset),  # its window's last below the limit, mid-hour
        (old + 4800, 100, reset),  # the first at the limit: when the window hit it
        (old + 6900, 1, reset + 18000),  # the next window's last of the hour
        *[r[4:] for r in raw]]  # within eight days: every sample


def test_the_usage_view_reads_the_windows_ended_in_its_range(tmp_path):
    tmp_path.chmod(0o700)
    usage = PlanUsage(History(tmp_path / "h.sqlite3"))
    usage.accounts = {("claude", "a"): {"homes": [], "panes": [], "error": None}}
    first, second = NOW - 30000, NOW - 5000  # two ended 5h windows' resets, then this one
    usage.history.record_usage([("claude", "a", "5h", 18000, t, p, r) for t, p, r in (
        (NOW - 3 * 86400, 50, NOW - 3 * 86400 + 60),  # three days back: out of a 1-day range
        (NOW - 86400 - 600, 30, NOW - 86400 - 60),  # read within a window of it, ended before it
        (first - 9000, 20, first), (first - 60, 40, first + 2),  # re-read: the same window
        (second - 4000, 100, second), (second - 60, 100, second),
        (NOW - 600, 10, NOW + 9000), (NOW, 12, None))])  # a closed window is none of them
    [window] = usage.report(now=NOW, past=1)[0]["windows"]
    assert [(w["pct"], w["start"] / 1000, w["limit_at"]) for w in window["past"]] == [
        (40, first + 2 - 18000, None), (100, second - 18000, (second - 4000) * 1000)]
    assert window["past"][0]["samples"] == [[(first - 9000) * 1000, 20], [(first - 60) * 1000, 40]]
    assert len(usage.report(now=NOW, past=7)[0]["windows"][0]["past"]) == 4
    assert "past" not in usage.report(now=NOW)[0]["windows"][0]  # the strip's minute poll
    # A re-read that moves this window's reset past now leaves it current, not ended.
    assert ended("5h", 18000, [(NOW - 600, 5, NOW - 30), (NOW - 60, 6, NOW + 30)], NOW) == []
