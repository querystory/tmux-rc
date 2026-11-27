"""The guard refuses the AGENTS.md mistakes, and install/check agree with each other."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def guard(event, root):
    env = {**os.environ, "TMUXRC_LIVE_ROOT": str(root)}
    return subprocess.run([sys.executable, SCRIPTS / "agent_guard.py"], input=json.dumps(event),
                          capture_output=True, text=True, env=env).returncode


@pytest.mark.parametrize(("cmd", "code"), [
    ("tmux kill-server", 2),
    ("TMUX_TMPDIR=/tmp/x tmux kill-server", 2),
    ("ls && tmux kill-session -t demo", 2),
    (r"tmux list-sessions \; kill-server", 2),
    ("tmux list-sessions ';' kill-server", 2),
    ("env -u TMUX tmux -L t1 ls; tmux kill-server", 2),
    ("env -u TMUX tmux -L t1 kill-server", 0),
    ("env -u TMUX tmux -L t1 ls & tmux kill-server", 2),
    ("env -u TMUX tmux -L t1 kill-server >/dev/null 2>&1", 0),
    ("env -u TMUX tmux -L t1 kill-server &>/dev/null &", 0),
    ("tmux \\\nkill-server", 2),
    ('tmux "kill-\\\nserver"', 2),
    ("env -u TMUX \\\ntmux -L t1 kill-server", 0),
    ("echo 'tmux \\\nkill-server'", 0),
    ("env -u TMUX tmux -S /tmp/sock kill-session -t a", 0),
    ("tmux kill-pane -t %42", 0),
    ("git status", 0),
])
def test_tmux_kill(tmp_path, cmd, code):
    event = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/"}
    assert guard(event, tmp_path) == code


@pytest.mark.parametrize(("start", "cmd", "code"), [
    ("root", "git checkout some-pr", 2),
    ("root", "git fetch && git reset --hard origin/main", 2),
    ("root", "git fetch && git checkout --detach origin/integration", 0),
    ("root", "git log --oneline -3", 0),
    ("root/openbus", "git reset --hard origin/main", 2),
    ("/", "cd {root} && git reset --hard origin/main", 2),
    ("/", "git -C {root} checkout some-pr", 2),
    ("/", "git -C {root} checkout --detach origin/integration", 0),
    ("root", "git -c advice.detachedHead=false checkout some-pr", 2),
    ("root", "git --no-pager reset --hard origin/main", 2),
    ("/", "git --no-pager -C {root} -c a=b checkout some-pr", 2),
    ("root", "git -c advice.detachedHead=false checkout --detach origin/integration", 0),
    ("root", "cd /tmp/wt && git commit -m x", 0),
    ("root", "git -C /tmp/wt commit -m x", 0),
    ("root", "cd .claude/worktrees/x && git commit -m x", 0),
])
def test_live_root_git(tmp_path, start, cmd, code):
    root = tmp_path / "root"
    cwd = root / start.removeprefix("root").lstrip("/") if start.startswith("root") else start
    event = {"tool_name": "Bash", "tool_input": {"command": cmd.format(root=root)}, "cwd": str(cwd)}
    assert guard(event, root) == code


@pytest.mark.parametrize(("rel", "code"), [
    ("openbus/server.py", 2),
    (".env", 0),
    (".claude/worktrees/x/openbus/server.py", 0),
])
def test_live_root_edits(tmp_path, rel, code):
    event = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / rel)}, "cwd": "/"}
    assert guard(event, tmp_path) == code


def test_edit_elsewhere_passes(tmp_path):
    event = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/elsewhere.py"}, "cwd": "/"}
    assert guard(event, tmp_path / "root") == 0


def test_install_is_idempotent_and_check_sees_it(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    env["HOME"] = str(tmp_path)
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir()
    existing = {"type": "command", "command": "agent-history hook"}  # another host's spelling
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [existing]}]}}))

    def run(cmd):
        return subprocess.run([sys.executable, SCRIPTS / "agent_hooks.py", cmd], env=env,
                              capture_output=True, text=True)

    before = run("check")
    assert before.returncode == 1
    assert "no PreToolUse hook" in before.stdout
    assert "no Stop hook" not in before.stdout  # an existing spelling counts
    run("install")
    run("install")
    hooks = json.loads(settings.read_text())["hooks"]
    assert {e: len(g) for e, g in hooks.items()} == {"Stop": 1, "PreToolUse": 1, "SessionEnd": 1,
                                                     "SubagentStop": 1}
    after = run("check").stdout
    assert "settings.json" not in after
    assert "tmux-rc-agent-guard is not installed" not in after


def test_bash_only_guard_gets_the_edit_tools(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    env["HOME"] = str(tmp_path)
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir()
    guard_hook = {"type": "command", "command": "tmux-rc-agent-guard"}
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash",
                                                              "hooks": [guard_hook]}]}}))

    def run(cmd):
        return subprocess.run([sys.executable, SCRIPTS / "agent_hooks.py", cmd], env=env,
                              capture_output=True, text=True)

    assert "no PreToolUse hook (tmux-rc-agent-guard) for Edit, Write" in run("check").stdout
    run("install")
    run("install")
    groups = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    assert [g["matcher"] for g in groups] == ["Bash", "Edit|Write|MultiEdit|NotebookEdit"]
    assert "PreToolUse" not in run("check").stdout
