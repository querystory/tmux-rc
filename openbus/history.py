"""Structural pane history, independent of browser visits and daemon restarts.

Full inventories make removals and empty fleets explicit. Unchanged inventories get a
heartbeat each minute; an outage is never silently carried forward. Imported logs are
partial evidence, stored separately so estimates cannot overwrite exact observations.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import socket
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)
HEARTBEAT = 60
COVERAGE = 120
BACKFILL_TTL = 4 * 3600
STATES = ("Needs you", "Running", "Idle", "Unknown", "Compacting", "Waiting")
AGENT_TOOLS = {"claude", "codex", "gemini", "opencode", "omp"}
MAX_SPAN = 90 * 86400  # bounds a request; the database itself is never pruned
GOAL_KEY = "running_goal"
LEAD_BUCKETS = 2880  # 24h of 5-minute buckets plus a 7d lead fits; a 90d lead at 1h does not


def duration(text: str) -> int:
    """'36h' or '7d' in seconds. ValueError for anything else, or beyond MAX_SPAN."""
    match = re.fullmatch(r"(\d{1,4})([hd])", text)
    seconds = int(match[1]) * (3600 if match[2] == "h" else 86400) if match else 0
    if not 0 < seconds <= MAX_SPAN:
        raise ValueError(text)
    return seconds


def default_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return Path(os.environ.get("TMUXRC_HISTORY_DB", state / "tmux-rc/history.sqlite3"))


def state_index(pane: dict) -> int:
    activity = pane.get("activity")
    if activity == "waiting" and pane.get("waiting_on") != "external": return 0
    if activity == "running": return 1
    if activity == "compacting": return 4
    if activity == "waiting": return 5
    if activity == "idle": return 2
    return 3


def agent_counts(pane: dict) -> dict:
    """Observed agents, not processes or CPU utilization; completed workers are gone."""
    foreground, background = [0] * len(STATES), [0] * len(STATES)
    if pane.get("tool") in AGENT_TOOLS:
        foreground[state_index(pane)] = 1
        subs = pane.get("subagents", [])
        if not isinstance(subs, list):
            background = None
        else:
            for agent in subs:
                if not isinstance(agent, dict) or agent.get("state") == "done":
                    continue
                background[state_index({"activity": agent.get("state"),
                                        "waiting_on": agent.get("waiting_on", "external")})] += 1
            # `agents` may come from the agent's own chrome (omp's "👥 N") when the parsed
            # roster missed workers: count the shortfall as running so history matches the card.
            run, compact = STATES.index("Running"), STATES.index("Compacting")
            busy = background[run] + background[compact]
            background[run] += max(0, (pane.get("agents") or 0) - busy)
    return {"foreground": foreground, "background": background}


def sum_counts(rows: list[dict], key: str) -> list[int] | None:
    # Missing legacy fields mean unmeasured, not zero. Empty inventories ARE zero.
    if any(row.get(key) is None for row in rows):
        return None
    return [sum(row[key][i] for row in rows) for i in range(len(STATES))]


def grouped(panes: list[dict]) -> tuple[list[int], list[dict]]:
    groups = {}
    counts = [0] * len(STATES)
    for p in panes:
        key = (p.get("session"), p.get("tool") or "other")
        group = groups.setdefault(key, {"session": key[0], "tool": key[1],
                                       "n": [0] * len(STATES), "members": []})
        group["n"][p["state"]] += 1
        group["members"].append(p)
        counts[p["state"]] += 1
    for group in groups.values():
        members = group.pop("members")
        for key in ("foreground", "background"):
            group[key] = sum_counts(members, key)
    return counts, list(groups.values())


def pane_key(server: str, pane_id: str, birth: str | None) -> str:
    """A pane identity that survives daemon restarts. The birth (pane pid) makes a
    recycled tmux pane id a different pane, so it never inherits the old one's rows."""
    return f"{server}:{pane_id}:{'unknown' if birth is None else birth}"


def _extend(db, now: float, payload_id: int) -> None:
    last = db.execute(
        "SELECT t, last_seen, payload_id FROM snapshot_intervals ORDER BY t DESC LIMIT 1",
    ).fetchone()
    if last and last[2] == payload_id and last[1] <= now <= last[1] + COVERAGE:
        db.execute("UPDATE snapshot_intervals SET last_seen=? WHERE t=?", (now, last[0]))
    else:
        db.execute("INSERT OR REPLACE INTO snapshot_intervals VALUES (?, ?, ?)",
                   (now, now, payload_id))


# Schema migrations, indexed by PRAGMA user_version. Databases from before versioning
# report 0 but may already have any of the first four applied, so those steps must stay
# idempotent. Append new steps; never edit or reorder a released one.
def _create_base(db) -> None:
    for sql in (
        "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        ("CREATE TABLE IF NOT EXISTS inventory_payloads ("
         "id INTEGER PRIMARY KEY, panes TEXT NOT NULL UNIQUE)"),
        ("CREATE TABLE IF NOT EXISTS snapshot_intervals ("
         "t REAL PRIMARY KEY, last_seen REAL NOT NULL, "
         "payload_id INTEGER NOT NULL REFERENCES inventory_payloads(id))"),
        ("CREATE TABLE IF NOT EXISTS log_observations ("
         "t REAL NOT NULL, uid TEXT NOT NULL, tool TEXT NOT NULL, "
         "state INTEGER NOT NULL CHECK(state BETWEEN 0 AND 5), valid_until REAL, "
         "PRIMARY KEY(t, uid))"),
    ):
        db.execute(sql)


def _add_valid_until(db) -> None:
    # Legacy imports lack proof of a pane lifetime. Retain them on disk, but
    # exclude them from charts until a verified re-import supplies bounds.
    columns = {r[1] for r in db.execute("PRAGMA table_info(log_observations)")}
    if "valid_until" not in columns:
        db.execute("ALTER TABLE log_observations ADD COLUMN valid_until REAL")


def _widen_states(db) -> None:
    # Expand the legacy constraint without rewriting or reinterpreting observations.
    schema = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='log_observations'",
    ).fetchone()[0]
    if "BETWEEN 0 AND 3" in schema:
        db.execute("ALTER TABLE log_observations RENAME TO old_log_observations")
        _create_base(db)
        db.execute("INSERT INTO log_observations SELECT * FROM old_log_observations")
        db.execute("DROP TABLE old_log_observations")


def _compress_snapshots(db) -> None:
    # Losslessly compress old heartbeats, including outages.
    legacy = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='snapshots' AND type='table'",
    ).fetchone()
    if legacy:
        db.execute("INSERT OR IGNORE INTO inventory_payloads(panes) "
                   "SELECT DISTINCT panes FROM snapshots")
        rows = db.execute(
            "SELECT s.t, p.id FROM snapshots s JOIN inventory_payloads p "
            "ON p.panes=s.panes ORDER BY s.t",
        )
        for now, payload_id in rows:
            _extend(db, now, payload_id)
        db.execute("DROP TABLE snapshots")
    db.execute("CREATE VIEW IF NOT EXISTS snapshots AS SELECT t, last_seen, panes "
               "FROM snapshot_intervals JOIN inventory_payloads ON payload_id=id")


def _add_checkpoints(db) -> None:
    # Current state, not history: one row per pane, replaced in place. See
    # docs/design/activity-clock-persistence.md.
    db.execute("CREATE TABLE pane_checkpoints ("
               "uid TEXT PRIMARY KEY, server TEXT NOT NULL, fp TEXT NOT NULL, "
               "last_activity_at REAL NOT NULL, idle_since REAL, card TEXT)")


def _add_plan_usage(db) -> None:
    # Plan-limit samples per account and window; see docs/design/plan-usage.md.
    db.execute("CREATE TABLE plan_usage (provider TEXT NOT NULL, account TEXT NOT NULL, "
               "window TEXT NOT NULL, seconds INTEGER NOT NULL, t REAL NOT NULL, "
               "pct REAL NOT NULL, resets_at REAL, PRIMARY KEY(provider, account, window, t))")


def _index_plan_usage(db) -> None:
    # The latest observation is looked up by time across windows; the key leads with window.
    db.execute("CREATE INDEX plan_usage_by_time ON plan_usage(provider, account, t)")


MIGRATIONS = (_create_base, _add_valid_until, _widen_states, _compress_snapshots,
              _add_checkpoints, _add_plan_usage, _index_plan_usage)


class History:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A private directory protects files throughout creation/recreation. Do not
        # chmod an arbitrary override parent (it might be /tmp or a shared directory).
        if path.parent.stat().st_mode & 0o077:
            raise ValueError("History requires a private directory (mode 0700)")
        # SQLite derives new WAL/SHM permissions from the main database. Set its
        # mode BEFORE the first connection, including an existing database upgrade.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        for suffix in ("-wal", "-shm"):
            try:
                Path(f"{path}{suffix}").chmod(0o600)
            except FileNotFoundError:
                pass
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")  # not allowed inside a transaction
            # IMMEDIATE takes the write lock before reading the version, so two daemons
            # starting together cannot both run the same step.
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise ValueError("History database was written by a newer tmux-rc")
            for step in MIGRATIONS[version:]:
                step(db)
            db.execute(f"PRAGMA user_version={len(MIGRATIONS)}")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('host', ?)", (socket.gethostname(),))
            host = db.execute("SELECT value FROM metadata WHERE key='host'").fetchone()[0]
            if host != socket.gethostname():
                raise ValueError("History database belongs to another host")
        path.chmod(0o600)
        self._last = None
        self._written = 0.0
        self._failed = 0.0

    @contextmanager
    def connect(self):
        # Per-operation connections: watcher writes from its worker, FastAPI reads from
        # another thread. WAL readers cannot block the daemon's next observation.
        db = sqlite3.connect(self.path, timeout=0.5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, states: list[dict], server: str, now: float | None = None,
               *, births: dict[str, str] | None = None) -> None:
        now = time.time() if now is None else now
        births = births or {}
        panes = sorted(({
            "uid": pane_key(server, p["pane_id"], births.get(p["pane_id"])),
            "session": p.get("session") or "",
            "tool": p.get("tool") or "other", "state": state_index(p),
            **agent_counts(p),
        } for p in states), key=lambda p: p["uid"])
        # Version the inventory itself so even an empty observation records capability.
        payload = json.dumps({"version": 2, "panes": panes},
                             separators=(",", ":"), sort_keys=True)
        if payload == self._last and now - self._written < HEARTBEAT: return
        if self._failed and now - self._failed < HEARTBEAT: return
        try:
            with self.connect() as db:
                db.execute("INSERT OR IGNORE INTO inventory_payloads(panes) VALUES (?)", (payload,))
                payload_id = db.execute(
                    "SELECT id FROM inventory_payloads WHERE panes=?", (payload,),
                ).fetchone()[0]
                _extend(db, now, payload_id)
        except (OSError, sqlite3.Error):
            self._failed = now
            logger.warning("Could not persist pane history; retrying in one minute", exc_info=True)
            return
        self._last, self._written, self._failed = payload, now, 0.0

    def load_checkpoints(self) -> dict[str, dict]:
        """Every pane checkpoint, read once at startup; memory is authoritative after."""
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM pane_checkpoints").fetchall()
        return {r["uid"]: {**dict(r), "card": r["card"] and json.loads(r["card"])}
                for r in rows}

    def save_checkpoints(self, rows: list[dict], now: float | None = None) -> bool:
        """Upsert pane checkpoints. A fingerprint the row already holds keeps its
        earliest times, so a second daemon re-seeing the same screen cannot move them."""
        now = time.time() if now is None else now
        if self._failed and now - self._failed < HEARTBEAT: return False
        try:
            with self.connect() as db:
                db.executemany(
                    "INSERT INTO pane_checkpoints VALUES (:uid, :server, :fp, "
                    ":last_activity_at, :idle_since, :card) "
                    "ON CONFLICT(uid) DO UPDATE SET last_activity_at=CASE WHEN fp=excluded.fp "
                    "THEN min(last_activity_at, excluded.last_activity_at) "
                    "ELSE excluded.last_activity_at END, fp=excluded.fp, "
                    "idle_since=CASE WHEN fp=excluded.fp AND idle_since IS NOT NULL "
                    "AND excluded.idle_since IS NOT NULL "
                    "THEN min(idle_since, excluded.idle_since) ELSE excluded.idle_since END, "
                    "card=excluded.card",
                    [{**r, "card": r["card"] and json.dumps(r["card"], default=str)}
                     for r in rows],
                )
        except (OSError, sqlite3.Error):
            self._failed = now
            logger.warning("Could not checkpoint panes; retrying in one minute", exc_info=True)
            return False
        self._failed = 0.0
        return True

    def delete_checkpoints(self, uids: list[str]) -> None:
        with self.connect() as db:
            db.executemany("DELETE FROM pane_checkpoints WHERE uid=?", [(u,) for u in uids])

    def record_usage(self, rows: list[tuple]) -> None:
        """(provider, account, window, seconds, t, pct, resets_at) rows. A Codex sample is
        stamped with its log event's time, so re-reading the same event is a no-op."""
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO plan_usage VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    def latest_usage(self, provider: str, account: str) -> list[tuple]:
        """(window, seconds, t, pct, resets_at) of the latest observation. One read stamps
        all its windows alike, so a window a plan change dropped is left behind with it."""
        with self.connect() as db:
            return db.execute("SELECT window, seconds, t, pct, resets_at FROM plan_usage "
                              "WHERE provider=?1 AND account=?2 AND t=(SELECT max(t) FROM "
                              "plan_usage WHERE provider=?1 AND account=?2) ORDER BY window",
                              (provider, account)).fetchall()

    def usage_between(self, provider: str, account: str, window: str,
                      since: float, until: float) -> list[tuple]:
        with self.connect() as db:
            return db.execute("SELECT t, pct FROM plan_usage WHERE provider=? AND account=? "
                              "AND window=? AND t BETWEEN ? AND ? ORDER BY t",
                              (provider, account, window, since, until)).fetchall()

    def import_logs(self, observations: list[tuple]) -> int:
        """Idempotent, transactional import. Raw screen/summary text is never stored."""
        with self.connect() as db:
            before = db.total_changes
            db.executemany(
                "INSERT INTO log_observations VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(t, uid) DO UPDATE SET tool=excluded.tool, state=excluded.state, "
                "valid_until=excluded.valid_until WHERE log_observations.valid_until IS NULL",
                observations,
            )
            return db.total_changes - before

    def set_goal(self, goal: int | None) -> None:
        """The fleet's running goal: one shared number, so every device draws the same line."""
        with self.connect() as db:
            db.execute("DELETE FROM metadata WHERE key=?", (GOAL_KEY,))
            if goal is not None:
                db.execute("INSERT INTO metadata VALUES (?, ?)", (GOAL_KEY, str(goal)))

    def query(self, window: str = "24h", now: float | None = None, lead: str | None = None) -> dict:
        """`window` sets the bucket size; `lead` adds that much earlier history at the same
        bucket size, so a client can average over a window that starts before the chart."""
        now = time.time() if now is None else now
        with self.connect() as db:
            live_start = db.execute("SELECT min(t) FROM snapshots").fetchone()[0]
            log_start = db.execute(
                "SELECT min(t) FROM log_observations WHERE valid_until IS NOT NULL",
            ).fetchone()[0]
            first = min((t for t in (live_start, log_start) if t is not None), default=now)
            span = max(60, now - first) if window == "all" else duration(window)
            start = max(first, now - span)
            # Bound the response even for years of history. Five-minute bins at 24h,
            # with smaller bins while the database is young.
            # The lead shares the window's bucket size until the total passes LEAD_BUCKETS;
            # past that the buckets coarsen, so no window/lead pair can outgrow the bound.
            lead_span = duration(lead) if lead else 0
            desired = max(60, (now - start) / 360, (now - start + lead_span) / LEAD_BUCKETS)
            step = next((s for s in (60, 300, 900, 1800, 3600, 21600, 86400) if s >= desired),
                        math.ceil(desired / 86400) * 86400)
            start = math.floor(max(first, start - lead_span) / step) * step
            goal = db.execute("SELECT value FROM metadata WHERE key=?", (GOAL_KEY,)).fetchone()
            records = db.execute(
                "SELECT t, uid, tool, state, valid_until FROM log_observations "
                "WHERE t>=? AND t<=? AND valid_until IS NOT NULL ORDER BY t, uid",
                (start - BACKFILL_TTL, min(now, live_start) if live_start else now),
            ).fetchall()
            samples, known, cursor = [], {}, 0
            for bucket in range(int(start), int(now) + 1, step):
                at = min(bucket + step - 0.001, now)
                live = db.execute(
                    "SELECT last_seen, panes FROM snapshots WHERE t<=? ORDER BY t DESC LIMIT 1",
                    (at,),
                ).fetchone()
                source, panes, versioned = "gap", None, False
                if live and at - live[0] <= COVERAGE:
                    source, payload = "daemon", json.loads(live[1])
                    versioned = isinstance(payload, dict) and payload.get("version") == 2
                    panes = payload["panes"] if versioned else payload
                elif live_start is None or at < live_start:
                    while cursor < len(records) and records[cursor][0] <= at:
                        t, uid, tool, state, valid_until = records[cursor]
                        known[uid] = {"t": t, "uid": uid, "tool": tool, "state": state,
                                      "session": None,
                                      "valid_until": valid_until}
                        cursor += 1
                    panes = [p for p in known.values()
                             if at - p["t"] <= BACKFILL_TTL and at < p["valid_until"]]
                    if panes: source = "logs"
                    else: panes = None
                counts, groups = grouped(panes) if panes is not None else (None, [])
                samples.append({"t": bucket * 1000, "n": counts,
                                "groups": groups, "source": source,
                                **{key: sum_counts(groups, key) if panes or versioned else None
                                   for key in ("foreground", "background")}})
        return {"samples": samples, "step": step * 1000, "first": first * 1000,
                "goal": int(goal[0]) if goal else None,
                "states": STATES, "backfill_ttl": BACKFILL_TTL,
                "backfill_note": ("Log reconstruction (lighter bars) is partial; "
                                  "matched states carried "
                                  "forward up to 4 hours within verified pane lifetimes. "
                                  "Historical tmux sessions are unknown.")}
