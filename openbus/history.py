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
import socket
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)
HEARTBEAT = 60
COVERAGE = 120
BACKFILL_TTL = 4 * 3600
STATES = ("Needs you", "Running", "Idle", "Unknown")


def default_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return Path(os.environ.get("TMUXRC_HISTORY_DB", state / "tmux-rc/history.sqlite3"))


def state_index(pane: dict) -> int:
    activity = pane.get("activity")
    if activity == "waiting" and pane.get("waiting_on") != "external": return 0
    if activity in {"running", "compacting", "waiting"}: return 1
    if activity == "idle": return 2
    return 3


def grouped(panes: list[dict]) -> tuple[list[int], list[dict]]:
    groups = {}
    counts = [0, 0, 0, 0]
    for p in panes:
        key = (p.get("session") or "", p.get("tool") or "other")
        group = groups.setdefault(key, {"session": key[0], "tool": key[1], "n": [0, 0, 0, 0]})
        group["n"][p["state"]] += 1
        counts[p["state"]] += 1
    return counts, list(groups.values())


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
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inventory_payloads (
                    id INTEGER PRIMARY KEY, panes TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS snapshot_intervals (
                    t REAL PRIMARY KEY, last_seen REAL NOT NULL,
                    payload_id INTEGER NOT NULL REFERENCES inventory_payloads(id));
                CREATE TABLE IF NOT EXISTS log_observations (
                    t REAL NOT NULL, uid TEXT NOT NULL, tool TEXT NOT NULL,
                    state INTEGER NOT NULL CHECK(state BETWEEN 0 AND 3),
                    valid_until REAL,
                    PRIMARY KEY(t, uid));
            """)
            # Legacy imports lack proof of a pane lifetime. Retain them on disk, but
            # exclude them from charts until a verified re-import supplies bounds.
            columns = {r[1] for r in db.execute("PRAGMA table_info(log_observations)")}
            if "valid_until" not in columns:
                db.execute("ALTER TABLE log_observations ADD COLUMN valid_until REAL")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('host', ?)", (socket.gethostname(),))
            host = db.execute("SELECT value FROM metadata WHERE key='host'").fetchone()[0]
            if host != socket.gethostname():
                raise ValueError("History database belongs to another host")
            self._migrate_snapshots(db)
        path.chmod(0o600)
        self._last = None
        self._written = 0.0
        self._failed = 0.0

    @staticmethod
    def _extend(db, now: float, payload_id: int) -> None:
        last = db.execute(
            "SELECT t, last_seen, payload_id FROM snapshot_intervals ORDER BY t DESC LIMIT 1",
        ).fetchone()
        if last and last[2] == payload_id and last[1] <= now <= last[1] + COVERAGE:
            db.execute("UPDATE snapshot_intervals SET last_seen=? WHERE t=?", (now, last[0]))
        else:
            db.execute("INSERT OR REPLACE INTO snapshot_intervals VALUES (?, ?, ?)",
                       (now, now, payload_id))

    @classmethod
    def _migrate_snapshots(cls, db) -> None:
        # Losslessly compress old heartbeats in one transaction, including outages.
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
                cls._extend(db, now, payload_id)
            db.execute("DROP TABLE snapshots")
        db.execute("CREATE VIEW IF NOT EXISTS snapshots AS SELECT t, last_seen, panes "
                   "FROM snapshot_intervals JOIN inventory_payloads ON payload_id=id")

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
            "uid": f"{server}:{p['pane_id']}:{births.get(p['pane_id'], 'unknown')}",
            "session": p.get("session") or "",
            "tool": p.get("tool") or "other", "state": state_index(p),
        } for p in states), key=lambda p: p["uid"])
        payload = json.dumps(panes, separators=(",", ":"), sort_keys=True)
        if payload == self._last and now - self._written < HEARTBEAT: return
        if self._failed and now - self._failed < HEARTBEAT: return
        try:
            with self.connect() as db:
                db.execute("INSERT OR IGNORE INTO inventory_payloads(panes) VALUES (?)", (payload,))
                payload_id = db.execute(
                    "SELECT id FROM inventory_payloads WHERE panes=?", (payload,),
                ).fetchone()[0]
                self._extend(db, now, payload_id)
        except (OSError, sqlite3.Error):
            self._failed = now
            logger.warning("Could not persist pane history; retrying in one minute", exc_info=True)
            return
        self._last, self._written, self._failed = payload, now, 0.0

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

    def query(self, window: str = "24h", now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.connect() as db:
            live_start = db.execute("SELECT min(t) FROM snapshots").fetchone()[0]
            log_start = db.execute(
                "SELECT min(t) FROM log_observations WHERE valid_until IS NOT NULL",
            ).fetchone()[0]
            first = min((t for t in (live_start, log_start) if t is not None), default=now)
            span = {"1h": 3600, "24h": 86400, "7d": 604800, "all": max(60, now - first)}[window]
            start = max(first, now - span)
            # Bound the response even for years of history. Five-minute bins at 24h,
            # with smaller bins while the database is young.
            desired = max(60, (now - start) / 360)
            step = next((s for s in (60, 300, 900, 1800, 3600, 21600, 86400) if s >= desired),
                        math.ceil(desired / 86400) * 86400)
            start = math.floor(start / step) * step
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
                source, panes = "gap", None
                if live and at - live[0] <= COVERAGE:
                    source, panes = "daemon", json.loads(live[1])
                elif live_start is None or at < live_start:
                    while cursor < len(records) and records[cursor][0] <= at:
                        t, uid, tool, state, valid_until = records[cursor]
                        known[uid] = {"t": t, "uid": uid, "tool": tool, "state": state,
                                      "session": "(historical session unknown)",
                                      "valid_until": valid_until}
                        cursor += 1
                    panes = [p for p in known.values()
                             if at - p["t"] <= BACKFILL_TTL and at < p["valid_until"]]
                    if panes: source = "logs"
                    else: panes = None
                counts, groups = grouped(panes) if panes is not None else (None, [])
                samples.append({"t": bucket * 1000, "n": counts,
                                "groups": groups, "source": source})
        return {"samples": samples, "step": step * 1000, "first": first * 1000,
                "states": STATES, "backfill_ttl": BACKFILL_TTL,
                "backfill_note": ("Log reconstruction (lighter bars) is partial; "
                                  "matched states carried "
                                  "forward up to 4 hours within verified pane lifetimes. "
                                  "Historical tmux sessions are unknown.")}
