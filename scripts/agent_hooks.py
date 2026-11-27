#!/usr/bin/env python3
"""Install, or check, the user-level Claude Code hooks this repo's docs rely on.

The agent-history hooks (Stop, SessionEnd, SubagentStop) keep the session index the
daemon searches fresh; the guard (PreToolUse, scripts/agent_guard.py) enforces the
AGENTS.md rules that cannot be undone. Both live in the user's settings.json, outside
every checkout, so a new machine has neither and nothing fails loudly: an agent just
follows the docs as if they were enforced. `check` is how a host finds out.

  agent_hooks.py install   copy the guard to ~/.local/bin, merge missing hooks into settings
  agent_hooks.py check     report what this host lacks; exit 1 if anything (`make doctor`)

Settings are ~/.claude/settings.json plus $CLAUDE_CONFIG_DIR's, deduplicated through
symlinks. A hook already present under any spelling of its command is left alone.
"""
import filecmp
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BIN = Path.home() / ".local/bin"
GUARD_SRC = Path(__file__).resolve().parent / "agent_guard.py"
GUARD = BIN / "tmux-rc-agent-guard"
HISTORY = BIN / "agent-history"
# event -> (matcher, command, marker that recognises the hook however it was spelled)
GUARD_CMD = "$HOME/.local/bin/tmux-rc-agent-guard"
HISTORY_CMD = "$HOME/.local/bin/agent-history hook"
HOOKS = {
    "PreToolUse":   ("Bash|Edit|Write|MultiEdit|NotebookEdit", GUARD_CMD, "tmux-rc-agent-guard"),
    "Stop":         (None, HISTORY_CMD, "agent-history hook"),
    "SessionEnd":   (None, HISTORY_CMD, "agent-history hook"),
    "SubagentStop": (None, HISTORY_CMD, "agent-history hook"),
}


def settings_files() -> list[Path]:
    dirs = [Path.home() / ".claude"]
    if env := os.environ.get("CLAUDE_CONFIG_DIR"):
        dirs.append(Path(env).expanduser())
    return list(dict.fromkeys((d / "settings.json").resolve() for d in dirs))


def load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def has_hook(settings: dict, event: str, marker: str) -> bool:
    groups = settings.get("hooks", {}).get(event, [])
    return any(marker in h.get("command", "") for g in groups for h in g.get("hooks", []))


def install() -> int:
    BIN.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(GUARD_SRC, GUARD)
    GUARD.chmod(0o755)
    for path in settings_files():
        settings = load(path)
        added = []
        for event, (matcher, command, marker) in HOOKS.items():
            if not has_hook(settings, event, marker):
                group = {"hooks": [{"type": "command", "command": command}]}
                if matcher:
                    group = {"matcher": matcher, **group}
                settings.setdefault("hooks", {}).setdefault(event, []).append(group)
                added.append(event)
        if added:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(settings, indent=2) + "\n")
        print(f"{path}: {'added ' + ', '.join(added) if added else 'already has every hook'}")
    if not HISTORY.exists():
        print("agent-history is not built: `make install-units` builds it (needs Go)")
    return 0


def check() -> int:
    problems = []
    if not GUARD.exists():
        problems.append(f"{GUARD} is not installed")
    elif not filecmp.cmp(GUARD, GUARD_SRC, shallow=False):
        problems.append(f"{GUARD} differs from this checkout's scripts/agent_guard.py")
    if not HISTORY.exists():
        problems.append(f"{HISTORY} is not built (make install-units, needs Go)")
    for path in settings_files():
        settings = load(path)
        problems += [f"{path}: no {event} hook ({marker})"
                     for event, (_, _, marker) in HOOKS.items()
                     if not has_hook(settings, event, marker)]
    is_active = ["systemctl", "--user", "is-active", "--quiet", "tmux-rc.service"]
    if not shutil.which("systemctl") or subprocess.run(is_active, check=False).returncode:
        problems.append("tmux-rc.service is not running (make install-units)")
    for p in problems:
        print(f"MISSING  {p}")
    if problems:
        print("Fix with `make install-agent-hooks` (and `make install-units` for the service).")
        return 1
    print("ok: agent hooks, guard, agent-history and tmux-rc.service are in place")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in {"install", "check"}:
        sys.exit(__doc__)
    sys.exit(install() if cmd == "install" else check())
