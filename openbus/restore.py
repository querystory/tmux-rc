"""Reboot restore (docs/design/reboot-restore.md). Once a minute, a snapshot of each tmux
server's workspace: sessions, windows, pane order, directories, and the conversation each
agent pane holds. When that server dies, its snapshot is offered back.

A snapshot is current state, not history: one row per server, replaced in place, its
payload rewritten only when the layout changes and its last-seen stamp every minute, since
that stamp stands for when the server died. Nothing from a pane's command line or
environment is stored but the account dir each agent ran on, and each session's default
one; a resume command is rebuilt from agent-history at restore time."""
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
from pathlib import Path

from . import accounts, agent_history, plan_usage, tmux
from .expunge import codex_status_line, codex_thread_shown
from .history import AGENT_TOOLS
from .plan_usage import ENV

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

    def tick(self, states: list[dict], now: float | None = None,
             screens: dict | None = None) -> None:
        now = time.time() if now is None else now
        if now - self._at < EVERY:
            return
        self._at = now
        try:
            server = identity()
            payload = json.dumps(self._snapshot(states, screens or {}), sort_keys=True,
                                 separators=(",", ":"))
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

    def _snapshot(self, states: list[dict], screens: dict) -> dict:
        by_pane = {s.get("pane_id"): s for s in states}
        sessions: dict[str, dict] = {}
        windows: dict[str, dict] = {}
        rows = tmux.rows("list-panes", _PANES)
        seen, bound = set(), self._bindings({r[10]: r[13] for r in rows}, screens)
        for (name, path, group, wid, windex, current, wname, auto, zoomed, layout,
             pane, index, active, _pid, cwd, command) in rows:
            if name not in sessions:
                sessions[name] = {"name": name, "path": path, "group": group, "windows": [],
                                  "accounts": _session_accounts(name)}
            session = sessions[name]
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

    def _bindings(self, pane_pids: dict[str, str], screens: dict) -> dict[str, dict]:
        """Pane id -> {harness, session_id} for the one live session in the pane's
        foreground process group. A stopped agent under a foreground one is not the pane's;
        none or several, and the pane stays unbound rather than guessed. When agent-history
        can't tell, a pane whose foreground hasn't changed keeps its last binding.

        A Codex client names on its command line the thread it was resumed with, even
        after /new, and is listed only while the daemon holds that thread; the thread it
        shows now is on its status bar (with `session-id` configured), so where the bar
        names one, that wins, and keeps an unchanged foreground bound."""
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
                harness = run.get("harness")  # an account per agent that has them
                found.setdefault(pane, []).append({"harness": harness, "session_id": sid, **(
                    {"account": _account(run)} if harness in ENV else {})})
        out = {}
        for pane, group in foreground.items():
            last = self._bound[pane][1] if self._bound.get(pane, ("",))[0] == group else None
            if len(found.get(pane, ())) == 1:
                out[pane] = found[pane][0]
            elif last and (unknown is None or last["harness"] in unknown):
                out[pane] = last
            binding = out.get(pane, last)
            if binding and (shown := _shown_thread(binding, screens.get(pane), group)):
                out[pane] = {**binding, "session_id": shown}
        self._bound = {p: (foreground[p], b) for p, b in out.items()}
        return out


def _shown_thread(binding: dict, hist: list[dict] | None, group: str) -> str | None:
    """The thread a Codex pane's latest status bar shows, read as the account it runs on
    configures that bar, or None. Only a capture taken since the foreground process
    started counts: an older one (a failed capture keeps it) may be a previous client's."""
    account = binding.get("account") if binding["harness"] == "codex" else None
    stat = tmux.proc_stat(group)
    if not (account and hist and len(stat) > 19 and hist[-1]["ts"] >= tmux.started_at(stat[19])):
        return None
    return codex_thread_shown(codex_status_line(Path(account)), hist[-1]["text"] or "")


def _account(run: dict) -> str | None:
    """The config dir the agent runs on, from its own environment: a session copied to
    another account (Move) keeps its id in both, and only this says which one it is. None
    when unreadable: restore then refuses it rather than guess. A relative dir is the
    agent's, so it is resolved against the agent's cwd, never the daemon's."""
    pid = str(run.get("pid", 0))
    if not (env := plan_usage.pane_env(pid, run["harness"])):
        return None
    config = plan_usage.home(run["harness"], env)
    try:
        return str(config if config.is_absolute() else Path(plan_usage.canonical(
            os.path.join(os.readlink(f"/proc/{pid}/cwd"), config))))
    except OSError:
        return None


def _session_accounts(session: str) -> dict[str, str | None]:
    """The session's own account default per provider (`tmux set-environment`, as the
    account picker sets it): a dir, or None where it is marked removed; unset is absent."""
    env = tmux.show_env(session)
    return {t: plan_usage.canonical(v) if v else None for t, var in ENV.items()
            if (v := env.get(var)) is not None}


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
        # An index naming a deleted copy is no loss when the recorded account holds one:
        # _elsewhere judges that.
        if entry is None or (entry.get("source_missing") and "account" not in binding):
            return False, "its conversation is gone"
        if entry.get("running_unknown"):
            return False, "can't tell whether it's already running"
        if entry.get("running"):
            return False, "it's already running"
        if why := _elsewhere(pane, binding):
            return False, why
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
    # One resuming on another account (_elsewhere) comes back only if picked.
    return (working or died - last < RECENT) and "resume_from" not in pane, None


def _elsewhere(pane: dict, binding: dict) -> str | None:
    """Why the pane can't resume on the account it ran on, or None. Its dir must still hold
    the conversation: resuming the id elsewhere would silently pick up another account's
    copy, likely older. When exactly one other account has it, the pane is offered
    unticked, resuming there only if picked (`resume_from`); never the default dir unasked."""
    if "account" not in binding:  # recorded before accounts were, or a harness without them
        return None
    account, harness, sid = binding["account"], binding["harness"], binding["session_id"]
    if account is None:
        return "can't tell which account it ran on"
    if accounts.holds(harness, sid, account):
        return None
    where = f"its account dir {accounts.tilde(account)} no longer has this conversation"
    others = [p for p in accounts.holders(harness, sid) if p["dir"] != account]
    if len(others) != 1:
        return where + ("; several other accounts do" if others else "")
    pane["resume_from"] = others[0]["dir"]
    pane["note"] = f"{where}; tick it to resume from {others[0]['name']} ({others[0]['tilde']})"
    return None


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
            _defaults(session, name)
        else:
            fresh = next((w for w in links if w["id"] not in built), None)
            first = fresh and _picked(windows[fresh["id"]], fresh["id"], chosen)[0]["cwd"]
            seed = tmux.new_session(name, first or session["path"], size=_size(windows, session))
            if _defaults(session, name):  # the seed's shell started before them: again, after
                tmux.run("respawn-pane", "-k", "-t", seed, "-c", first or session["path"])
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


def _defaults(session: dict, name: str) -> bool:
    """Set the session's recorded account defaults on its rebuilt namesake, before its
    other shells start, so they and its new windows open where they did. Whether any."""
    for tool, config in session.get("accounts", {}).items():
        tmux.set_env(ENV[tool], config, session=name)
    return bool(session.get("accounts"))


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
        binding = entry["binding"]
        sid, account = binding["session_id"], entry.get("resume_from") or binding.get("account")
        async with slots:
            found = await asyncio.to_thread(agent_history.get, sid) or {}
            # agent-history drops the argv when its indexed copy is gone (source_missing);
            # the recorded account's copy then resumes the harness's own way.
            argv = found.get("resume_argv") or (
                [*accounts.RESUME[binding["harness"]], sid] if found and account else [])
            if found.get("running") or found.get("running_unknown") or not argv or \
                    argv[0] not in agent_history.RESUMABLE:
                audit("restore_resume", pane, sid, "skipped: running, unknown or not resumable")
                return
            if "account" in binding and not (account and await asyncio.to_thread(
                    accounts.holds, binding["harness"], sid, account)):
                audit("restore_resume", pane, sid, "skipped: its account dir lacks it")
                return
            line = shlex.join(accounts.resume_command({**found, "resume_argv": argv}, account))
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
