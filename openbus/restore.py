"""Reboot restore (docs/design/reboot-restore.md). Once a minute, a snapshot of each tmux
server's workspace: sessions, windows, pane order, directories, and the conversation each
agent pane holds. When that server dies, its snapshot is offered back.

A snapshot is current state, not history: one row per server, replaced in place, its
payload rewritten only when the layout changes and its last-seen stamp every minute, since
that stamp stands for when the server died. Nothing from a pane's command line or
environment is stored; a resume command is rebuilt from agent-history at restore time."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import time
from datetime import datetime

from . import accounts, agent_history, tmux
from .history import AGENT_TOOLS

logger = logging.getLogger(__name__)
EVERY = 60
KEEP = 30 * 86400  # a dead server's snapshot is offered this long, then dropped
RECENT = 86400  # pane defaults: active this close to the server's death is recent
_SHELLS = frozenset({"bash", "zsh", "fish", "sh"})
PACE = 3  # agents starting at once: forty Node processes on a just-booted box fight
READY = 60  # seconds an agent gets to reach its prompt before its slot passes on

# One list-panes call for sessions, windows and panes alike: separate calls could see a
# window made or split between them, and a layout that disagrees with its pane list.
_PANES = ("#{session_name}\t#{session_path}\t#{session_group}\t#{window_id}\t#{window_index}"
          "\t#{window_active}\t#{window_name}\t#{automatic-rename}\t#{window_zoomed_flag}"
          "\t#{window_layout}\t#{pane_id}\t#{pane_index}\t#{pane_active}\t#{pane_pid}"
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
            if identity() != server:  # a replacement's workspace, not the dead one's
                return
            with self.history.connect() as db:
                # The stamp alone, unless the layout changed or the row is gone.
                if self._last != (server, payload) or db.execute(
                        "UPDATE layout_snapshots SET last_seen=? WHERE server=?",
                        (now, server)).rowcount == 0:
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
        rows = tmux.rows("list-panes", _PANES)
        seen, bound = set(), self._bindings({r[10]: r[13] for r in rows})
        for (name, path, group, wid, windex, current, wname, auto, zoomed, layout,
             pane, index, active, _pid, cwd, command) in rows:
            session = sessions.setdefault(name, {"name": name, "path": path, "group": group,
                                                 "windows": []})
            if not any(w["id"] == wid for w in session["windows"]):
                session["windows"].append({"id": wid, "index": int(windex),
                                           "current": current == "1"})
            windows.setdefault(wid, {"name": wname, "manual": auto != "1",
                                     "zoomed": zoomed == "1", "layout": layout, "panes": []})
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


def consume(history, server: str, how: str | None) -> bool:
    """Mark a snapshot restored or dismissed, False if someone else already did; None
    puts it back on offer (a restore that failed before building anything)."""
    with history.connect() as db:
        return db.execute("UPDATE layout_snapshots SET consumed=? WHERE server=? AND "
                          "(consumed IS NULL) = ?", (how, server, how is not None)).rowcount == 1


def offer(history) -> dict | None:
    """The candidate with each pane's default and, when it can't come back, why."""
    found = candidate(history)
    if found is None:
        return None
    agent_history.reconcile()  # every pane is judged by its transcript's latest state
    panes = [p for w in found["windows"].values() for p in w["panes"]]
    # One lookup for them all: each runs agent-history's liveness scan.
    entries = agent_history.get_many([p["binding"]["session_id"] for p in panes
                                      if p.get("binding")])
    for pane in panes:
        pane["default"], pane["blocked"] = _judge(pane, found["died_at"], entries)
    return found


def _judge(pane: dict, died: float, entries: dict | None) -> tuple[bool, str | None]:
    if not os.path.isdir(pane["cwd"]):
        return False, "its directory is gone"
    activity, binding = pane.get("activity"), pane.get("binding")
    working = activity in ("running", "compacting") or (
        activity == "waiting" and pane.get("waiting_on") != "external")
    last = pane.get("last_activity") or 0
    if binding:
        # launch() resumes from agent-history too: without it the pane would come back a
        # shell and the snapshot be spent, so say what to fix rather than promise a resume.
        if entries is None:
            return False, "agent-history isn't answering; rebuild it with make install-units"
        entry = entries.get(binding["session_id"])
        if entry is None or entry.get("source_missing"):
            return False, "its conversation is gone"
        if entry.get("running_unknown"):
            return False, "can't tell whether it's already running"
        if entry.get("running"):
            return False, "it's already running"
        pane["cwd"] = entry.get("cwd") or pane["cwd"]  # the harness finds it only there
        if not os.path.isdir(pane["cwd"]):
            return False, "its directory is gone"
        # The transcript, not the screen: an idle agent's clock moves with anything drawn.
        spoke = _epoch(entry.get("last_active"))
        last = pane["last_activity"] = last if spoke is None else spoke  # the card shows it
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


def build(snapshot: dict, chosen: set[tuple[str, int]]) -> list[tuple[str, dict]]:
    """Rebuild the chosen panes' sessions, windows and splits as a person would, starting
    the tmux server if none runs (new_session puts it in its own scope with the login
    shell's environment). Every pane starts as the user's shell in its directory; agents
    are resumed afterwards, paced (launch). Returns each built pane with its snapshot
    entry. A session name already taken gets a suffix, never a merge into a live session;
    a window shared by a group or a link is built once and shared again."""
    taken = {r[0] for r in tmux.rows("list-windows", "#{session_name}")} \
        if tmux.server_running() else set()
    windows, built, groups, out = snapshot["windows"], {}, {}, []
    for session in snapshot["sessions"]:
        links = sorted((w for w in session["windows"] if _picked(windows[w["id"]], w["id"],
                                                                  chosen)),
                       key=lambda w: w["index"])
        if not links:
            continue
        name = _free(session["name"], taken)
        if session["group"] in groups:  # a group member shares the windows already built
            tmux.run("new-session", "-d", "-s", name, "-t", groups[session["group"]])
        else:
            fresh = next((w for w in links if w["id"] not in built), None)
            first = fresh and _picked(windows[fresh["id"]], fresh["id"], chosen)[0]["cwd"]
            seed = tmux.new_session(name, first or session["path"], size=_size(windows, session))
            for link in links:
                target = f"{name}:{link['index']}"
                if link["id"] in built:
                    tmux.run("link-window", "-d", "-s", built[link["id"]], "-t", target)
                    continue
                if link is fresh:
                    if tmux.run("display-message", "-p", "-t", seed, "#{window_index}") \
                            != str(link["index"]):
                        tmux.run("move-window", "-s", seed, "-t", target)
                    out += _window(windows[link["id"]], link["id"], seed, chosen)
                else:
                    cwd = _picked(windows[link["id"]], link["id"], chosen)[0]["cwd"]
                    head = tmux.run("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", target,
                                    "-c", cwd)
                    out += _window(windows[link["id"]], link["id"], head, chosen)
                built[link["id"]] = target
            if not fresh:  # only shared windows here: the seed window was a placeholder
                tmux.run("kill-pane", "-t", seed)
        groups.setdefault(session["group"] or None, name)
        taken.add(name)
        current = next((w for w in links if w["current"]), links[0])
        tmux.run("select-window", "-t", f"{name}:{current['index']}")
    return out


def _size(windows: dict, session: dict) -> tuple[int, int] | None:
    """The session's size as its current window recorded it (`<csum>,<W>x<H>,...`): with no
    client attached, tmux would build it 80x24. None, tmux's default, for a layout without one."""
    current = next((w for w in session["windows"] if w["current"]), session["windows"][0])
    found = re.match(r"[0-9a-f]+,(\d+)x(\d+),", windows[current["id"]].get("layout") or "")
    return (int(found[1]), int(found[2])) if found else None


def _window(window: dict, wid: str, head: str, chosen) -> list[tuple[str, dict]]:
    """Split the window's chosen panes off its first, then its geometry, name and focus."""
    panes = _picked(window, wid, chosen)
    ids = [head]
    for pane in panes[1:]:
        ids.append(tmux.run("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", ids[-1],
                            "-c", pane["cwd"]))
    try:  # the saved string names every pane; with some left out tmux refuses it
        tmux.run("select-layout", "-t", head, window["layout"] if len(panes) == len(
            window["panes"]) else "tiled")
    except subprocess.CalledProcessError:
        tmux.run("select-layout", "-t", head, "tiled")
    if window["manual"]:  # else tmux renames it after the agent the moment it starts
        tmux.run("set-option", "-w", "-t", head, "automatic-rename", "off")
        tmux.run("rename-window", "-t", head, window["name"])
    active = next((i for i, p in zip(ids, panes, strict=True) if p["active"]), ids[0])
    tmux.run("select-pane", "-t", active)
    if window["zoomed"] and len(ids) > 1:
        tmux.run("resize-pane", "-Z", "-t", active)
    return list(zip(ids, panes, strict=True))


def _picked(window: dict, wid: str, chosen) -> list[dict]:
    return [p for p in window["panes"] if (wid, p["index"]) in chosen]


def _free(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    n = 1
    while f"{name}-restored{n if n > 1 else ''}" in taken:
        n += 1
    return f"{name}-restored{n if n > 1 else ''}"


async def launch(built: list[tuple[str, dict]], watcher, audit) -> None:
    """Resume each bound agent in its prebuilt pane, PACE at a time: waiting on the user
    first, then working, then by recency. Each resume is typed into the pane's shell, so
    the login shell's PATH finds the agent and an agent that exits leaves a shell in its
    place. Right before each, the running check runs again: a session resumed by hand
    meanwhile is skipped, never started twice."""
    def rank(item):
        p = item[1]
        return (p.get("activity") != "waiting", p.get("activity") not in ("running", "compacting"),
                -(p.get("last_activity") or 0))

    slots = asyncio.Semaphore(PACE)

    async def one(pane: str, entry: dict) -> None:
        sid = entry["binding"]["session_id"]
        async with slots:
            found = await asyncio.to_thread(agent_history.get, sid) or {}
            argv = found.get("resume_argv") or []
            if found.get("running") or found.get("running_unknown") or not argv or \
                    argv[0] not in agent_history.RESUMABLE:
                audit("restore_resume", pane, sid, "skipped: running, unknown or not resumable")
                return
            line = shlex.join(accounts.resume_command(found))
            await asyncio.to_thread(tmux.run, "send-keys", "-t", pane, "-l", line)
            await asyncio.to_thread(tmux.run, "send-keys", "-t", pane, "Enter")
            audit("restore_resume", pane, sid, "ok")
            for _ in range(READY):  # its slot passes on at its prompt, or after READY
                state = next((s for s in watcher.states if s.get("pane_id") == pane), {})
                if state.get("activity") in ("idle", "waiting") and \
                        state.get("tool") in AGENT_TOOLS:
                    return
                watcher.request_reparse(pane)
                await asyncio.sleep(1)

    agents = sorted(((p, e) for p, e in built if e.get("binding")), key=rank)
    await asyncio.gather(*(one(p, e) for p, e in agents))
