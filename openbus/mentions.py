"""Start an agent where a Markdown file addresses one: a line `@claude <request>` in the
folder TMUXRC_MENTIONS_DIR names opens a window with that agent started on the request,
and from there it is a pane like any other. The why: docs/design/mention-trigger.md."""
from __future__ import annotations

import asyncio
import logging
import os
import stat
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path

from . import live, telemetry, tmux
from .scratch import _load, _save

logger = logging.getLogger(__name__)
AGENTS = ("claude", "codex", "gemini", "omp")  # each takes an opening prompt as its last argument
POLL = 5    # seconds between looks at the folder
QUIET = 30  # a file rests this long first: a note saved mid-sentence is not yet a request
OPEN = 5    # windows started here that may be open at once: all an agent's own mentions can cost
MARK = "@tmuxrc_mention"  # the tmux window option on each window started here
PROMPT = ("{file} addresses you:\n\n{request}\n\nWrite your answer in that file, directly "
          "beneath that line, and commit it. Name any agent without the @.")


def mentions(text: str) -> list[str]:
    """Each `agent request` a line addresses: `@agent request` from the line's first column,
    so never quoted or indented, and not inside a code fence: one opened by a run of ` or
    ~, however indented, until a line that is only as long a run of the same."""
    out, fence = [], ""
    for line in text.splitlines():
        bare = line.lstrip()
        if fence:
            if bare.startswith(fence) and not bare.strip(fence[0] + " \t"):
                fence = ""  # closed by the run alone: with a word after it, it is content
        elif bare.startswith(("```", "~~~")):
            run = bare[0] * (len(bare) - len(bare.lstrip(bare[0])))
            fence = "" if "`" in run and "`" in bare[len(run):] else run  # ```a code span```
        elif line.startswith("@"):
            agent, _, request = line[1:].partition(" ")
            agent = agent.lower().rstrip(",:")
            if agent in AGENTS and request.strip():
                out.append(f"{agent} {request.strip()}")
    return out


def windows() -> int:
    """How many windows a mention started are open: the ones poll marked, whatever they
    are named now. Counted by id: tmux lists a window once for each session that shares it."""
    listed = tmux.run("list-windows", "-a", "-F", f"#{{{MARK}}}#{{window_id}}") \
        if tmux.server_running() else ""
    return len({w for w in listed.splitlines() if w.startswith("1")})


class Mentions:
    """Owns what each file was last read to hold, and the store of what was started."""

    def __init__(self, root: str, store: Path, launchers: Callable[[], list[dict]]):
        self.root, self.store, self.launchers = Path(os.path.realpath(root)), store, launchers
        self.read: dict[Path, tuple[int, list[str]]] = {}

    def found(self) -> list[tuple[float, Path, str]]:
        """(changed, file, mention) for every mention in the folder's Markdown, the file
        changed longest ago first. Nothing outside the folder is read: os.walk enters no
        linked folder, and a linked file is skipped."""
        out = []
        for folder, dirs, names in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]  # .git and its like
            for path in (Path(folder, n) for n in names if n.endswith(".md")):
                try:
                    at = path.lstat()
                    if not stat.S_ISREG(at.st_mode):
                        continue
                    if self.read.get(path, (0,))[0] != at.st_mtime_ns:
                        text = path.read_text(encoding="utf-8", errors="replace")
                        self.read[path] = at.st_mtime_ns, mentions(text)
                except OSError:  # gone since the walk
                    continue
                out += [(at.st_mtime, path, m) for m in self.read[path][1]]
        return sorted(out, key=lambda m: m[0])

    def due(self, now: float, opened: int) -> list[tuple[str, str]]:
        """The (file, mention) to start now, with `opened` such windows open already. The
        store holds, per mention, how many copies of it each file held when they were
        started. More copies in the folder than that are new, and they are the ones in the
        files that gained some, so an old copy's file being edited takes no request from
        a new one's. The total only rises and is saved before anything starts, so a
        mention starts at most once, through restarts, renames and a file that vanishes
        and returns. A folder's first reading starts nothing: all it holds is counted."""
        if not self.root.is_dir():
            return []
        state = _load(self.store, {})
        first = str(self.root) not in state
        started = state.setdefault(str(self.root), {})
        here, changed, due, save = defaultdict(Counter), {}, [], first
        for when, path, mention in self.found():
            file = str(path.relative_to(self.root))
            here[mention][file] += 1
            changed[file] = when
        for mention, at in here.items():
            was = Counter(started.get(mention))
            new = at.total() - was.total()
            if first or not new:
                was = at  # all of it is counted, wherever a renamed file took its copies
            gained = sorted((at - was).elements(), key=changed.get, reverse=True)
            for file in gained[:max(new, 0)]:
                # else still being written, or enough open: it waits, and starts later
                if changed[file] <= now - QUIET and opened + len(due) < OPEN:
                    was[file] += 1
                    due.append((file, mention))
            save |= started.get(mention, {}) != was  # started, or moved with its file
            started[mention] = was
        if save:
            _save(self.store, state)
        return due

    def poll(self) -> list[str]:
        """Start what is due: the panes opened. The request reaches the agent as one
        argument (sh's "$1"), which no shell parses, inside a prompt, so it is no option
        either; the launcher's own command is configuration, and sh reads that."""
        panes = []
        for file, mention in self.due(time.time(), windows()):
            agent, _, request = mention.partition(" ")
            command = next((e["command"] for e in self.launchers() if e["icon"] == agent), agent)
            argv = ["sh", "-c", f'{command} "$1"', "sh", PROMPT.format(file=file, request=request)]
            try:
                pane, _ = live.open_window(f"@{agent} {Path(file).stem}"[:40], argv, str(self.root))
                tmux.run("set-option", "-w", "-t", pane, MARK, "1")
            except Exception as e:  # noqa: BLE001 - the rest of what is due still starts
                telemetry.audit("mention", "-", "mentions", file, outcome=f"error: {e}"[:80])
                continue
            telemetry.audit("mention", pane, "mentions", file)
            panes.append(pane)
        return panes

    async def run(self, watcher) -> None:
        while True:
            try:
                for pane in await asyncio.to_thread(self.poll):
                    watcher.request_reparse(pane)  # wakes a full tick, which finds the new pane
            except Exception:  # one bad look must not end the loop
                logger.warning("mention trigger failed", exc_info=True)
            await asyncio.sleep(POLL)
