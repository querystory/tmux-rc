"""Claude and Codex plan limits: how much of each account's 5h and 7d windows is used.

One account per config dir: a pane's agent reads CLAUDE_CONFIG_DIR or CODEX_HOME (else
HOME) from its environment, so two panes may draw on two plans. Codex writes its limits
into its own session logs, so reading them needs no credential. Claude publishes them
only through an undocumented OAuth endpoint, polled at most every CLAUDE_TTL per account.
An omp pane counts toward the Claude accounts its omp profile is signed in to, as omp's own
usage report names them.
See docs/design/plan-usage.md.
"""
from __future__ import annotations

import asyncio
import heapq
import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from . import tmux

logger = logging.getLogger(__name__)
POLL = 60  # Codex logs are local and cheap; the Claude endpoint is still CLAUDE_TTL
CLAUDE_TTL = 300
# How old a reading may be and still stand in for a failed read: three Claude polls, so one
# or two transient failures in a row keep the meter up (a 5h window moves a few points in
# that time), while an outage, or an expired token nobody refreshes, still reads unavailable.
STALE_OK = 3 * CLAUDE_TTL
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_WINDOWS = {"five_hour": ("5h", 5 * 3600), "seven_day": ("7d", 7 * 86400)}
ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
OMP = ["usage", "--json", "--provider", "anthropic"]  # ~1s; every provider takes ~15s
TAIL = 1 << 20  # Codex session logs reach hundreds of MB; the newest limits sit at the end
CODEX_FILES = 20  # bounds the scan when recent logs hold no limits event at all
SPARK_POINTS = 120  # samples sent per window: a sidebar sparkline's width, not a week of polls


def window_name(minutes: int) -> str:
    return f"{minutes // 1440}d" if minutes % 1440 == 0 else f"{minutes // 60}h"


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def canonical(config: str) -> str:
    """One spelling per dir: ~ expanded and "..", "." and doubled slashes folded, so an
    alias can't list one login twice, or miss its profile (accounts.py). normpath keeps a
    leading "//" (POSIX leaves its meaning to the system); on Linux it is just "/"."""
    path = os.path.normpath(os.path.expanduser(config))
    return "/" + path.lstrip("/") if path.startswith("//") else path


def home(provider: str, env: dict) -> Path:
    """The config dir an agent with this environment reads: its override, else ~/.claude
    or ~/.codex under its HOME."""
    default = Path(env.get("HOME") or Path.home()) / f".{provider}"
    return Path(canonical(env.get(ENV[provider]) or str(default)))


def _agent(pid: str, tool: str) -> str:
    """The tool's own process under the pane, by name or the script bun/node runs, else the
    pane's (an inline `VAR=… tool` sets VAR only in the tool's process)."""
    return next((p for p in tmux.processes(pid)
                 if tool in (tmux.proc_read(p, "comm").strip(), tmux.program(p))), pid)


def _environ(pid: str, keys: tuple[str, ...]) -> dict:
    """Just these variables of the process; every other one is dropped unread."""
    pairs = (v.partition("=") for v in tmux.proc_read(pid, "environ").split("\0"))
    return {k: v for k, _, v in pairs if k in keys}


def pane_env(pid: str, tool: str) -> dict:
    """HOME and the tool's config override, from the agent process under the pane."""
    return _environ(_agent(pid, tool), ("HOME", ENV[tool]))


def omp_launch(pid: str) -> tuple[list[str], tuple]:
    """How the pane's omp runs, to run it the same way: its executable, then up to the omp
    script a wrapper or bun runs, from the pane's cwd ([] if none is found); and whose
    logins it holds, as (variable, value) pairs: its HOME, and its profile (--profile,
    else OMP_PROFILE, else omp's default). The daemon's PATH need not hold omp (bun
    installs it in ~/.bun/bin), and omp keeps each profile's logins in its own store
    under HOME, not in a Claude dir."""
    agent = _agent(pid, "omp")
    argv = tmux.proc_read(agent, "cmdline").split("\0")
    at = next((i for i, a in enumerate(argv) if os.path.basename(a) == "omp"), None)
    try:  # at 0 omp is the executable itself; else the script, which may be relative
        exe = os.readlink(f"/proc/{agent}/exe")  # replaced since it started: the link runs it
        exe = exe if os.path.exists(exe) else f"/proc/{agent}/exe"
        launch = [exe, *argv[1:at], os.path.join(
            os.readlink(f"/proc/{agent}/cwd"), argv[at])][:at + 1] if at is not None else []
    except OSError:  # gone, or not ours
        launch = []
    env = _environ(agent, ("HOME", "OMP_PROFILE"))
    if flag := re.search(r"\0--profile[=\0]([^\0]*)", "\0" + "\0".join(argv)):
        env["OMP_PROFILE"] = flag[1]
    return launch, tuple(sorted(env.items()))


def read_omp(launch: list[str], who: tuple) -> set[str] | None:
    """The Anthropic account ids these logins hold, from omp's own usage report (omp reads
    its credentials; tmux-rc never does). None when the read fails."""
    env = {k: v for k, v in os.environ.items() if k != "OMP_PROFILE"}
    try:
        out = subprocess.run([*launch, *OMP], capture_output=True, text=True, timeout=20,
                             env={**env, **dict(who)}, check=True).stdout
        return {r["metadata"]["accountId"] for r in json.loads(out)["reports"]
                if r.get("provider") == "anthropic" and (r.get("metadata") or {}).get("accountId")}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as e:
        logger.info("omp usage unreadable: %s", type(e).__name__)
        return None


_omp: dict[tuple, tuple[float, set[str]]] = {}  # omp_launch's who -> (read at, account ids)


def omp_accounts(launch: list[str], who: tuple, fresh: bool = False) -> set[str]:
    """These logins' Anthropic accounts as last read; `fresh` rereads them once a POLL is
    past (PlanUsage's thread only: a request must not wait on omp). A failed read keeps
    the last answer."""
    read_at, ids = _omp.get(who, (0.0, set()))
    if fresh and launch and time.time() - read_at >= POLL:
        _omp[who] = (time.time(), ids)  # a slow or failing omp is asked once a POLL
        if (got := read_omp(launch, who)) is not None:
            _omp[who] = (time.time(), ids := got)
    return ids


def pane_envs(panes: list[dict], birth, claude_dirs=(), fresh=False) -> list[tuple[str, dict, str]]:
    """(tool, pane_env, pane id) for each live Claude or Codex pane. An omp pane counts as
    a Claude pane on each of `claude_dirs` signed in to an account its profile holds: omp
    may rotate between them, and which one a pane is on now is not exposed."""
    out = []
    for p in panes:
        tool, pane_id = p.get("tool"), p["pane_id"]
        if tool in ENV and (pid := birth(pane_id)):
            out.append((tool, pane_env(pid, tool), pane_id))
        elif tool == "omp" and claude_dirs and (pid := birth(pane_id)):
            ids = omp_accounts(*omp_launch(pid), fresh)
            out += [("claude", env_for("claude", d), pane_id) for d in claude_dirs
                    if oauth_account(d, env_for("claude", d)).get("accountUuid") in ids]
    return out


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
    their own log, so the newest event wins, not the newest-touched file; a log last
    written before that event cannot hold a newer one, which ends the scan."""
    # Every log is stat'ed (a long session keeps writing a file in an old date dir), but
    # only the newest CODEX_FILES are kept: ~1ms for a few hundred logs.
    files = heapq.nlargest(CODEX_FILES, (codex_home / "sessions").glob("*/*/*/*.jsonl"),
                           key=lambda f: f.stat().st_mtime)
    best: list[dict] = []
    for path in files:
        if best and path.stat().st_mtime < best[0]["t"]:
            break
        if (samples := _last_limits(path)) and (not best or samples[0]["t"] > best[0]["t"]):
            best = samples
    return best


def claude_samples(data: dict, now: float) -> list[dict]:
    """The OAuth usage response's windows. A null window is one not yet opened: 0%. A
    per-model weekly limit ("7d Fable") is only in `limits`, as a weekly_scoped entry
    naming its model; the seven_day_<model> keys beside it stay null on current plans."""
    windows = []
    for key, (name, seconds) in CLAUDE_WINDOWS.items():
        if key not in data:  # a changed shape reads unavailable, never as 0%
            raise KeyError(key)
        w = data[key] or {}
        windows.append((name, seconds, w.get("utilization"), w.get("resets_at")))
    for limit in data.get("limits") or []:
        model = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
        if limit.get("kind") == "weekly_scoped" and model:
            windows.append((f"7d {model}", 7 * 86400, limit.get("percent"), limit.get("resets_at")))
    return [{"window": name, "seconds": seconds, "t": now, "pct": float(pct or 0),
             "resets_at": resets and _epoch(resets)} for name, seconds, pct, resets in windows]


def env_for(provider: str, config: Path) -> dict:
    """The environment that points the agent at this config dir: none for the provider's
    default (~/.claude keeps its .claude.json beside it, not in it), else the override."""
    return {} if config == Path.home() / f".{provider}" else {ENV[provider]: str(config)}


def oauth_account(config: Path, env: dict) -> dict:
    """The non-secret identity Claude Code keeps beside its credentials: .claude.json
    inside an overridden config dir, else next to the default one. {} when signed out."""
    meta = config / ".claude.json" if env.get(ENV["claude"]) else config.parent / ".claude.json"
    try:
        account = json.loads(meta.read_text()).get("oauthAccount")
    except (OSError, ValueError, AttributeError):
        return {}
    return account if isinstance(account, dict) else {}


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

    def get() -> list[dict]:
        with _OPENER.open(request, timeout=10) as response:
            return claude_samples(json.load(response), now)
    try:
        return get()
    except urllib.error.HTTPError:  # a status is an answer: a 401 won't fix itself in 2s
        raise
    except (urllib.error.URLError, TimeoutError):  # a network blip: one retry
        time.sleep(2)
        return get()


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


def thin(samples: list[tuple]) -> list[list[float]]:
    """At most SPARK_POINTS of these (t, pct) samples, newest kept, in ms."""
    step = max(1, -(-len(samples) // SPARK_POINTS))
    return [[t * 1000, p] for t, p in samples[::-1][::step][::-1]]


def ended(name: str, seconds: int, rows: list[tuple], now: float) -> list[dict]:
    """The windows these (t, pct, resets_at) samples show ending by now, oldest first,
    shaped like a current window: its final %, its trend, and when it reached 100%
    (`limit_at`). A window is told by its reset; consecutive ones are at least 5h apart, so
    a reset an hour or less from the last is the same window read again, and its latest
    read stands for it."""
    windows: list[list] = []
    for t, pct, resets in rows:
        if resets is None:  # not yet opened
            continue
        if not windows or resets > windows[-1][0] + 3600:
            windows.append([resets, []])
        windows[-1][0] = resets
        windows[-1][1].append((t, pct))
    return [{"window": name, "pct": max(p for _, p in samples), "resets_at": resets * 1000,
             "start": (resets - seconds) * 1000, "samples": thin(samples),
             "limit_at": next((t * 1000 for t, p in samples if p >= 100), None)}
            for resets, samples in windows if resets <= now]


class PlanUsage:
    """Discovers accounts from the panes, samples them into History, reports for the UI."""

    def __init__(self, history, fetch=fetch_claude, read=read_codex, profiles=None):
        self.history, self.fetch, self.read = history, fetch, read
        # The config dirs worth metering with no pane on them: the daemon passes the listed
        # accounts; alone, each provider's default dir.
        self.profiles = profiles or (lambda: [(t, Path.home() / f".{t}") for t in ENV])
        self.accounts: dict[tuple[str, str], dict] = {}
        self._fetched: dict[str, float] = {}
        self._errors: dict[tuple[str, str], str | None] = {}

    def discover(self, panes: list[dict], birth) -> dict[tuple[str, str], dict]:
        """Every account a live agent pane uses, plus the known profiles' (accounts.py)."""
        found: dict[tuple[str, str], dict] = {}
        listed = self.profiles()
        sources = [(t, env_for(t, d), None) for t, d in listed] + pane_envs(
            panes, birth, [d for t, d in listed if t == "claude"], fresh=True)
        for tool, env, pane_id in sources:
            config = home(tool, env)
            if tool == "codex":  # a home that has logged nothing yet reads "no data yet"
                ident = config.is_dir() and {"key": str(config)}
            else:
                uuid = oauth_account(config, env).get("accountUuid")
                ident = uuid and {"key": uuid}
            if ident:
                account = found.setdefault((tool, ident["key"]), {**ident, "homes": [],
                                                                  "panes": []})
                account["homes"] += [config] if config not in account["homes"] else []
                account["panes"] += [pane_id] if pane_id not in (None, *account["panes"]) else []
        return found

    def _fetch_any(self, homes: list[Path], now: float) -> list[dict]:
        """One account can be logged in under several config dirs, and one dir's token may
        be stale while another's works: the first that answers wins."""
        for config in homes[:-1]:
            with suppress(Exception):
                return self.fetch(config, now)
        return self.fetch(homes[-1], now)

    def poll(self, panes: list[dict], birth, now: float | None = None) -> None:
        now = time.time() if now is None else now
        accounts = self.discover(panes, birth)
        rows = []
        for (tool, key), account in accounts.items():
            # The last fetch's verdict outlives the account's absence from one poll.
            account["error"] = self._errors.get((tool, key))
            try:
                if tool == "codex":
                    samples, account["error"] = self.read(account["homes"][0]), None
                elif now - self._fetched.get(key, 0) >= CLAUDE_TTL:
                    self._fetched[key] = now
                    samples, account["error"] = self._fetch_any(account["homes"], now), None
                else:
                    samples = []
            except Exception as e:  # noqa: BLE001 - any failure reads as "unavailable"
                # The type only: an HTTP error's text could echo request details.
                logger.info("plan usage unavailable for a %s account: %s", tool, type(e).__name__)
                samples, account["error"] = [], "unavailable"
            self._errors[(tool, key)] = account["error"]
            rows += [(tool, key, s["window"], s["seconds"], s["t"], s["pct"], s["resets_at"])
                     for s in samples]
        self.accounts = accounts  # first: a failed write (logged by run()) loses only samples
        if rows and self.history:
            self.history.record_usage(rows)

    async def run(self, watcher) -> None:
        while True:
            try:
                await asyncio.to_thread(self.poll, list(watcher.states), watcher.pane_birth)
            except Exception:  # one bad poll must not end sampling
                logger.warning("plan usage poll failed", exc_info=True)
            await asyncio.sleep(POLL)

    def _window(self, tool: str, key: str, row: tuple, now: float, past: float) -> dict:
        """One window for the UI, in ms: its latest %, its trend thinned to SPARK_POINTS,
        and the projection over every sample; with `past` (days), the windows that ended
        in that time too, for the usage view's sawtooth."""
        name, seconds, observed, pct, resets = row
        out = {"window": name}
        if past:  # from a window's length earlier, so the oldest tooth starts at its start
            since = now - past * 86400
            recent = self.history.usage_between(tool, key, name, since - seconds, now)
            out["past"] = [w for w in ended(name, seconds, recent, now)
                           if w["resets_at"] >= since * 1000]
        if resets is None or resets <= now:  # the window reset since: nothing used yet
            return {**out, "pct": 0.0, "resets_at": None, "samples": []}
        # Up to the row's own observation: a poll landing in between can't skew the trend.
        samples = [(t, p) for t, p, _ in
                   self.history.usage_between(tool, key, name, resets - seconds, observed)]
        trend = project(samples, resets, seconds, now)
        return {**out, "pct": pct, "resets_at": resets * 1000,
                "start": (resets - seconds) * 1000, "samples": thin(samples),
                "projected": trend["projected"],
                "limit_at": trend["limit_at"] and trend["limit_at"] * 1000,
                # When it reached 100%, from every sample: the thinned trend can miss it by hours.
                "hit_at": next((t * 1000 for t, p in samples if p >= 100), None)}

    def report(self, now: float | None = None, past: float = 0) -> list[dict]:
        now = time.time() if now is None else now
        accounts = self.accounts  # one snapshot: poll() swaps it from a worker thread
        out = []
        for (tool, key), account in accounts.items():
            # A failed read keeps showing the last reading while it is recent (STALE_OK),
            # marked with its age; older numbers would mislead. No History: none to show.
            rows = self.history.latest_usage(tool, key) if self.history else []
            newest = max((r[2] for r in rows), default=0)
            if account["error"] and now - newest > STALE_OK:
                rows = []
            error = None if rows else account["error"] or (
                "no data yet" if self.history else "unavailable")  # not "no limits": unknown
            # Named by the UI, from the profile its dirs belong to (accounts.py).
            out.append({"provider": tool, "dirs": [str(h) for h in account.get("homes", [])],
                        "panes": account["panes"], "error": error,
                        "as_of": newest * 1000 if rows and account["error"] else None,
                        "windows": [self._window(tool, key, row, now, past) for row in rows]})
        return sorted(out, key=lambda a: (a["provider"], a["dirs"]))
