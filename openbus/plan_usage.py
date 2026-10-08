"""Claude and Codex plan limits: how much of each account's 5h and 7d windows is used.

One account per config dir: a pane's agent reads CLAUDE_CONFIG_DIR or CODEX_HOME (else
HOME) from its environment, so two panes may draw on two plans. Codex writes its limits
into its own session logs, so reading them needs no credential. Claude publishes them
only through an undocumented OAuth endpoint, polled at most every CLAUDE_TTL per account.
See docs/design/plan-usage.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import tmux

logger = logging.getLogger(__name__)
POLL = 60  # Codex logs are local and cheap; the Claude endpoint is still CLAUDE_TTL
CLAUDE_TTL = 300
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_WINDOWS = {"five_hour": ("5h", 5 * 3600), "seven_day": ("7d", 7 * 86400)}
ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
TAIL = 1 << 20  # Codex session logs reach hundreds of MB; the newest limits sit at the end
CODEX_FILES = 5  # newest session logs searched for a limits event
SPARK_POINTS = 120  # samples sent per window: a sidebar sparkline's width, not a week of polls


def window_name(minutes: int) -> str:
    return f"{minutes // 1440}d" if minutes % 1440 == 0 else f"{minutes // 60}h"


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def home(provider: str, env: dict) -> Path:
    """The config dir an agent with this environment reads: its override, else ~/.claude
    or ~/.codex under its HOME."""
    if override := env.get(ENV[provider]):
        return Path(override)
    return Path(env.get("HOME") or Path.home()) / f".{provider}"


def pane_env(pid: str, tool: str) -> dict:
    """HOME and the tool's config override, from the agent process under the pane (an
    inline `CLAUDE_CONFIG_DIR=… claude` sets it there, not in the shell). Every other
    variable is dropped unread."""
    agent = next((p for p in tmux.processes(pid) if tmux.proc_read(p, "comm").strip() == tool),
                 pid)
    pairs = (v.partition("=") for v in tmux.proc_read(agent, "environ").split("\0"))
    return {k: v for k, _, v in pairs if k in ("HOME", ENV[tool])}


def codex_samples(event: dict) -> list[dict]:
    """The windows in one Codex token_count event. Which slot holds which window varies
    by plan (a weekly-only plan reports it as primary), so window_minutes names it."""
    limits = (event.get("payload") or {}).get("rate_limits")
    if not isinstance(limits, dict) or limits.get("limit_id", "codex") != "codex":
        return []
    t = _epoch(event["timestamp"])
    out = []
    for slot in ("primary", "secondary"):
        w = limits.get(slot)
        if not w or not w.get("window_minutes"):
            continue
        resets = w.get("resets_at") or (t + w["resets_in_seconds"] if "resets_in_seconds" in w
                                        else None)
        out.append({"window": window_name(w["window_minutes"]),
                    "seconds": w["window_minutes"] * 60, "t": t,
                    "pct": float(w["used_percent"]), "resets_at": resets})
    return out


def _last_limits(path: Path) -> list[dict]:
    """The last token_count event in this log's tail that carries limits."""
    with path.open("rb") as f:
        f.seek(max(0, f.seek(0, os.SEEK_END) - TAIL))
        lines = f.read().splitlines()
    for line in reversed(lines):
        if b'"rate_limits"' in line:
            try:
                if samples := codex_samples(json.loads(line)):
                    return samples
            except (ValueError, KeyError, TypeError):
                continue
    return []


def read_codex(codex_home: Path) -> list[dict]:
    """The latest limits Codex logged under this home. Concurrent sessions each write
    their own log, so the newest event wins, not the newest-touched file."""
    files = sorted((codex_home / "sessions").glob("*/*/*/*.jsonl"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    found = (s for path in files[:CODEX_FILES] if (s := _last_limits(path)))
    return max(found, key=lambda s: s[0]["t"], default=[])


def claude_samples(data: dict, now: float) -> list[dict]:
    """The OAuth usage response's windows. A null window is one not yet opened: 0%."""
    out = []
    for key, (name, seconds) in CLAUDE_WINDOWS.items():
        w = data.get(key) or {}
        resets = w.get("resets_at")
        out.append({"window": name, "seconds": seconds, "t": now,
                    "pct": float(w.get("utilization") or 0),
                    "resets_at": resets and _epoch(resets)})
    return out


def claude_account(config: Path, env: dict) -> dict | None:
    """The non-secret identity Claude Code keeps beside its credentials: .claude.json
    inside an overridden config dir, else next to the default one."""
    meta = config / ".claude.json" if env.get(ENV["claude"]) else config.parent / ".claude.json"
    try:
        account = json.loads(meta.read_text()).get("oauthAccount") or {}
    except (OSError, ValueError):
        return None
    short = (account.get("emailAddress") or "?").split("@")[0]
    return account.get("accountUuid") and {"key": account["accountUuid"], "short": short}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):  # urllib would carry the token to any redirect target
        return None  # so a 3xx is an HTTPError: "unavailable"


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_claude(config: Path, now: float) -> list[dict]:
    """GET the usage endpoint with this config dir's OAuth token, read in-process only.
    An expired token raises rather than refreshing: Claude Code owns that file."""
    oauth = json.loads((config / ".credentials.json").read_text())["claudeAiOauth"]
    if oauth.get("expiresAt", 0) / 1000 <= now:
        raise PermissionError("token expired")
    request = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {oauth['accessToken']}", "anthropic-beta": "oauth-2025-04-20"})
    with _OPENER.open(request, timeout=10) as response:
        return claude_samples(json.load(response), now)


def project(samples: list[tuple[float, float]], resets_at: float, seconds: int,
            now: float) -> dict:
    """Where the window ends at the current pace: a least-squares slope over this window's
    samples, anchored at 0% when it opened, extended from now to the reset. The latest
    value holds until now (no new Codex event means no new use), so a quiet spell slows
    the pace instead of leaving a forecast in the past. `limit_at` is when that line
    crosses 100%, if before the reset."""
    start = resets_at - seconds
    points = [(start, 0.0), *((t, p) for t, p in samples if t >= start)]
    if now > points[-1][0]:
        points.append((now, points[-1][1]))
    n = len(points)
    mt, mp = sum(t for t, _ in points) / n, sum(p for _, p in points) / n
    var = sum((t - mt) ** 2 for t, _ in points)
    slope = max(0.0, sum((t - mt) * (p - mp) for t, p in points) / var) if var else 0.0
    t, p = points[-1]
    projected = p + slope * (resets_at - t)
    return {"projected": round(projected, 1),
            "limit_at": t + (100 - p) / slope if projected > 100 and p < 100 else None}


class PlanUsage:
    """Discovers accounts from the panes, samples them into History, reports for the UI."""

    def __init__(self, history, fetch=fetch_claude, read=read_codex):
        self.history, self.fetch, self.read = history, fetch, read
        self.accounts: dict[tuple[str, str], dict] = {}
        self._fetched: dict[str, float] = {}

    def discover(self, panes: list[dict], birth) -> dict[tuple[str, str], dict]:
        """Every account a live agent pane uses, plus the daemon user's own defaults."""
        found: dict[tuple[str, str], dict] = {}
        sources = [(t, {k: os.environ[k] for k in ("HOME", ENV[t]) if k in os.environ}, None)
                   for t in ENV]
        sources += [(p["tool"], pane_env(pid, p["tool"]), p["pane_id"]) for p in panes
                    if p.get("tool") in ENV and (pid := birth(p["pane_id"]))]
        for tool, env, pane_id in sources:
            config = home(tool, env)
            if tool == "codex":
                ident = (config / "sessions").is_dir() and {"key": str(config),
                                                            "short": config.name.lstrip(".")}
            else:
                ident = claude_account(config, env)
            if ident:
                account = found.setdefault((tool, ident["key"]), {**ident, "home": config,
                                                                  "panes": []})
                account["panes"] += [pane_id] if pane_id else []
        return found

    def poll(self, panes: list[dict], birth, now: float | None = None) -> None:
        now = time.time() if now is None else now
        accounts = self.discover(panes, birth)
        rows = []
        for (tool, key), account in accounts.items():
            previous = self.accounts.get((tool, key), {})
            account["error"] = previous.get("error")
            try:
                if tool == "codex":
                    samples, account["error"] = self.read(account["home"]), None
                elif now - self._fetched.get(key, 0) >= CLAUDE_TTL:
                    self._fetched[key] = now
                    samples, account["error"] = self.fetch(account["home"], now), None
                else:
                    samples = []
            except Exception as e:  # noqa: BLE001 - any failure reads as "unavailable"
                # The type only: an HTTP error's text could echo request details.
                logger.info("plan usage unavailable for a %s account: %s", tool, type(e).__name__)
                samples, account["error"] = [], "unavailable"
            rows += [(tool, key, s["window"], s["seconds"], s["t"], s["pct"], s["resets_at"])
                     for s in samples]
        if rows and self.history:
            self.history.record_usage(rows)
        self.accounts = accounts

    async def run(self, watcher) -> None:
        while True:
            try:
                await asyncio.to_thread(self.poll, list(watcher.states), watcher.pane_birth)
            except Exception:  # one bad poll must not end sampling
                logger.warning("plan usage poll failed", exc_info=True)
            await asyncio.sleep(POLL)

    def _window(self, tool: str, key: str, row: tuple, now: float) -> dict:
        """One window for the UI, in ms: its latest %, its trend thinned to SPARK_POINTS
        (newest kept), and the projection over every sample."""
        name, seconds, _, pct, resets = row
        if resets is None or resets <= now:  # the window reset since: nothing used yet
            return {"window": name, "pct": 0.0, "resets_at": None, "samples": []}
        samples = self.history.usage_since(tool, key, name, resets - seconds)
        trend = project(samples, resets, seconds, now)
        thin = samples[::-1][::max(1, -(-len(samples) // SPARK_POINTS))][::-1]
        return {"window": name, "pct": pct, "resets_at": resets * 1000,
                "start": (resets - seconds) * 1000, "samples": [[t * 1000, p] for t, p in thin],
                "projected": trend["projected"],
                "limit_at": trend["limit_at"] and trend["limit_at"] * 1000}

    def report(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        accounts = self.accounts  # one snapshot: poll() swaps it from a worker thread
        count = {t: sum(k[0] == t for k in accounts) for t in ENV}
        out = []
        for (tool, key), account in accounts.items():
            # Stale numbers would mislead, and without History there are none to show.
            error = account["error"] or (None if self.history else "unavailable")
            rows = [] if error else self.history.latest_usage(tool, key)
            error = error or (None if rows else "no data yet")  # not "no limits": unknown
            out.append({"provider": tool, "label": account["short"] if count[tool] > 1 else None,
                        "panes": account["panes"], "error": error,
                        "windows": [self._window(tool, key, row, now) for row in rows]})
        return sorted(out, key=lambda a: (a["provider"], a["label"] or ""))
