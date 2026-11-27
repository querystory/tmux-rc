"""Antigravity and Gemini CLI quotas: logins, families, window lengths, and PlanUsage's use."""
import json
from datetime import UTC, datetime

import pytest

from openbus import google_quota, plan_usage
from openbus.google_quota import GoogleQuota, agy_login, family, gemini_login, samples, span
from openbus.history import History
from openbus.plan_usage import PlanUsage

NOW = datetime(2026, 3, 4, 10, tzinfo=UTC).timestamp()
H = 3600


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _agy_item(expires=NOW + H, email="pat@example.com") -> bytes:
    return json.dumps({"token": {"access_token": "tok-a", "token_type": "Bearer",
                                 "expiry": "2026-03-04T03:00:00.123456789-08:00"
                                 if expires is None else _iso(expires)},
                       "auth_method": "consumer", "id_token_email": email}).encode()


# Invented, in fetchAvailableModels' shape: a Gemini pool partly used, a Claude pool untouched
# (its reset a full window away), and an autocomplete model with no reset at all.
MODELS = {"models": {
    "gemini-fast": {"displayName": "Gemini Fast", "modelProvider": "MODEL_PROVIDER_GOOGLE",
                    "quotaInfo": {"remainingFraction": 0.75, "resetTime": _iso(NOW + 2 * H)}},
    "gemini-deep": {"displayName": "Gemini Deep", "modelProvider": "MODEL_PROVIDER_GOOGLE",
                    "quotaInfo": {"remainingFraction": 0.4, "resetTime": _iso(NOW + 2 * H)}},
    "claude-poet": {"displayName": "Claude Poet", "modelProvider": "MODEL_PROVIDER_ANTHROPIC",
                    "quotaInfo": {"remainingFraction": 1, "resetTime": _iso(NOW + 5 * H)}},
    "tab_complete": {"modelProvider": "MODEL_PROVIDER_GOOGLE",
                     "quotaInfo": {"remainingFraction": 1}},
}}


def test_agy_login_reads_gos_nanosecond_expiry():
    login = agy_login(_agy_item(expires=None))
    assert login["token"] == "tok-a" and login["email"] == "pat@example.com"
    assert login["expires"] == datetime(2026, 3, 4, 11, 0, 0, 123456, tzinfo=UTC).timestamp()
    assert agy_login(None) is None and agy_login(b'{"token": {}}') is None


def test_gemini_login_from_keyring_else_file(tmp_path):
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini/google_accounts.json").write_text('{"active": "lee@example.com"}')
    raw = json.dumps({"serverName": "main-account",
                      "token": {"accessToken": "tok-g", "expiresAt": NOW * 1000}}).encode()
    assert gemini_login(raw, tmp_path) == {"token": "tok-g", "expires": NOW,
                                           "email": "lee@example.com"}
    assert gemini_login(None, tmp_path) is None  # signed out
    (tmp_path / ".gemini/oauth_creds.json").write_text(
        json.dumps({"access_token": "tok-f", "expiry_date": NOW * 1000}))
    assert gemini_login(None, tmp_path)["token"] == "tok-f"


def test_window_is_the_shortest_that_fits_and_never_shrinks():
    assert span(5 * H) == 5 * H  # an untouched 5h quota resets 5h out
    assert span(2 * H) == 5 * H
    assert span(20 * H) == 86400
    assert span(3 * 86400) == 7 * 86400
    assert span(2 * H, prior=7 * 86400) == 7 * 86400  # a week's last hours stay a week


def test_family_names_gemini_cli_models():
    assert family("gemini-2.5-flash-lite") == "Flash Lite"
    assert family("gemini-3-pro-preview") == "Pro"
    assert family("models/gemini-2.5-pro") == "Pro"


def test_agy_samples_one_meter_per_family_at_its_tightest_model():
    got = samples(google_quota.agy_quotas(MODELS), NOW, {})
    assert got == [
        # Untouched: its reset only rolls with the clock, so it is a window not yet opened.
        {"window": "5h Claude", "seconds": 5 * H, "t": NOW, "pct": 0.0, "resets_at": None},
        {"window": "5h Gemini", "seconds": 5 * H, "t": NOW, "pct": 60.0, "resets_at": NOW + 2 * H}]
    with pytest.raises(KeyError):  # a changed shape reads unavailable, never as 0%
        google_quota.agy_quotas({})


def test_gemini_cli_buckets_by_family():
    data = {"buckets": [
        {"modelId": "gemini-2.5-pro", "remainingFraction": 0.9, "resetTime": _iso(NOW + 14 * H)},
        {"modelId": "gemini-3-pro-preview", "remainingFraction": 0.5,
         "resetTime": _iso(NOW + 14 * H)},
        {"modelId": "gemini-2.5-flash", "remainingFraction": 1, "resetTime": _iso(NOW + 14 * H)}]}
    got = samples(google_quota.gemini_quotas(data), NOW, {})
    assert [(s["window"], s["pct"]) for s in got] == [("1d Flash", 0.0), ("1d Pro", 50.0)]


def _quota(items: dict, answers: dict) -> tuple[GoogleQuota, list]:
    calls = []

    def post(base, method, token, body, agent="tmux-rc"):
        calls.append((method, token, body, agent))
        return answers[method]
    return GoogleQuota(read=lambda attrs: items.get(attrs["service"]), post=post), calls


def test_accounts_skip_adc_panes_and_expired_idle_logins(monkeypatch, tmp_path):
    monkeypatch.setattr(google_quota.time, "time", lambda: NOW)
    monkeypatch.setenv("HOME", str(tmp_path))
    q, _ = _quota({"gemini": _agy_item()}, {})
    envs = [("antigravity", {"HOME": str(tmp_path)}, "%1"),
            ("antigravity", {"HOME": str(tmp_path), "AGY_ADC_AUTH": "true"}, "%2")]
    accounts = q.accounts(envs)
    assert list(accounts) == [("antigravity", "pat@example.com")]
    assert accounts[("antigravity", "pat@example.com")]["panes"] == ["%1"]
    q, _ = _quota({"gemini": _agy_item(expires=NOW - 1)}, {})
    assert q.accounts([]) == {}  # expired, and no agy is running to refresh it
    assert list(q.accounts(envs[:1])) == [("antigravity", "pat@example.com")]


def test_plan_usage_polls_google_at_ttl_and_names_the_login(monkeypatch, tmp_path):
    monkeypatch.setattr(google_quota.time, "time", lambda: NOW)
    monkeypatch.setenv("HOME", str(tmp_path))
    q, calls = _quota({"gemini": _agy_item()}, {"fetchAvailableModels": MODELS})
    usage = PlanUsage(History(tmp_path / "h.sqlite3"), profiles=list, google=q)
    usage.poll([], lambda _: None, NOW)
    usage.poll([], lambda _: None, NOW + 60)  # inside the TTL: no second request
    assert [(c[0], c[1], c[3]) for c in calls] == [
        ("fetchAvailableModels", "tok-a", "tmux-rc antigravity")]
    # Stored per pool ("5h Gemini"), reported as one series per family on the plain window,
    # so they chart beside Claude's and Codex's 5h, the past windows too. The Claude pool,
    # never used, is not shown at all.
    rows = usage.report(NOW, past=30)
    assert [(a["provider"], a["family"], a["name"], a["error"]) for a in rows] == [
        ("antigravity", "Gemini", "pat@example.com", None)]
    assert [(w["window"], w["pct"], w["resets_at"]) for w in rows[0]["windows"]] == [
        ("5h", 60.0, (NOW + 2 * H) * 1000)]


def test_a_family_used_once_stays_shown_after_its_window_ends(tmp_path):
    history = History(tmp_path / "h.sqlite3")
    history.record_usage([("antigravity", "pat@example.com", "5h Claude", 5 * H, NOW - 9 * H,
                           40.0, NOW - 6 * H)])
    usage = PlanUsage(history, profiles=list)
    usage.accounts = {("antigravity", "pat@example.com"): {
        "key": "pat@example.com", "homes": [], "panes": [], "name": "pat@example.com",
        "error": None}}
    history.record_usage([("antigravity", "pat@example.com", w, 5 * H, NOW, 0.0, None)
                          for w in ("5h Claude", "5h GPT")])
    [row] = usage.report(NOW)  # Claude read 40% earlier; GPT never moved
    assert row["family"] == "Claude"
    assert row["windows"][0] | {"samples": []} == {
        "window": "5h", "pct": 0.0, "resets_at": None, "samples": []}  # "not started"
    # An unreadable login still says so, though no family shows.
    usage.accounts[("antigravity", "pat@example.com")]["error"] = "unavailable"
    assert [r["error"] for r in usage.report(NOW + plan_usage.STALE_OK + 1)] == ["unavailable"]


def test_gemini_cli_finds_its_project_once(monkeypatch, tmp_path):
    monkeypatch.setattr(google_quota.time, "time", lambda: NOW)
    monkeypatch.setenv("HOME", str(tmp_path))
    raw = json.dumps({"token": {"accessToken": "tok-g", "expiresAt": (NOW + H) * 1000}}).encode()
    q, calls = _quota({"gemini-cli-oauth": raw}, {
        "loadCodeAssist": {"cloudaicompanionProject": "invented-project"},
        "retrieveUserQuota": {"buckets": []}})
    [(key, account)] = q.accounts([]).items()
    assert key[0] == "gemini"
    q.fetch("gemini", account, NOW, {})
    q.fetch("gemini", account, NOW, {})
    assert [c[0] for c in calls] == ["loadCodeAssist", "retrieveUserQuota", "retrieveUserQuota"]
    assert calls[-1][2] == {"project": "invented-project"}
