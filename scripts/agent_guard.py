#!/usr/bin/env python3
"""Claude Code PreToolUse hook: refuse the AGENTS.md mistakes that cannot be undone.

Installed (as ~/.local/bin/tmux-rc-agent-guard) by `make install-agent-hooks`; `make
doctor` reports a host without it. Exit 2 blocks the call and shows stderr to the agent.

- `tmux kill-server` / `kill-session` outside an isolated test server: agents inherit
  $TMUX, so the bare form (TMUX_TMPDIR or not) kills the user's server and every agent
  in it. `env -u TMUX tmux -L|-S ...` is the isolated form and passes.
- Edits and branch moves in the live checkout (the daemon unit's WorkingDirectory): it
  runs detached at origin/integration, and anything else there silently drops the PRs
  under test. The rebuild's own `git checkout --detach origin/integration`, `.env` and
  worktrees under `.claude/worktrees` stay allowed.

It reads only the command text, so it catches mistakes, not an agent working around it;
the rules in AGENTS.md hold either way.
"""
import json
import os
import re
import sys
from pathlib import Path

UNIT = Path.home() / ".config/systemd/user/tmux-rc.service"
KILL = re.compile(r"\btmux\b.*\bkill-(server|session)\b")
ISOLATED = re.compile(r"\benv\s+-u\s+TMUX\b.*\btmux\s+-[LS]\s*\S")
GIT_MOVE = re.compile(
    r"\bgit\s+(checkout|switch|reset|restore|pull|merge|rebase|commit|cherry-pick|stash)\b")
REBUILD = re.compile(r"\bgit\s+checkout\s+--detach\s+origin/integration$")
# One shell command between unquoted, unescaped separators (&& || ; | newline): tmux runs
# `tmux ls \; kill-server` as one invocation, so its escaped `;` must not split it.
PIECE = re.compile(r"""(?:\\.|'[^']*'|"(?:\\.|[^"\\])*"|[^\\'";&|\n]|(?<!&)&(?!&)|['"\\])+""",
                   re.DOTALL)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def live_root() -> Path | None:
    """The checkout the daemon runs from, or None on a host without the unit."""
    if env := os.environ.get("TMUXRC_LIVE_ROOT"):
        return Path(env).resolve()
    try:
        m = re.search(r"^WorkingDirectory=(.+)$", UNIT.read_text(), re.MULTILINE)
    except OSError:
        return None
    return Path(m.group(1).strip()).resolve() if m else None


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def verdict(event: dict, root: Path | None) -> str | None:
    """Why this tool call is refused, or None to let it run."""
    tool, args = event.get("tool_name"), event.get("tool_input") or {}
    cwd = Path(event.get("cwd") or ".").resolve()
    if tool == "Bash":
        for part in PIECE.findall(args.get("command", "")):
            if KILL.search(part) and not ISOLATED.search(part):
                return ("tmux kill-server/kill-session would hit the user's live tmux server "
                        "($TMUX is inherited). Close only the window or pane id you created, or "
                        "isolate a test server with `env -u TMUX tmux -L <socket>` (AGENTS.md).")
            moves = GIT_MOVE.search(part) and not REBUILD.search(part.strip())
            if root and cwd == root and moves:
                return (f"{root} is the live deploy, detached at origin/integration; moving it "
                        "drops every PR under test. Work in a worktree cut from origin/main "
                        "(AGENTS.md).")
    path = args.get("file_path") or args.get("notebook_path")
    if tool in EDIT_TOOLS and root and path:
        p = (cwd / path).resolve()
        allowed = p == root / ".env" or inside(p, root / ".claude/worktrees")
        if inside(p, root) and not allowed:
            return (f"{root} is the live deploy; never edit files there. Make the change in a "
                    "worktree cut from origin/main (AGENTS.md).")
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0  # not ours to judge; never wedge the agent on a malformed event
    reason = verdict(event, live_root())
    if reason:
        sys.stderr.write(f"Blocked by tmux-rc-agent-guard: {reason}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
