"""Ratchet: regex calls in openbus/ may only go down. See AGENTS.md#parser."""

import ast
from pathlib import Path

_RE_FUNCS = {
    "compile", "search", "match", "fullmatch", "sub", "subn", "split", "findall", "finditer",
}

# Lower an entry when you remove a regex; raising one needs a reviewer's explicit sign-off.
BASELINE = {
    "accounts.py": 3,
    "agent_move.py": 1,
    "classify.py": 94,
    "expunge.py": 5,
    "fit.py": 1,
    "gpt_live.py": 1,
    "history.py": 1,
    "host_security.py": 1,
    "live.py": 2,
    "live_providers.py": 2,
    "plan_usage.py": 1,
    "render.py": 2,
    "repository.py": 1,
    "restore.py": 1,
    "scratch.py": 3,
    "scratch_markdown.py": 5,
    "scratch_sidecars.py": 4,
    "server.py": 9,
    "teleport.py": 3,
    "tmux.py": 15,
    "transcript.py": 1,
    "watcher.py": 4,
}


def _count(path: Path) -> int:
    return sum(
        isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        and n.value.id == "re" and n.attr in _RE_FUNCS
        for n in ast.walk(ast.parse(path.read_text()))
    )


def test_regex_count_only_goes_down() -> None:
    files = (Path(__file__).parent.parent / "openbus").glob("*.py")
    counts = {p.name: _count(p) for p in files}
    more = {f: (n, BASELINE.get(f, 0)) for f, n in counts.items() if n > BASELINE.get(f, 0)}
    assert not more, (
        f"regex calls (now, baseline) went up: {more}. "
        "The LLM reads the pane: fix the parser prompt, not code. See AGENTS.md#parser"
    )
    fewer = {f: (counts.get(f, 0), b) for f, b in BASELINE.items() if counts.get(f, 0) < b}
    assert not fewer, f"regex calls (now, baseline) went down: {fewer}. Lower BASELINE."
