"""Teleport: continue a Claude Code cloud session (claude.ai/code) in a local tmux window.

`claude --teleport <id>` does the work: it brings the conversation down and checks out the
session's branch. What it leaves to the caller is what this module answers before a window
opens: which local checkout to run in (it refuses any repository but the session's first),
and what the session is called (the title does not come down with the conversation).
Both are read from the same sessions API the CLI uses, with the account's own token.
See docs/design/teleport.md.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from pathlib import Path

from . import plan_usage

API = "https://api.anthropic.com/v1/code/sessions/"
_ID = re.compile(r"\b(?:session|cse)_[A-Za-z0-9]+")
_BRANCH = re.compile(r"[\w.][\w./-]*")  # what the fetch below may be handed as a refspec
# Fetch first: the cloud session pushed its branch after this checkout last fetched, and
# the CLI only tries `git checkout` (local, then origin/<branch>). A failed fetch is not
# fatal: the session then resumes on the worktree's own branch and says so.
_FETCH = 'git fetch -q origin "$0"; exec "$@"'


def session_id(text: str) -> str | None:
    """The session id in a pasted claude.ai/code link, or a bare id; None for neither."""
    found = _ID.search(text)
    return found and found[0]


def fetch(config: Path, sid: str, now: float) -> dict:
    """GET the session with this config dir's OAuth token (plan_usage.token). A status is
    raised as urllib's HTTPError: 404 is a session this account cannot see."""
    request = urllib.request.Request(API + sid, headers={
        "Authorization": f"Bearer {plan_usage.token(config, now)}",
        "anthropic-version": "2023-06-01"})
    with plan_usage.OPENER.open(request, timeout=15) as response:
        return json.load(response)


def describe(data: dict) -> dict:
    """What a launch needs from the API's session: its title, the repository teleport
    insists on (the first one the session works in), that repository's branch, and the
    other repositories, which teleport leaves behind. The web app's reply wraps the session
    and calls its settings `config`; the CLI's are bare under `session_context`."""
    data = data.get("response_shape") or data
    context = data.get("config") or data.get("session_context") or {}
    repos = [g for o in context.get("outcomes") or []
             if o.get("type") == "git_repository" and (g := o.get("git_info") or {}).get("repo")]
    first = repos[0] if repos else {}
    branch = next(iter(first.get("branches") or []), "")
    return {"title": " ".join(str(data.get("title") or "").split()),
            "repo": first.get("repo", ""),
            "branch": branch if _BRANCH.fullmatch(branch) else "",
            "also": [g["repo"] for g in repos[1:]]}


def _git(cwd: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                              timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def checkout(repo: str, dirs: list[str]) -> str | None:
    """The main checkout of `repo` ("owner/name") among the repositories `dirs` sit in:
    where origin points at it, whatever the URL's scheme. A worktree answers with its main
    checkout, which is where `claude --worktree` makes the new one."""
    seen = set()
    for d in dirs:
        common = _git(d, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if not common.endswith("/.git") or common in seen:
            continue  # not a repository, a bare one, or one already asked
        seen.add(common)
        origin = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$",
                           _git(d, "config", "--get", "remote.origin.url"))
        if origin and origin[1].lower() == repo.lower():
            return common[:-len("/.git")]
    return None


def command(sid: str, info: dict) -> list[str]:
    """The argv that teleports `sid`: in a fresh worktree when the session has a
    repository, so the branch checkout never touches a checkout someone is working in,
    and named as it was on claude.ai."""
    argv = ["claude", "--teleport", sid]
    if info["title"]:
        argv += ["--name", info["title"]]
    if not info["repo"]:
        return argv
    slug = re.sub(r"[^a-z0-9]+", "-", info["title"].lower()).strip("-")[:40]
    argv += ["--worktree", f"{slug or 'teleport'}-{sid[-6:].lower()}"]
    return ["sh", "-c", _FETCH, info["branch"], *argv] if info["branch"] else argv
