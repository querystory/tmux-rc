"""Reboot restore (docs/design/reboot-restore.md). Once a minute, a snapshot of each tmux
server's workspace: sessions, windows, pane order, directories, and the conversation each
agent pane holds. When that server dies, its snapshot is offered back.

A snapshot is current state, not history: one row per server, replaced in place, its
payload rewritten only when the layout changes and its last-seen stamp every minute, since
that stamp stands for when the server died. Nothing from a pane's command line or
environment is stored; a resume command is rebuilt from agent-history at restore time."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import time
from datetime import datetime

from . import agent_history, tmux
from .history import AGENT_TOOLS

logger = logging.getLogger(__name__)
EVERY = 60
KEEP = 30 * 86400  # a dead server's snapshot is offered this long, then dropped
RECENT = 86400  # pane defaults: active this close to the server's death is recent
_SHELLS = frozenset({"bash", "zsh", "fish", "sh"})

_WINDOWS = ("#{session_name}\t#{session_path}\t#{session_group}\t#{window_id}\t#{window_index}"
            "\t#{window_active}\t#{window_name}\t#{automatic-rename}\t#{window_zoomed_flag}"
            "\t#{window_layout}")
_PANES = ("#{window_id}\t#{pane_id}\t#{pane_index}\t#{pane_active}\t#{pane_pid}"
          "\t#{pane_current_path}\t#{pane_current_command}")


def identity() -> str:
    """'<boot_id>:<server pid>:<its start time>'. The start time keeps a later server that
    drew the same pid, or any process holding it, from passing for a dead one."""
    uid = tmux.server_uid(strict=True)
    return f"{uid}:{tmux.proc_stat(uid.rpartition(':')[2])[19]}"


def alive(server: str) -> bool:
    boot, pid, start = server.rsplit(":", 2)
    with open("/proc/sys/kernel/random/boot_id") as f:
        current = f.read().strip()
    stat = tmux.proc_stat(pid)
    return boot == current and len(stat) > 19 and stat[19] == start


class Recorder:
    """Writes the snapshot from the watcher's thread, at most once a minute."""

    def __init__(self, history):
        self.history = history
        self._at = 0.0
        self._last: tuple[str, str] | None = None  # (server, payload) last written
        self._bound: dict[str, tuple[str, dict]] = {}  # pane id -> (foreground pgrp, binding)

    def tick(self, states: list[dict], now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now - self._at < EVERY:
            return
        self._at = now
        try:
            server = identity()
            payload = json.dumps(self._snapshot(states), sort_keys=True, separators=(",", ":"))
            with self.history.connect() as db:
                if self._last == (server, payload):
                    db.execute("UPDATE layout_snapshots SET last_seen=? WHERE server=?",
                               (now, server))
                else:
                    db.execute("INSERT INTO layout_snapshots VALUES (?, ?, ?, NULL) ON CONFLICT"
                               "(server) DO UPDATE SET last_seen=excluded.last_seen, "
                               "payload=excluded.payload", (server, now, payload))
                db.execute("DELETE FROM layout_snapshots WHERE last_seen<?", (now - KEEP,))
            self._last = server, payload
        except (OSError, IndexError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            logger.warning("Could not record the workspace snapshot", exc_info=True)

    def _snapshot(self, states: list[dict]) -> dict:
        by_pane = {s.get("pane_id"): s for s in states}
        sessions: dict[str, dict] = {}
        windows: dict[str, dict] = {}
        for name, path, group, wid, index, current, wname, auto, zoomed, layout in tmux.rows(
                "list-windows", _WINDOWS):
            session = sessions.setdefault(name, {"name": name, "path": path, "group": group,
                                                 "windows": []})
            session["windows"].append({"id": wid, "index": int(index), "current": current == "1"})
            windows.setdefault(wid, {"name": wname, "manual": auto != "1",
                                     "zoomed": zoomed == "1", "layout": layout, "panes": []})
        rows = tmux.rows("list-panes", _PANES)
        seen, bound = set(), self._bindings({r[1]: r[4] for r in rows})
        for wid, pane, index, active, _pid, cwd, command in rows:
            if pane in seen:  # a grouped or linked window lists its panes once per session
                continue
            seen.add(pane)
            s = by_pane.get(pane, {})
            windows[wid]["panes"].append({
                "index": int(index), "active": active == "1", "cwd": cwd,
                "command": os.path.basename(command), "binding": bound.get(pane),
                "title": s.get("title") or s.get("agent_title") or s.get("bootstrap_title"),
                "tool": s.get("tool"), "activity": s.get("activity"),
                "waiting_on": s.get("waiting_on"), "cost": s.get("cost"),
                "context_pct": s.get("context_pct"),
                # To the minute: the payload must not change with every keystroke.
                "last_activity": int(s.get("last_activity_at") or 0) // 60 * 60,
            })
        return {"sessions": list(sessions.values()), "windows": windows}

    def _bindings(self, pane_pids: dict[str, str]) -> dict[str, dict]:
        """Pane id -> {harness, session_id} for the one live session in the pane's
        foreground process group. A stopped agent under a foreground one is not the pane's;
        none or several, and the pane stays unbound rather than guessed. When agent-history
        can't tell, a pane whose foreground hasn't changed keeps its last binding."""
        foreground = {}
        for pane, pid in pane_pids.items():
            stat = tmux.proc_stat(pid)
            if len(stat) > 5:
                foreground[pane] = stat[5]  # tpgid: the terminal's foreground group
        live = agent_history.live()
        unknown = set(live["unknown"]) if live else None
        found: dict[str, list[dict]] = {}
        for sid, run in (live or {}).get("sessions", {}).items():
            stat = tmux.proc_stat(run.get("pid", 0))
            pane = next((p for p, g in foreground.items() if stat[2:3] == [g]), None)
            if pane:
                found.setdefault(pane, []).append({"harness": run.get("harness"),
                                                   "session_id": sid})
        out = {}
        for pane, group in foreground.items():
            if len(found.get(pane, ())) == 1:
                out[pane] = found[pane][0]
            elif pane in self._bound and self._bound[pane][0] == group and (
                    unknown is None or self._bound[pane][1]["harness"] in unknown):
                out[pane] = self._bound[pane][1]
        self._bound = {p: (foreground[p], b) for p, b in out.items()}
        return out


def candidate(history) -> dict | None:
    """The newest dead server's snapshot, unless already restored or dismissed. Live
    servers are skipped, so a replacement server can't hide the one that died; consumed
    ones are not, so an older loss never surfaces once the newest has been handled."""
    with history.connect() as db:
        rows = db.execute("SELECT server, last_seen, payload, consumed FROM layout_snapshots "
                          "ORDER BY last_seen DESC").fetchall()
    for server, last_seen, payload, consumed in rows:
        if alive(server):
            continue
        return None if consumed else {"server": server, "died_at": last_seen, **json.loads(payload)}
    return None


def consume(history, server: str, how: str) -> bool:
    """Mark a snapshot restored or dismissed; False if someone else already did."""
    with history.connect() as db:
        return db.execute("UPDATE layout_snapshots SET consumed=? WHERE server=? AND "
                          "consumed IS NULL", (how, server)).rowcount == 1


def offer(history) -> dict | None:
    """The candidate with each pane's default and, when it can't come back, why."""
    found = candidate(history)
    if found is None:
        return None
    agent_history.reconcile()  # every pane is judged by its transcript's latest state
    died = found["died_at"]
    for window in found["windows"].values():
        for pane in window["panes"]:
            pane["default"], pane["blocked"] = _judge(pane, died)
    return found


def _judge(pane: dict, died: float) -> tuple[bool, str | None]:
    if not os.path.isdir(pane["cwd"]):
        return False, "its directory is gone"
    activity, binding = pane.get("activity"), pane.get("binding")
    working = activity in ("running", "compacting") or (
        activity == "waiting" and pane.get("waiting_on") != "external")
    last = pane.get("last_activity") or 0
    if binding:
        entry = agent_history.get(binding["session_id"])
        if entry is None or entry.get("source_missing"):
            return False, "its conversation is gone"
        if entry.get("running_unknown"):
            return False, "can't tell whether it's already running"
        if entry.get("running"):
            return False, "it's already running"
        pane["cwd"] = entry.get("cwd") or pane["cwd"]  # the harness finds it only there
        # The transcript, not the screen: an idle agent's clock moves with anything drawn.
        spoke = _epoch(entry.get("last_active"))
        last = last if spoke is None else spoke
    elif pane.get("tool") in AGENT_TOOLS:
        pane["note"] = "no conversation was recorded for it, so it returns as a shell"
        return False, None
    elif pane.get("command") in _SHELLS:
        return False, None  # a shell with nothing running: there for the asking
    return working or died - last < RECENT, None


def _epoch(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except ValueError:
        return None
