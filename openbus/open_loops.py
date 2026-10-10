"""Open loops: which pieces of work have stopped moving, and what would unstick each.

The join of three sources: GitHub pull requests (a few paged GraphQL requests per REFRESH,
cached), pane state (live from the watcher, including its PR associations) and worktrees
(local git, cached on the same cadence). Requests render from the cache and never touch the
network. Workstreams come from stacks, then labels, then an optional keyword map. See
docs/design/open-loops.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from .pr_titles import run_gh
from .repository import github_repository

logger = logging.getLogger(__name__)
REFRESH = 900  # a few requests however many PRs, so a quarter hour costs GitHub little
POLL = 60  # how often the loop looks for a pane association the cache has not fetched
HORIZON = 14 * 86400  # older open PRs and idle worktrees are counted, not listed
STALE = 3 * 86400  # an open PR untouched this long has dropped
IDLE_DIRTY = 86400  # a pane idle this long over uncommitted changes has dropped
RECENT = 86400  # "Moving" looks back this far until daily snapshots make it a diff
CHUNK = 20  # pane-associated PRs looked up per request, beyond the searches
STACK_DEPTH = 3  # levels of unfetched stack parents looked up toward a stack's root
PAGE, MAX_PAGES = 40, 5  # a search's page, and how many pages before the rest are dropped
PAGED = "pageInfo { hasNextPage endCursor } nodes { ...P }"
AFTER = "%AFTER%"  # where a page's cursor goes; % cannot occur in a repository name
MAX_STAT = 200  # uncommitted files whose mtimes date a worktree's last activity

FIELDS = """number title url isDraft state updatedAt mergedAt author { login }
baseRefName headRefName mergeable reviewDecision
repository { nameWithOwner defaultBranchRef { name } }
reviewRequests(first: 10) { nodes { requestedReviewer { __typename } } }
latestReviews(first: 10) { nodes { author { __typename login } state submittedAt } }
commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
baseRef { associatedPullRequests(states: MERGED, first: 1) { totalCount } }
labels(first: 20) { nodes { name } }"""


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(stamp: str | None) -> float:
    return datetime.fromisoformat(stamp).timestamp() if stamp else 0.0


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
        "committed_at": _ts(commit.get("committedDate")),  # not the push: GitHub keeps no such date
        # A bot (a Copilot review, say) is not a reviewer a person will answer for.
        "reviewers": sum(r["requestedReviewer"].get("__typename") in ("User", "Team")
                         for r in node["reviewRequests"]["nodes"] if r["requestedReviewer"]),
        "reviews": [{"by": r["author"]["login"], "state": r["state"],
                     "at": _ts(r["submittedAt"])} for r in humans],
        "labels": [x["name"].removeprefix("workstream:") for x in node["labels"]["nodes"]
                   if x["name"].startswith("workstream:")],
        # Stacked on a branch that merged (or was deleted): it needs a retarget.
        "base_merged": stacked and (node.get("baseRef") is None or
                                    node["baseRef"]["associatedPullRequests"]["totalCount"] > 0),
    }


def _graphql(body: str, stopping: Event | None = None) -> dict:
    """One request, retried once: GitHub answers a search that runs long with a 502."""
    fragment = f"fragment P on PullRequest {{ {FIELDS} }} " if "...P" in body else ""
    for _ in range(2):
        with suppress(ValueError):  # a 502 arrives as an HTML page
            reply = json.loads(run_gh(["api", "graphql", "-f",
                                       f"query={fragment}query {{ viewer {{ login }} {body} }}"],
                                      60, stopping) or "{}")
            # Partial errors are fine for a pane's reference that names nothing or a count,
            # not for a search of PRs: an empty one would replace good rows with none.
            data = reply.get("data") or {}
            if data.get("viewer") and (data.get("s") or not body.startswith("s:")
                                       or "issueCount" in body):
                return data
    raise RuntimeError("GitHub unavailable")


def _lookups(lookups: list[tuple[str, str]]):
    """("ref", body) per CHUNK of (repository, selection) lookups."""
    for i in range(0, len(lookups), CHUNK):
        yield "ref", " ".join(
            f"r{n}: repository(owner: {json.dumps(repo.split('/', 1)[0])}, name: "
            f"{json.dumps(repo.split('/', 1)[1])}) {{ {selection} }}"
            for n, (repo, selection) in enumerate(lookups[i:i + CHUNK]))


def requests(refs: list[tuple[str, int]], now: float):
    """(tag, body) per request: each search a page at a time, the pane-associated PRs in
    chunks, and the count of older PRs. Many small requests, because one that asks for
    everything outlasts GitHub's own timeout on a busy account."""
    horizon, recent = _iso(now - HORIZON), _iso(now - RECENT)
    mine, asked = (f"is:pr is:open {who} archived:false" for who in ("author:@me",
                                                                    "review-requested:@me"))
    for tag, q in (("mine", f"{mine} updated:>={horizon}"),
                   ("asked", f"{asked} updated:>={horizon}"),
                   ("merged", f"is:pr is:merged involves:@me merged:>={recent}")):
        yield tag, f's: search(type: ISSUE, first: {PAGE}, query: "{q}"{AFTER}) {{ {PAGED} }}'
    # issueOrPullRequest, not pullRequest: a number that names an issue is just null.
    yield from _lookups([(repo, f"issueOrPullRequest(number: {int(num)}) {{ ...P }}")
                         for repo, num in refs])
    for who in (mine, asked):  # disjoint: nobody can be asked to review their own PR
        q = f"{who} updated:<{horizon}"
        yield "older", f's: search(type: ISSUE, first: 0, query: "{q}") {{ issueCount }}'


def fetch_github(refs: list[tuple[str, int]], now: float, stopping: Event | None = None) -> dict:
    prs, older, truncated, data = {}, 0, False, {}

    def run(tag, body):
        nonlocal older, truncated, data
        after = ""
        for _ in range(MAX_PAGES):  # only a search has pages; the rest stop after one
            data = _graphql(body.replace(AFTER, after), stopping)
            result = data.get("s") or {}
            older += result.get("issueCount", 0)
            looked = [v for k, v in data.items() if k.startswith("r") and v]
            for node in result.get("nodes") or [
                    n for v in looked for n in [v.get("issueOrPullRequest"),
                                                *(v.get("pullRequests") or {}).get("nodes", [])]]:
                if node and "number" in node:  # an issue matches the fragment as {}
                    pr = prs.setdefault((node["repository"]["nameWithOwner"].lower(),
                                         node["number"]), _pr(node))
                    pr["asked"] = pr.get("asked") or tag == "asked"
            page = result.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return
            after = f", after: {json.dumps(page['endCursor'])}"
        truncated = True  # still more pages: say so rather than look complete

    for tag, body in requests(refs, now):
        run(tag, body)
    # A stack's parent may be nobody's search result (a teammate's PR, or yours from before
    # the horizon): look up the PR whose head is each unmatched base, a level at a time.
    tried = set()
    for _ in range(STACK_DEPTH):
        heads = {(p["repo"].lower(), p["head"]) for p in prs.values()} | tried
        bases = list(dict.fromkeys((p["repo"], p["base"]) for p in prs.values()
                                   if p["stacked"] and (p["repo"].lower(), p["base"]) not in heads))
        tried |= {(repo.lower(), base) for repo, base in bases}
        newest = "first: 1, orderBy: {field: UPDATED_AT, direction: DESC}"
        parent = "pullRequests(headRefName: %s, " + newest + ") { nodes { ...P } }"
        for tag, body in _lookups([(repo, parent % json.dumps(base)) for repo, base in bases]):
            run(tag, body)
    return {"viewer": data["viewer"]["login"], "older": older, "truncated": truncated,
            "prs": list(prs.values())}


def _git(*args: str) -> str | None:
    """No optional locks: a status here must never take an index.lock an agent then hits."""
    try:
        return subprocess.run(["git", "--no-optional-locks", *args], capture_output=True,
                              text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _mtime(path: Path) -> float:
    """A deleted file has no mtime; its deletion touched the nearest directory still there."""
    for each in (path, *path.parents):
        with suppress(OSError):
            return each.stat().st_mtime
    return 0.0


def _active_at(path: str, changed: list[str]) -> float:
    """The last commit, checkout or staging (the index and HEAD log), or the last edit to a
    file that is still uncommitted: an unstaged edit touches neither git file."""
    dotgit = Path(path, ".git")
    try:
        gitdir = dotgit if dotgit.is_dir() else Path(dotgit.read_text().split(":", 1)[1].strip())
    except (OSError, IndexError):
        return 0.0
    return max(_mtime(f) for f in [gitdir / "index", gitdir / "logs" / "HEAD",
                                   *(Path(path, name) for name in changed[:MAX_STAT])])


def _worktree(path: str, branch: str, repo: str | None) -> dict | None:
    # Every untracked file, not its directory: editing a file leaves the directory's mtime.
    status = _git("-C", path, "status", "--porcelain", "-z", "--branch", "-uall")
    if status is None:
        return None
    header, *fields = status.split("\0")
    changed, rest = [], iter(fields)
    for entry in rest:  # "XY name"; a rename or copy is followed by its old name
        if entry:
            changed.append(entry[3:])
            if {"R", "C"} & set(entry[:2]):
                next(rest, None)
    # An upstream that is pushed and then deleted (gone) almost always went on merge: its
    # commits live in main now even though no remote ref holds them. A detached HEAD is a
    # scratch merge or a bisect, not a branch of work.
    unpushed = 0 if "[gone]" in header or not branch else int(
        (_git("-C", path, "rev-list", "--count", "HEAD", "--not", "--remotes") or "0").strip())
    return {"path": path, "repo": repo, "branch": branch, "unpushed": unpushed,
            "dirty": len(changed), "active_at": _active_at(path, changed)}


def scan_worktrees(cwds: list[str], stopping: Event | None = None,
                   previous: list[dict] = ()) -> list[dict]:
    """Every worktree of every repository a pane sits in: the panes say which repositories
    matter, and each repository's own list finds worktrees wherever they live. Where git
    fails, the previous scan's rows stand in, so a hiccup does not hide drift."""
    old = {w["path"]: w for w in previous}
    commons = set()
    for cwd in filter(None, set(cwds)):
        found = _git("-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        # Discovery failed: the repository a previous scan placed this cwd in still counts.
        commons |= {found.strip()} if found else {
            w["common"] for w in previous if (cwd + "/").startswith(w["path"] + "/")}
    out = []
    for common in sorted(commons - {""}):
        repo, listing = None, _git("--git-dir", common, "worktree", "list", "--porcelain")
        if listing is None:
            out += [w for w in previous if w.get("common") == common]
            continue
        for block in listing.strip().split("\n\n"):
            fields = dict([*line.split(" ", 1), ""][:2] for line in block.splitlines())
            path = fields.get("worktree")
            if stopping is not None and stopping.is_set():
                return out
            if not path or "bare" in fields or "prunable" in fields or not os.path.isdir(path):
                continue
            repo = repo or github_repository(path)  # one origin per repository
            branch = fields.get("branch", "").removeprefix("refs/heads/")
            wt = _worktree(path, branch, repo)
            if wt := (wt and {**wt, "common": common}) or old.get(path):
                out.append(wt)
    return out


def _pane(p: dict) -> dict:
    return {"pane_id": p.get("pane_id"), "label": p.get("title") or p.get("label"),
            "session": p.get("session"), "window_index": p.get("window_index"),
            "activity": p.get("activity"), "since": p.get("state_since")}


def build(github: dict, worktrees: list[dict], panes: list[dict], now: float,
          keywords: dict[str, list[str]] | None = None) -> dict:
    """The three lanes, each a list of workstreams with their rows, and how many dropped
    worktrees the horizon hid. Pure: no I/O."""
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
    stacks = defaultdict(list)
    for key, p in prs.items():
        stacks[find(key)].append(p)
    named = {}  # stack root -> its workstream: a label, else a keyword, else the stack itself
    for top, group in stacks.items():
        text = " ".join(f"{p['title']} {p['head']}" for p in group).lower()
        name = next((label for p in group for label in p["labels"]), None) or next(
            (name for name, words in (keywords or {}).items()
             if any(w and w.lower() in text for w in words)), None)
        named[top] = ({"id": f"workstream:{name}", "name": name} if name else
                      {"id": f"{prs[top]['repo']}#{prs[top]['number']}", "name": prs[top]["title"]})

    def ws(key):
        return key and named[find(key)]["id"]

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
        pane_ws[p.get("pane_id")] = ws(keys[-1]) if keys else None  # the latest work wins
    covered = set(sitting) | {w["path"] for w in worktrees if head_of(w) in owners}

    lanes = {lane: defaultdict(list) for lane in ("waiting", "moving", "dropped")}
    for key, p in prs.items():
        group, ref = ws(key), {k: p[k] for k in ("repo", "number", "title", "url")}
        if p["state"] == "MERGED" and p["merged_at"] >= now - RECENT:
            lanes["moving"][group].append({**ref, "kind": "merged", "at": p["merged_at"]})
        if p["state"] != "OPEN" or p["updated_at"] < now - HORIZON:  # a pane's old reference
            continue
        if p["committed_at"] >= now - RECENT:
            lanes["moving"][group].append({**ref, "kind": "committed", "at": p["committed_at"]})
        lanes["moving"][group] += [{**ref, "kind": "reviewed", "by": r["by"], "state": r["state"],
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
             and not p["reviewers"]),  # a past review is not someone still asked
            ("checks_failed", p["checks"] in ("FAILURE", "ERROR")),
            ("base_merged", p["base_merged"]),
            ("conflicts", p["mergeable"] == "CONFLICTING" and not p["base_merged"]),
            ("stale", p["updated_at"] < now - STALE),
        ) if hit]
        if waiting or dropped:
            lanes["waiting" if waiting else "dropped"][group].append(
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

    older = 0
    for w in worktrees:
        key = head_of(w)
        landed = key and prs[key]["state"] == "MERGED"
        if w["path"] in covered or not (w["dirty"] or (w["unpushed"] and not landed)):
            continue
        if w["active_at"] < now - HORIZON:
            older += 1
            continue
        lanes["dropped"][ws(key)].append(
            {"kind": "worktree", "reasons": ["no_pane"], "at": w["active_at"],
             **{k: w[k] for k in ("path", "repo", "branch", "dirty", "unpushed")}})

    info = {w["id"]: w for w in named.values()}
    return {"older_worktrees": older, "lanes": {
        lane: sorted(({"workstream": info.get(group),
                       "items": sorted(rows, key=lambda r: -(r["at"] or 0))}
                      for group, rows in groups.items() if rows),
                     key=lambda g: -(g["items"][0]["at"] or 0))
        for lane, groups in lanes.items()}}


def keywords() -> dict[str, list[str]]:
    """TMUXRC_WORKSTREAMS: {"name": ["word", ...]}, the user's last-resort grouping."""
    try:
        value = json.loads(os.environ.get("TMUXRC_WORKSTREAMS") or "{}")
    except ValueError:
        return {}
    return {str(k): [str(w) for w in v] for k, v in value.items() if isinstance(v, list)
            } if isinstance(value, dict) else {}


def _refs(panes: list[dict]) -> list[tuple[str, int]]:
    return list(dict.fromkeys((r["repo"], r["number"]) for p in panes for r in p.get("prs") or []))


class OpenLoops:
    """Owns the cache. refresh() runs on a worker thread; report() only reads."""

    def __init__(self):
        self.github, self.worktrees, self.refs = {}, [], set()
        self.fetched_at = self.scanned_at = self.error = None
        self.stopping = Event()  # set on shutdown, so a refresh in its thread stops early

    def refresh(self, panes: list[dict], now: float | None = None) -> None:
        now = time.time() if now is None else now
        refs = _refs(panes)
        try:
            self.github, self.fetched_at, self.error = (fetch_github(refs, now, self.stopping),
                                                        now, None)
            self.refs = set(refs)  # looked up: a failure leaves them due for the next minute
        except Exception as e:  # noqa: BLE001 - keep the last good answer, say it is old
            logger.info("open loops: GitHub fetch failed: %s", type(e).__name__)
            self.error = "unavailable"
        worktrees = scan_worktrees([p.get("cwd") for p in panes], self.stopping, self.worktrees)
        if not self.stopping.is_set():  # a scan cut short by shutdown is partial
            self.worktrees, self.scanned_at = worktrees, now

    async def run(self, watcher) -> None:
        while not watcher.booted():  # before the first tick there are no panes to join
            await watcher.wait_for_state_change(watcher.state_version(), 30)
        due = 0.0
        try:
            while True:
                # Early as well as on the cadence when a pane gains an association the cache
                # has not looked up: at startup the classifier restores them a tick later.
                panes = list(watcher.states)
                if time.monotonic() >= due or not set(_refs(panes)) <= self.refs:
                    due = time.monotonic() + REFRESH
                    try:
                        await asyncio.to_thread(self.refresh, panes)
                    except Exception:  # one bad refresh must not end the loop
                        logger.warning("open loops refresh failed", exc_info=True)
                await asyncio.sleep(POLL)
        finally:  # cancelling the task cannot stop its thread; this lets the thread stop
            self.stopping.set()

    def report(self, panes: list[dict], now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {"generated_at": now, "fetched_at": self.fetched_at, "error": self.error,
                "scanned_at": self.scanned_at, "older_open_prs": self.github.get("older", 0),
                "truncated": self.github.get("truncated", False),
                **build(self.github, self.worktrees, panes, now, keywords())}
