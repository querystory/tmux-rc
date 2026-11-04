"""Optional, complete worker inventories supplied by a local orchestrator."""
from __future__ import annotations

import json
from pathlib import Path

STALE_SECONDS = 30
MAX_BYTES = 1 << 20
ACTIVITIES = frozenset({
    "running", "idle", "waiting", "compacting", "quota_blocked", "unknown", "done",
})


def validate(value: object, now: float) -> dict:
    """Validate an entire snapshot before accepting any of its observations."""
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("unsupported worker snapshot")
    for field in ("source_id", "run_id"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError("missing source or run identity")
    stamp = value.get("observed_at")
    if type(stamp) not in (int, float) or not 0 <= stamp <= now + 5:
        raise ValueError("invalid observation time")
    if type(value.get("sequence")) is not int or value["sequence"] < 0:
        raise ValueError("invalid sequence")
    controllers, workers = value.get("controllers"), value.get("workers")
    if not isinstance(controllers, list) or not isinstance(workers, list):
        raise TypeError("complete controllers and workers required")
    controller_ids = set()
    for binding in controllers:
        _binding(binding)
        if binding["pane_id"] in controller_ids:
            raise ValueError("duplicate controller")
        controller_ids.add(binding["pane_id"])
    ids, panes = set(), set()
    for worker in workers:
        if not isinstance(worker, dict):
            raise TypeError("invalid worker")
        uid = worker.get("id")
        if not isinstance(uid, str) or not uid or uid in ids:
            raise ValueError("duplicate or missing worker identity")
        ids.add(uid)
        if worker.get("controller_pane_id") not in controller_ids:
            raise ValueError("unregistered controller")
        if worker.get("activity") not in ACTIVITIES:
            raise ValueError("invalid worker activity")
        if not isinstance(worker.get("tool"), str) or not worker["tool"]:
            raise ValueError("missing worker harness")
        if worker.get("waiting_on") not in (None, "user", "external"):
            raise ValueError("invalid waiting target")
        if worker.get("activity") == "waiting" and worker.get("waiting_on") is None:
            raise ValueError("waiting worker requires a target")
        if worker.get("pane") is not None:
            _binding(worker["pane"])
            key = tuple(worker["pane"][k] for k in ("server", "pane_id", "birth"))
            if key in panes:
                raise ValueError("multiple workers claim one pane")
            panes.add(key)
    return value


def _binding(value: object) -> None:
    if not isinstance(value, dict) or any(
        not isinstance(value.get(k), str) or not value[k] for k in ("pane_id", "birth", "server")
    ):
        raise ValueError("complete pane lifetime required")


def is_stale(snapshot: dict, now: float) -> bool:
    """Whether a roster is too old to vouch for its workers' activity."""
    return now - snapshot["observed_at"] > STALE_SECONDS


def reconcile(snapshot: dict, states: list[dict], server: str | None,
              now: float) -> tuple[list[dict], list[dict]]:
    """Bind only current pane lifetimes and keep the parser's own records untouched."""
    panes = {p["pane_id"]: p for p in states}

    def bound(binding: dict) -> bool:
        pane = panes.get(binding["pane_id"])
        return bool(pane and server == binding["server"] and pane.get("birth") == binding["birth"])

    controllers = {c["pane_id"] for c in snapshot["controllers"] if bound(c)}
    stale = is_stale(snapshot, now)
    workers = [{
        "id": w["id"], "source": snapshot["source_id"], "run_id": snapshot["run_id"],
        "tool": w["tool"], "activity": "unknown" if stale else w["activity"],
        "waiting_on": None if stale else w.get("waiting_on"),
        "session": panes[w["controller_pane_id"]].get("session", ""),
        "controller_pane_id": w["controller_pane_id"],
        "pane_id": w["pane"]["pane_id"] if w.get("pane") and bound(w["pane"]) else None,
    } for w in snapshot["workers"]
        if w["controller_pane_id"] in controllers and w["activity"] != "done"]
    published = [{**p, "subagents": [], "agents": 0, "worker_source": snapshot["source_id"]}
                 if p["pane_id"] in controllers else dict(p) for p in states]
    return published, sorted(workers, key=lambda w: w["id"])


class Reader:
    """The last accepted complete inventory ages normally through transient read errors."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.snapshot: dict | None = None
        self.retired: set[tuple[str, str]] = set()

    def read(self, now: float) -> str:
        try:
            self.snapshot = self._load(now)
        except (OSError, ValueError, TypeError, RecursionError):
            stale = self.snapshot is not None and is_stale(self.snapshot, now)
            return "stale" if stale else "unavailable"
        return "stale" if is_stale(self.snapshot, now) else "fresh"

    def _load(self, now: float) -> dict:
        """The file's snapshot if it may replace the current one; raises otherwise."""
        with self.path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("worker snapshot too large")
        candidate = validate(json.loads(raw), now)
        self._accept(candidate, (candidate["source_id"], candidate["run_id"]))
        return candidate

    def _accept(self, candidate: dict, identity: tuple[str, str]) -> None:
        if identity in self.retired:
            raise ValueError("retired run")
        previous = self.snapshot
        if previous is None:
            return
        old_identity = previous["source_id"], previous["run_id"]
        if candidate["observed_at"] < previous["observed_at"]:
            raise ValueError("time moved backward")
        if identity == old_identity:
            if candidate["sequence"] < previous["sequence"]:
                raise ValueError("out of order snapshot")
            if candidate["sequence"] == previous["sequence"] and candidate != previous:
                raise ValueError("sequence reused")
        elif candidate["observed_at"] <= previous["observed_at"]:
            raise ValueError("older run")
        else:
            self.retired.add(old_identity)
