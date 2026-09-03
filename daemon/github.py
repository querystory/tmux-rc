"""Fetch the user's review-requested PRs via the `gh` CLI, for the phone's "Needs you"
attention focus (the Orchestrator "Review requested" group).

Read-only and best-effort: every failure path — `gh` missing, not authenticated, a
network error, a non-zero exit, unparseable JSON — yields an empty list and NEVER raises to
the caller. It shells out to the already-installed `gh` so it reuses the host's GitHub auth;
no token is stored or read here. The watcher calls fetch_review_requests() from a thread on a
slow cadence (see watcher._reviews_loop); nothing here touches the event loop.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

# gh --json field set we ask for. Kept minimal — exactly what a PR row renders.
_FIELDS = "number,title,url,repository,author,updatedAt"
_LIMIT = "30"  # a review queue longer than this is its own problem; cap the payload
_TIMEOUT = 15  # gh + one network round-trip; short enough not to wedge the reviews loop


def _off_flag() -> bool:
    """TMUXRC_GH_REVIEWS explicitly set to a falsey value forces the feature off even where
    `gh` is present. Unset ⇒ not forced off (availability then decides)."""
    v = os.environ.get("TMUXRC_GH_REVIEWS")
    return v is not None and v.strip().lower() in ("0", "false", "no", "off", "")


def enabled() -> bool:
    """Poll for reviews only when it can actually work AND the user has not opted out.
    Gated on the `gh` binary existing, so a host without it never shells out; a user with
    gh who does not want PR polling sets TMUXRC_GH_REVIEWS=0."""
    if _off_flag():
        return False
    return shutil.which("gh") is not None


def _search_args() -> list[str]:
    """The search qualifiers. Default: open PRs where you are an explicitly requested
    reviewer. TMUXRC_GH_REVIEW_QUERY replaces them wholesale with a raw gh search string
    (e.g. "review-requested:@me org:querystory") for a narrower/broader set."""
    override = (os.environ.get("TMUXRC_GH_REVIEW_QUERY") or "").strip()
    if override:
        return override.split()
    return ["--review-requested=@me", "--state=open"]


def _repo_name(repo: object) -> str:
    # gh has shipped `repository` as both an object and a bare string across versions.
    if isinstance(repo, dict):
        return repo.get("nameWithOwner") or repo.get("name") or ""
    return repo if isinstance(repo, str) else ""


def _author_login(author: object) -> str:
    if isinstance(author, dict):
        return author.get("login") or ""
    return author if isinstance(author, str) else ""


def _normalize(pr: dict) -> dict | None:
    """One gh search result -> the compact PR row the phone renders. Returns None for a row
    missing the essentials (url + number), so a malformed entry is dropped, not rendered."""
    url = pr.get("url")
    number = pr.get("number")
    if not url or number is None:
        return None
    repo = _repo_name(pr.get("repository"))
    return {
        # Stable key for the client's keyed list; also human-meaningful (owner/repo#123).
        "id": f"{repo}#{number}",
        "url": url,
        "repo": repo,
        "number": number,
        "title": pr.get("title") or "",
        "author": _author_login(pr.get("author")),
        "updated_at": pr.get("updatedAt") or "",
    }


def fetch_review_requests() -> list[dict]:
    """Return normalized PR dicts for the phone, newest-updated first, or [] on ANY failure.
    Blocking (subprocess) — the watcher calls it via asyncio.to_thread."""
    if not enabled():
        return []
    cmd = ["gh", "search", "prs", *_search_args(), "--json", _FIELDS, "--limit", _LIMIT]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT, check=True
        ).stdout
    except subprocess.CalledProcessError as e:
        # Most commonly "not logged into any GitHub hosts" — info, not warning: it is a
        # config state (no auth), not a daemon fault, and it would otherwise log every cycle.
        logger.info("gh review fetch failed (exit %s): %s", e.returncode, (e.stderr or "").strip()[:200])
        return []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.info("gh review fetch error: %s", e)
        return []
    try:
        raw = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        logger.info("gh review fetch: unparseable JSON")
        return []
    if not isinstance(raw, list):
        return []
    prs = [n for n in (_normalize(pr) for pr in raw if isinstance(pr, dict)) if n]
    prs.sort(key=lambda p: p["updated_at"], reverse=True)
    return prs
