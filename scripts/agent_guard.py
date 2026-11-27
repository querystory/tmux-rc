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
# `git` and its global options (`-c k=v`, `--no-pager`, ...); group 1: the checkout `-C` names
GIT_C = (r"\bgit\s+(?:(?:-C\s+(\S+)|-c\s+\S+"
         r"|--(?:git-dir|work-tree|namespace|config-env)\s+\S+|-\S+)\s+)*")
GIT_MOVE = re.compile(
    GIT_C + r"(checkout|switch|reset|restore|pull|merge|rebase|commit|cherry-pick|stash)\b")
REBUILD = re.compile(GIT_C + r"checkout\s+--detach\s+origin/integration$")
CD = re.compile(r"^\s*(?:cd|pushd)(?:\s+(\S+))?\s*$")
# One shell command between unquoted, unescaped separators (&& || ; | & newline): tmux runs
# `tmux ls \; kill-server` as one invocation, so its escaped `;` must not split it. The `&`
# of a redirection (`2>&1`, `&>`) stays inside; a background `&` ends the command.
PIECE = re.compile(r"""(?:\\.|'[^']*'|"(?:\\.|[^"\\])*"|[^\\'";&|\n]|(?<=[<>])&|&(?=>)|['"\\])+""",
                   re.DOTALL)
QUOTED_OR_ESC = re.compile(r"""'[^']*'|"(?:\\.|[^"\\])*"|\\.""", re.DOTALL)
ESC = re.compile(r"\\.", re.DOTALL)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def live_root() -> Path | None:
    """The checkout the daemon runs from, or None on a host without the unit."""
    if env := os.environ.get("TMUXRC_LIVE_ROOT"):
        return Path(env).resolve()
    try:
        m = re.search(r"^WorkingDirectory=(.+)$", UNIT.read_text(), re.MULTILINE)
    except OSError:
        return None
    if not m:
        return None
    # systemd specifiers: `%h` is the home directory (the shipped unit uses it), `%%` a `%`
    spec = re.sub(r"%[%h]", lambda s: "%" if s[0] == "%%" else str(Path.home()), m[1].strip())
    return Path(spec).resolve()


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def joined(command: str) -> str:
    """The command with its line continuations removed, as the shell runs it: a
    backslash-newline joins lines everywhere but inside single quotes."""
    def drop(m: re.Match) -> str:
        return "" if m[0] == "\\\n" else m[0]
    return QUOTED_OR_ESC.sub(
        lambda m: m[0] if m[0].startswith("'") else ESC.sub(drop, m[0]), command)


def verdict(event: dict, root: Path | None) -> str | None:
    """Why this tool call is refused, or None to let it run."""
    tool, args = event.get("tool_name"), event.get("tool_input") or {}
    cwd = Path(event.get("cwd") or ".").resolve()
    if tool == "Bash":
        here = cwd  # where each piece runs: an earlier `cd`/`pushd` in the command moves it
        for part in PIECE.findall(joined(args.get("command", ""))):
            if cd := CD.match(part):
                here = here / os.path.expanduser((cd.group(1) or "~").strip("'\""))
                continue
            if KILL.search(part) and not ISOLATED.search(part):
                return ("tmux kill-server/kill-session would hit the user's live tmux server "
                        "($TMUX is inherited). Close only the window or pane id you created, or "
                        "isolate a test server with `env -u TMUX tmux -L <socket>` (AGENTS.md).")
            move = GIT_MOVE.search(part)
            if not (root and move) or REBUILD.search(part.strip()):
                continue
            repo = (here / (move.group(1) or ".").strip("'\"")).resolve()
            if inside(repo, root) and not inside(repo, root / ".claude/worktrees"):
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
