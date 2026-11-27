"""Antigravity CLI (agy) and Gemini CLI plan quotas, from Google's Cloud Code service.

Both CLIs sign in with a personal Google login kept in the OS keyring (one slot per CLI,
machine-wide), and both ask the same undocumented Cloud Code API how much of each model's
quota is left: agy `fetchAvailableModels` (each model's `quotaInfo`), Gemini CLI
`retrieveUserQuota` (a bucket per model). Each answers a remaining fraction and a reset
time per model; this module turns those into PlanUsage samples, one meter per model family,
at its most-constrained model. The token is read in-process from the CLI's own store and
never refreshed here: the CLI owns it. Sign-ins by gcloud ADC are not metered (no plan
window to read). See docs/design/plan-usage.md.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import open_dbus_connection

logger = logging.getLogger(__name__)
TOOLS = ("antigravity", "gemini")
# agy's own endpoint (its logs show it calls the daily host). It answers an Antigravity
# plan only to a client that names itself one, so the User-Agent says both.
AGY_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal"
AGY_AGENT = "tmux-rc antigravity"
GEMINI_URL = "https://cloudcode-pa.googleapis.com/v1internal"
# The keyring items each CLI signs in to (agy through go-keyring, Gemini CLI through keytar).
ITEMS = {"antigravity": {"service": "gemini", "username": "antigravity"},
         "gemini": {"service": "gemini-cli-oauth", "account": "main-account"}}
AGY_DIR = Path.home() / ".gemini" / "antigravity-cli"  # names the account to the UI
# The windows a quota may have. Google sends a reset time but no length: an untouched quota
# resets one full window from now, so the window is the shortest that fits the time left.
SPANS = (5 * 3600, 86400, 7 * 86400)
SLACK = 120  # seconds the reset may run past a window's length (the answer's own latency)
FAMILY = {"GOOGLE": "Gemini", "ANTHROPIC": "Claude", "OPENAI": "GPT"}
SECRETS = DBusAddress("/org/freedesktop/secrets", bus_name="org.freedesktop.secrets",
                      interface="org.freedesktop.Secret.Service")


def keyring(attrs: dict) -> bytes | None:
    """The secret of the unlocked keyring item with these attributes, over the Secret
    Service's plain session (D-Bus to this user's own keyring daemon), else None. A locked
    item is left alone: unlocking would prompt on the desktop."""
    try:
        with open_dbus_connection(bus="SESSION") as bus:
            _, session = bus.send_and_get_reply(
                new_method_call(SECRETS, "OpenSession", "sv", ("plain", ("s", ""))), timeout=5).body
            unlocked, _ = bus.send_and_get_reply(
                new_method_call(SECRETS, "SearchItems", "a{ss}", (attrs,)), timeout=5).body
            if not unlocked:
                return None
            (secrets,) = bus.send_and_get_reply(new_method_call(
                SECRETS, "GetSecrets", "aoo", (unlocked[:1], session)), timeout=5).body
    except Exception as e:  # noqa: BLE001 - no bus, no keyring daemon: nobody signed in here
        logger.debug("keyring unreadable: %s", type(e).__name__)
        return None
    return next((bytes(s[2]) for s in secrets.values()), None)


def _epoch(stamp: str) -> float:
    """An RFC 3339 time; Go's has nanoseconds, which fromisoformat takes only up to micro."""
    head, dot, rest = stamp.partition(".")
    if dot:
        digits = len(rest) - len(rest.lstrip("0123456789"))
        stamp = f"{head}.{rest[:min(digits, 6)]}{rest[digits:]}"
    return datetime.fromisoformat(stamp).timestamp()


def agy_login(raw: bytes | None) -> dict | None:
    """agy's keyring item: an OAuth token, its expiry and the login's email."""
    try:
        data = json.loads(raw or b"null")
        return {"token": data["token"]["access_token"], "expires": _epoch(data["token"]["expiry"]),
                "email": data.get("id_token_email") or ""}
    except (ValueError, KeyError, TypeError):
        return None


def gemini_login(raw: bytes | None, home: Path) -> dict | None:
    """Gemini CLI's login: its keyring item, else the oauth_creds.json older builds (and
    GEMINI_FORCE_FILE_STORAGE) keep in <home>/.gemini. The email is google_accounts.json's
    active one."""
    gemini = home / ".gemini"
    try:
        if raw:
            token = json.loads(raw)["token"]
            login = {"token": token["accessToken"], "expires": token.get("expiresAt", 0) / 1000}
        else:
            creds = json.loads((gemini / "oauth_creds.json").read_text())
            login = {"token": creds["access_token"], "expires": creds.get("expiry_date", 0) / 1000}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    with suppress(OSError, ValueError, AttributeError):
        login["email"] = json.loads((gemini / "google_accounts.json").read_text()).get("active")
    return {**login, "email": login.get("email") or ""}


def span(left: float, prior: int = 0) -> int:
    """The window a quota with `left` seconds to its reset runs in: the shortest that fits,
    and no shorter than this window was already read as (`prior`), since a weekly quota's
    last hours fit a 5h window too."""
    return next((s for s in SPANS if s >= max(left - SLACK, prior)), SPANS[-1])


def window_name(seconds: int) -> str:
    return f"{seconds // 86400}d" if seconds % 86400 == 0 else f"{seconds // 3600}h"


def family(model: str) -> str:
    """A Gemini CLI model's family: its id without "gemini", version or "preview", so
    gemini-2.5-flash-lite is "Flash Lite", and every Pro shares one meter."""
    words = [w for w in model.removeprefix("models/").split("-")
             if w and w not in ("gemini", "preview") and not w[0].isdigit()]
    return " ".join(w.title() for w in words) or model


def samples(quotas: list[tuple[str, float, str | None]], now: float, prior: dict) -> list[dict]:
    """(family, remaining fraction, reset time) per model, as one sample per family at its
    most-constrained model, named "<window> <family>". A model with no reset time is one
    no plan limits (agy's autocomplete) and is left out. `prior` is each window name's
    length as already read for its current window."""
    worst: dict[str, tuple[float, float]] = {}
    for fam, left, reset in quotas:
        if reset is not None and left is not None:
            at = _epoch(reset)
            if fam not in worst or left < worst[fam][0]:
                worst[fam] = (float(left), at)
    out = []
    for fam, (left, at) in sorted(worst.items()):
        known = next((s for name, s in prior.items() if name.split(" ", 1)[-1] == fam), 0)
        seconds = span(at - now, known)
        out.append({"window": f"{window_name(seconds)} {fam}", "seconds": seconds, "t": now,
                    "pct": round(max(0.0, 1 - left) * 100, 1), "resets_at": at})
    return out


def agy_quotas(data: dict) -> list[tuple]:
    """fetchAvailableModels' models: every one of a provider shares that provider's pool."""
    out = []
    for model in (data["models"] or {}).values():  # a changed shape raises: "unavailable"
        info, provider = model.get("quotaInfo") or {}, (model.get("modelProvider") or "")
        provider = provider.removeprefix("MODEL_PROVIDER_")
        out.append((FAMILY.get(provider, provider.title() or "Other"),
                    info.get("remainingFraction", 0.0), info.get("resetTime")))
    return out


def gemini_quotas(data: dict) -> list[tuple]:
    """retrieveUserQuota's buckets, one per model (and token type)."""
    return [(family(b.get("modelId") or ""), b.get("remainingFraction"), b.get("resetTime"))
            for b in data["buckets"] or [] if b.get("modelId")]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):  # urllib would carry the token to any redirect target
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def post(base: str, method: str, token: str, body: dict, agent: str = "tmux-rc") -> dict:
    request = urllib.request.Request(f"{base}:{method}", data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "User-Agent": agent})
    with OPENER.open(request, timeout=15) as response:
        return json.load(response)


class GoogleQuota:
    """The signed-in agy and Gemini CLI logins, and their quotas. Each login is read from
    its keyring slot at most once a minute (PlanUsage's poll), its quota at its TTL."""

    def __init__(self, read=keyring, post=post):
        self.read, self.post = read, post
        self._projects: dict[str, str] = {}  # a Gemini CLI login's Code Assist project

    def login(self, tool: str, home: Path) -> dict | None:
        raw = self.read(ITEMS[tool])
        return agy_login(raw) if tool == "antigravity" else gemini_login(raw, home)

    def accounts(self, envs: list[tuple[str, dict, str]]) -> dict[tuple[str, str], dict]:
        """Every signed-in login, keyed by email, with the panes on it: each CLI's own slot
        whether or not a pane runs it, so the meters show before one starts. An agy pane
        on gcloud ADC (AGY_ADC_AUTH) is not on that login."""
        homes = {("gemini", Path(e.get("GEMINI_CLI_HOME") or e.get("HOME") or Path.home()))
                 for t, e, _ in envs if t == "gemini"}
        out: dict[tuple[str, str], dict] = {}
        for tool, home in sorted({("antigravity", Path.home()), ("gemini", Path.home()), *homes}):
            login = self.login(tool, home)
            running = any(t == tool for t, *_ in envs)
            if not login or (login["expires"] <= time.time() and not running):
                continue  # signed out, or a token left to expire by a CLI nobody runs
            where = AGY_DIR if tool == "antigravity" else home / ".gemini"
            key = login["email"] or str(where)
            account = out.setdefault((tool, key), {"key": key, "homes": [], "panes": [],
                                                   "name": login["email"], "login": login,
                                                   "project": None})
            account["homes"] += [where] if where not in account["homes"] else []
        for tool, env, pane_id in envs:
            if tool == "antigravity" and env.get("AGY_ADC_AUTH", "").lower() in ("1", "true"):
                continue
            home = Path(env.get("GEMINI_CLI_HOME") or env.get("HOME") or Path.home())
            for (t, _), account in out.items():
                if t == tool and (tool == "antigravity" or home / ".gemini" in account["homes"]):
                    account["panes"] += [pane_id] if pane_id not in account["panes"] else []
                    account["project"] = account["project"] or env.get("GOOGLE_CLOUD_PROJECT")
        return out

    def fetch(self, tool: str, account: dict, now: float, prior: dict) -> list[dict]:
        login = account["login"]
        if login["expires"] <= now:
            raise PermissionError("token expired")
        if tool == "antigravity":
            data = self.post(AGY_URL, "fetchAvailableModels", login["token"], {}, AGY_AGENT)
            return samples(agy_quotas(data), now, prior)
        project = account.get("project") or self._projects.get(account["key"])
        if not project:  # as Gemini CLI finds it: the project Code Assist assigns the login
            meta = {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED",
                    "pluginType": "GEMINI"}
            project = self.post(GEMINI_URL, "loadCodeAssist", login["token"],
                                {"metadata": meta})["cloudaicompanionProject"]
            self._projects[account["key"]] = project
        data = self.post(GEMINI_URL, "retrieveUserQuota", login["token"], {"project": project})
        return samples(gemini_quotas(data), now, prior)
