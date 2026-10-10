"""Open loops: which pieces of work have stopped moving, and what would unstick each.

The join of three sources: GitHub pull requests (one GraphQL query per REFRESH, cached),
pane state (live from the watcher, including its PR associations) and worktrees (local
git, cached on the same cadence). Requests render from the cache and never touch the
network. Grouping is by stack only for now. See docs/design/open-loops.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from .pr_titles import run_gh
from .repository import github_repository

logger = logging.getLogger(__name__)
REFRESH = 900  # one query however many PRs, so a quarter hour costs GitHub almost nothing
HORIZON = 14 * 86400  # older open PRs and idle worktrees are counted, not listed
STALE = 3 * 86400  # an open PR untouched this long has dropped
IDLE_DIRTY = 86400  # a pane idle this long over uncommitted changes has dropped
RECENT = 86400  # "Moving" looks back this far until daily snapshots make it a diff
MAX_REFS = 50  # pane-associated PRs looked up beyond the searches

FIELDS = """number title url isDraft state updatedAt mergedAt author { login }
baseRefName headRefName mergeable reviewDecision
repository { nameWithOwner defaultBranchRef { name } }
reviewRequests(first: 10) { nodes { requestedReviewer { __typename } } }
latestReviews(first: 10) { nodes { author { __typename login } state submittedAt } }
commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
baseRef { associatedPullRequests(states: MERGED, first: 1) { totalCount } }"""


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(stamp: str | None) -> float:
    return datetime.fromisoformat(stamp).timestamp() if stamp else 0.0


def query(refs: list[tuple[str, int]], now: float) -> str:
    """Your open PRs and review requests touched within HORIZON, what merged within RECENT
    that involved you, and the PRs panes are associated with: one round trip."""
    horizon, recent = _iso(now - HORIZON), _iso(now - RECENT)
    mine, asked = (f"is:pr is:open {who} archived:false" for who in ("author:@me",
                                                                    "review-requested:@me"))
    prs = "nodes { ...P }"
    searches = [("mine", 100, f"{mine} updated:>={horizon}", prs),
                ("older", 0, f"{mine} updated:<{horizon}", "issueCount"),
                ("asked", 50, f"{asked} updated:>={horizon}", prs),
                ("merged", 100, f"is:pr is:merged involves:@me merged:>={recent}", prs)]
    parts = ["viewer { login }",
             *(f'{alias}: search(type: ISSUE, first: {n}, query: "{q}") {{ {body} }}'
               for alias, n, q, body in searches)]
    for i, (repo, number) in enumerate(refs[:MAX_REFS]):
        owner, name = repo.split("/", 1)
        # issueOrPullRequest, not pullRequest: a number that names an issue is just null.
        parts.append(f"r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)})"
                     f" {{ issueOrPullRequest(number: {int(number)}) {{ ...P }} }}")
    return f"fragment P on PullRequest {{ {FIELDS} }} query {{ {' '.join(parts)} }}"


def _pr(node: dict) -> dict:
    commit = ((node["commits"]["nodes"] or [{}])[0]).get("commit") or {}
    default = (node["repository"].get("defaultBranchRef") or {}).get("name")
    stacked = node["baseRefName"] != default
    humans = [r for r in node["latestReviews"]["nodes"]
              if (r.get("author") or {}).get("__typename") == "User"]
    return {
        "repo": node["repository"]["nameWithOwner"], "number": node["number"],
        "title": node["title"], "url": node["url"], "state": node["state"],
        "author": (node.get("author") or {}).get("login"), "draft": node["isDraft"],
        "updated_at": _ts(node["updatedAt"]), "merged_at": _ts(node.get("mergedAt")),
        "base": node["baseRefName"], "head": node["headRefName"], "stacked": stacked,
        "mergeable": node.get("mergeable"), "decision": node.get("reviewDecision"),
        "checks": (commit.get("statusCheckRollup") or {}).get("state"),
        "pushed_at": _ts(commit.get("committedDate")),
        # A bot (a Copilot review, say) is not a reviewer a person will answer for.
        "reviewers": sum(r["requestedReviewer"].get("__typename") in ("User", "Team")
                         for r in node["reviewRequests"]["nodes"] if r["requestedReviewer"]),
        "reviews": [{"by": r["author"]["login"], "state": r["state"],
                     "at": _ts(r["submittedAt"])} for r in humans],
        # Stacked on a branch that merged (or was deleted): it needs a retarget.
        "base_merged": stacked and (node.get("baseRef") is None or
                                    node["baseRef"]["associatedPullRequests"]["totalCount"] > 0),
    }


def fetch_github(refs: list[tuple[str, int]], now: float) -> dict:
    reply = json.loads(run_gh(["api", "graphql", "-f", f"query={query(refs, now)}"], 60) or "{}")
    data = reply.get("data")
    if not data or not data.get("viewer"):
        raise RuntimeError("GitHub unavailable")
    prs = {}
    for alias, value in data.items():
        nodes = (value.get("nodes") if "nodes" in value else [value.get("issueOrPullRequest")]
                 ) if isinstance(value, dict) and alias != "viewer" else []
        for node in nodes:
            if node and "number" in node:  # an issue matches the fragment as {}
                pr = prs.setdefault((node["repository"]["nameWithOwner"].lower(),
                                     node["number"]), _pr(node))
                pr["asked"] = pr.get("asked") or alias == "asked"
    return {"viewer": data["viewer"]["login"], "older": data["older"]["issueCount"],
            "prs": list(prs.values())}


def _git(*args: str) -> str | None:
    """No optional locks: a status here must never take an index.lock an agent then hits."""
    try:
        return subprocess.run(["git", "--no-optional-locks", *args], capture_output=True,
                              text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _active_at(path: str) -> float:
    dotgit = Path(path, ".git")
    try:
        gitdir = dotgit if dotgit.is_dir() else Path(dotgit.read_text().split(":", 1)[1].strip())
    except (OSError, IndexError):
        return 0.0
    return max((f.stat().st_mtime for f in (gitdir / "index", gitdir / "logs" / "HEAD")
                if f.exists()), default=0.0)


def _worktree(path: str, branch: str, repo: str | None) -> dict | None:
    status = _git("-C", path, "status", "--porcelain=v2", "--branch", "-uno")
    if status is None:
        return None
    lines = status.splitlines()
    # An upstream that is configured but gone was pushed and then deleted, almost always
    # on merge: its commits live in main now even though no remote ref holds them. A
    # detached HEAD is a scratch merge or a bisect, not a branch of work.
    gone = (any(x.startswith("# branch.upstream") for x in lines)
            and not any(x.startswith("# branch.ab") for x in lines))
    unpushed = 0 if gone or not branch else int(
        (_git("-C", path, "rev-list", "--count", "HEAD", "--not", "--remotes") or "0").strip())
    return {"path": path, "repo": repo, "branch": branch, "unpushed": unpushed,
            "dirty": sum(not x.startswith("#") for x in lines), "active_at": _active_at(path)}


def scan_worktrees(cwds: list[str]) -> list[dict]:
    """Every worktree of every repository a pane sits in: the panes say which repositories
    matter, and each repository's own list finds worktrees wherever they live."""
    commons = {(_git("-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
                or "").strip() for cwd in set(cwds) if cwd}
    out = []
    for common in sorted(commons - {""}):
        repo = None
        for block in (_git("--git-dir", common, "worktree", "list", "--porcelain") or ""
                      ).strip().split("\n\n"):
            fields = dict([*line.split(" ", 1), ""][:2] for line in block.splitlines())
            path = fields.get("worktree")
            if not path or "bare" in fields or "prunable" in fields or not os.path.isdir(path):
                continue
            repo = repo or github_repository(path)  # one origin per repository
            branch = fields.get("branch", "").removeprefix("refs/heads/")
            if wt := _worktree(path, branch, repo):
                out.append(wt)
    return out


def _pane(p: dict) -> dict:
    return {"pane_id": p.get("pane_id"), "label": p.get("title") or p.get("label"),
            "session": p.get("session"), "window_index": p.get("window_index"),
            "activity": p.get("activity"), "since": p.get("state_since")}


def build(github: dict, worktrees: list[dict], panes: list[dict], now: float) -> dict:
    """The three lanes, each a list of workstreams with their rows. Pure: no I/O."""
    prs = {(p["repo"].lower(), p["number"]): p for p in github.get("prs", [])}
    heads = {}
    for key, p in sorted(prs.items(), key=lambda kv: kv[1]["state"] == "OPEN"):
        heads[key[0], p["head"]] = key  # an open PR wins a head name over a closed one
    root = {key: key for key in prs}

    def find(key):
        while root[key] != key:
            key = root[key]
        return key

    for key, p in prs.items():  # a stack: this PR's base is another PR's head
        up = heads.get((key[0], p["base"])) if p["stacked"] else None
        if up and find(up) != find(key):
            root[find(key)] = find(up)

    def tree(cwd):  # the innermost worktree holding cwd
        hits = [w for w in worktrees if cwd and (cwd + "/").startswith(w["path"] + "/")]
        return max(hits, key=lambda w: len(w["path"]), default=None)

    def head_of(w):
        return w and w["repo"] and heads.get((w["repo"].lower(), w["branch"]))

    owners, pane_ws, sitting = defaultdict(list), {}, defaultdict(int)
    for p in panes:
        w = tree(p.get("cwd"))
        sitting[w and w["path"]] += 1
        keys = [k for k in [*((r["repo"].lower(), r["number"]) for r in p.get("prs") or []),
                            head_of(w)] if k in prs]
        for key in dict.fromkeys(keys):
            owners[key].append(_pane(p))
        pane_ws[p.get("pane_id")] = find(keys[-1]) if keys else None  # the latest work wins
    covered = set(sitting) | {w["path"] for w in worktrees if head_of(w) in owners}

    lanes = {lane: defaultdict(list) for lane in ("waiting", "moving", "dropped")}
    for key, p in prs.items():
        ws, ref = find(key), {k: p[k] for k in ("repo", "number", "title", "url")}
        if p["state"] == "MERGED" and p["merged_at"] >= now - RECENT:
            lanes["moving"][ws].append({**ref, "kind": "merged", "at": p["merged_at"]})
        if p["state"] != "OPEN" or p["updated_at"] < now - HORIZON:  # a pane's old reference
            continue
        if p["pushed_at"] >= now - RECENT:
            lanes["moving"][ws].append({**ref, "kind": "pushed", "at": p["pushed_at"]})
        lanes["moving"][ws] += [{**ref, "kind": "reviewed", "by": r["by"], "state": r["state"],
                                 "at": r["at"]} for r in p["reviews"] if r["at"] >= now - RECENT]
        mine = p["author"] == github.get("viewer")
        green = (not p["draft"] and not p["stacked"] and p["mergeable"] == "MERGEABLE"
                 and p["checks"] in ("SUCCESS", None))
        waiting = [reason for reason, hit in (
            ("review_requested", p["asked"]),
            ("approved", mine and green and p["decision"] == "APPROVED"),
            ("mergeable", mine and green and p["decision"] is None),  # no review required
        ) if hit]
        dropped = [reason for reason, hit in (
            ("no_reviewer", mine and not p["draft"] and p["decision"] == "REVIEW_REQUIRED"
             and not p["reviewers"] and not p["reviews"]),
            ("checks_failed", p["checks"] in ("FAILURE", "ERROR")),
            ("base_merged", p["base_merged"]),
            ("conflicts", p["mergeable"] == "CONFLICTING" and not p["base_merged"]),
            ("stale", p["updated_at"] < now - STALE),
        ) if hit]
        if waiting or dropped:
            lanes["waiting" if waiting else "dropped"][ws].append(
                {**ref, "kind": "pr", "reasons": waiting + dropped, "at": p["updated_at"],
                 "draft": p["draft"], "panes": owners[key]})

    for p in panes:
        w, row = tree(p.get("cwd")), {"kind": "pane", "pane": _pane(p), "at": p.get("state_since")}
        if p.get("activity") == "waiting" and p.get("waiting_on") == "user":
            question = p.get("question")
            lanes["waiting"][pane_ws[p.get("pane_id")]].append({**row, "reasons": ["needs_you"],
                "text": (question.get("prompt") if isinstance(question, dict) else None)
                or p.get("headline")})
        # Only a pane alone in its worktree owns the dirt: a shared checkout's is no one's.
        elif (p.get("activity") == "idle" and w and w["dirty"] and sitting[w["path"]] == 1
              and (p.get("state_since") or now) < now - IDLE_DIRTY):
            lanes["dropped"][pane_ws[p.get("pane_id")]].append(
                {**row, "reasons": ["idle_dirty"], "dirty": w["dirty"]})

    for w in worktrees:
        key = head_of(w)
        landed = key and prs[key]["state"] == "MERGED"
        if (w["path"] in covered or w["active_at"] < now - HORIZON
                or not (w["dirty"] or (w["unpushed"] and not landed))):
            continue
        lanes["dropped"][find(key) if key else None].append(
            {"kind": "worktree", "reasons": ["no_pane"], "at": w["active_at"],
             **{k: w[k] for k in ("path", "repo", "branch", "dirty", "unpushed")}})

    def workstream(ws):
        return ws and {"id": f"{prs[ws]['repo']}#{prs[ws]['number']}", "name": prs[ws]["title"]}

    return {lane: sorted(({"workstream": workstream(ws),
                           "items": sorted(rows, key=lambda r: -(r["at"] or 0))}
                          for ws, rows in groups.items() if rows),
                         key=lambda g: -(g["items"][0]["at"] or 0))
            for lane, groups in lanes.items()}


class OpenLoops:
    """Owns the cache. refresh() runs on a worker thread; report() only reads."""

    def __init__(self):
        self.github, self.worktrees = {}, []
        self.fetched_at = self.scanned_at = self.error = None

    def refresh(self, panes: list[dict], now: float | None = None) -> None:
        now = time.time() if now is None else now
        refs = list(dict.fromkeys((r["repo"], r["number"])
                                  for p in panes for r in p.get("prs") or []))
        try:
            self.github, self.fetched_at, self.error = fetch_github(refs, now), now, None
        except Exception as e:  # noqa: BLE001 - keep the last good answer, say it is old
            logger.info("open loops: GitHub fetch failed: %s", type(e).__name__)
            self.error = "unavailable"
        self.worktrees, self.scanned_at = scan_worktrees([p.get("cwd") for p in panes]), now

    async def run(self, watcher) -> None:
        while not watcher.booted():  # before the first tick there are no panes to join
            await watcher.wait_for_state_change(watcher.state_version(), 30)
        while True:
            try:
                await asyncio.to_thread(self.refresh, list(watcher.states))
            except Exception:  # one bad refresh must not end the loop
                logger.warning("open loops refresh failed", exc_info=True)
            await asyncio.sleep(REFRESH)

    def report(self, panes: list[dict], now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {"generated_at": now, "fetched_at": self.fetched_at, "error": self.error,
                "scanned_at": self.scanned_at, "older_open_prs": self.github.get("older", 0),
                "lanes": build(self.github, self.worktrees, panes, now)}
