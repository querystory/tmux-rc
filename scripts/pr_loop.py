"""Drive one pull request through CI and review until it is ready for a human merge decision.

The loop is a small graph: code nodes do the plumbing (gh, git, polling), an LLM is called
only for judgement (fix this CI failure, triage this review thread), gates stop the run
with a reason, and the last node is a human. It never merges. docs/design/pr-loop.md says
why it is shaped this way.

    uv run python scripts/pr_loop.py <PR> [--repo owner/name] [--dry-run] [--max-rounds N]
                                          [--engine claude|codex] [--check CMD]...
    uv run python scripts/pr_loop.py <PR> --summary-only [--check CMD]...   # rebuild the page
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import html
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

COPILOT = "copilot-pull-request-reviewer"
MARKER = "<!-- pr-loop -->"  # tags our own replies, so a rerun can tell them apart
SIGNATURE = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
POLL = 30
FAILED = {"fail", "cancel"}
READ_TOOLS = ["Read", "Grep", "Glob"]
EDIT_TOOLS = [*READ_TOOLS, "Edit", "Write", "Bash(make lint)", "Bash(make test)",
              "Bash(make fmt)", "Bash(uv run *)",
              "Bash(git diff*)", "Bash(git status*)", "Bash(node --test*)"]
DENY_TOOLS = ["Bash(git commit*)", "Bash(git push*)", "Bash(git reset*)",
              "Bash(git checkout*)", "Bash(gh *)"]
RESULT_SCHEMA = {"type": "object", "required": ["status", "summary"], "properties": {
    "status": {"enum": ["fixed", "blocked"]}, "summary": {"type": "string"}}}
TRIAGE_SCHEMA = {"type": "object", "required": ["action", "reply_text", "rationale"],
                 "properties": {"action": {"enum": ["fix", "reply", "decline"]},
                                "reply_text": {"type": "string"},
                                "rationale": {"type": "string"}}}


class BlockedError(Exception):
    """A gate failed: the run halts here with this reason and tells the human."""


# ── Pure logic: observation + state -> next action. No I/O, unit-tested. ───────────────────

def ci_status(checks):
    """Collapse `gh pr checks` buckets into pending / fail / pass."""
    buckets = {c.get("bucket") for c in checks}
    if not checks or "pending" in buckets:
        return "pending"
    return "fail" if buckets & FAILED else "pass"


def copilot_reviewed(reviews, head):
    mine = [r for r in reviews if (r.get("author") or {}).get("login") == COPILOT]
    return bool(mine) and ((mine[-1].get("commit") or {}).get("oid") == head)


def pending_threads(threads, handled):
    """Unresolved threads still needing triage: not ours last, not already triaged as-is."""
    out = []
    for t in threads:
        comments = t["comments"]["nodes"]
        if t["isResolved"] or not comments or MARKER in comments[-1]["body"]:
            continue
        seen = handled.get(t["id"], {})
        retry = seen.get("step") == "blocked" or (seen.get("action") == "fix"
                                                  and not seen.get("sha"))  # interrupted
        if seen.get("last_comment") == comments[-1]["databaseId"] and not retry:
            continue
        out.append(t)
    return out


def next_action(obs, st, max_rounds, max_ci_attempts):
    """The graph's edges. Returns (node, reason)."""
    if obs["state"] != "OPEN" or obs["isDraft"]:
        return "blocked", f"PR is {obs['state'].lower()}{' (draft)' if obs['isDraft'] else ''}"
    if obs["mergeable"] == "CONFLICTING":
        return "blocked", "PR conflicts with its base; rebasing is left to a human in v1"
    if obs["ci"] == "fail" or obs["threads"]:
        if len(st["rounds"]) >= max_rounds:
            return "done", f"max rounds ({max_rounds}) reached"
        if obs["ci"] == "fail":
            if st["ci_attempts"] >= max_ci_attempts:
                return "blocked", f"CI still failing after {max_ci_attempts} fix attempts"
            return "fix_ci", "CI is failing"
        return "triage", f"{len(obs['threads'])} unresolved review thread(s)"
    if not obs["reviewed"] and st.get("review_requested") != obs["head"]:
        return "request_review", "Copilot has not reviewed the head commit"  # alongside CI
    if obs["ci"] == "pending":
        return "wait", "CI pending"
    if not obs["reviewed"]:
        return "wait", "waiting for Copilot to review the head commit"
    return "done", "CI green, Copilot reviewed the head, no threads left to triage"


def parse_llm_json(text, schema):
    """The last JSON object in an LLM's answer, checked against the enums in `schema`."""
    obj = text if isinstance(text, dict) else None
    text, i = ("" if obj else text or ""), 0
    while (i := text.find("{", i)) >= 0:  # top-level objects only, so nesting can't win
        try:
            cand, end = json.JSONDecoder().raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        obj = cand if isinstance(cand, dict) else obj
        i = end
    if obj is None:
        raise ValueError(f"no JSON object in LLM output: {text[-200:]!r}")
    for key in schema["required"]:
        if key not in obj:
            raise ValueError(f"LLM JSON lacks {key!r}: {obj}")
    for key, spec in schema["properties"].items():
        if "enum" in spec and obj[key] not in spec["enum"]:
            raise ValueError(f"LLM JSON {key}={obj[key]!r} not in {spec['enum']}")
    return obj


def reply_body(text, sha=None):
    fixed = f"\n\nFixed in {sha}." if sha else ""
    return f"{text.strip()}{fixed}\n\n{SIGNATURE}\n{MARKER}"


# ── The human gate: a decision built only from what the run observed ───────────────────────
# Never from LLM prose. A triage call's rationale is its plan, written before any fix ran
# (on the first live run it even said the edits were "denied", because the read-only call
# had tried to apply them itself), so the page shows commits, diffs, CI, replies and thread
# state as GitHub reports them, and the reviewer's own words for the risk.

TEST_PATH = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.\w+$|\.test\.\w+$")
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`(])")
SUBJECT_MAX = 58  # commit subjects longer than this are cut at a word, with an ellipsis


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 'es' if word.endswith('x') else 's'}"


def first_sentence(text, limit=240):
    s = SENTENCE.split(" ".join((text or "").split()), maxsplit=1)[0]
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def short_subject(text):
    """A one-line commit subject: whole if it fits, else cut at a word with an ellipsis."""
    text = " ".join(text.split())
    return text if len(text) <= SUBJECT_MAX else text[:SUBJECT_MAX].rsplit(" ", 1)[0] + "…"


def fix_line(subject):
    """What a fix commit did, from its subject. Older runs cut subjects at 60 mid-word."""
    s = subject.removeprefix("Review: ").removeprefix("Fix CI: ")
    if len(s) >= 60 and not s.endswith("…"):
        s = s.rsplit(" ", 1)[0].rstrip(":,;") + "…"
    return s


def why_text(body, limit=420):
    """The PR's own reason for existing: its "Why" section, else its first paragraph."""
    body = (body or "").replace("\r", "")
    m = re.search(r"^#+\s*why\s*\n+(.+?)(?:\n\s*\n|\Z)", body,
                  re.IGNORECASE | re.MULTILINE | re.DOTALL)
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip() and not p.lstrip().startswith("#")]
    text = " ".join((m[1] if m else paras[0] if paras else "").split())
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def covered(cmd, ran):
    """Whether recipe line `cmd` was run by one of the commands in `ran` (or all of them)."""
    return any(r == "make test" or r.startswith(cmd) for r in ran)


def decide(repo, pr, st, raw, verify, outcome):
    """Everything the page and the stdout summary say, as plain data. Pure: unit-tested."""
    d = raw.get("pr") or {}
    head = d.get("headRefOid") or ""
    gh_threads = {t["id"]: t for t in ((d.get("reviewThreads") or {}).get("nodes") or [])}
    on_pr = {n["commit"]["oid"] for n in ((d.get("commits") or {}).get("nodes") or [])}
    reviews = [r for r in ((d.get("reviews") or {}).get("nodes") or [])
               if (r.get("author") or {}).get("login") == COPILOT]
    round_of = {tid: r["n"] for r in st["rounds"] for tid in r.get("triage", [])}
    commits = raw.get("commits") or {}
    cards = []
    for tid, t in st["threads"].items():
        g = gh_threads.get(tid, {})
        cs = (g.get("comments") or {}).get("nodes") or []
        first = cs[0] if cs else {}
        ours = [c for c in cs if MARKER in (c.get("body") or "")]
        c = commits.get(t.get("sha") or "", {})
        files = c.get("files", [])
        cards.append({
            "id": tid, "path": t["path"], "line": g.get("line"), "round": round_of.get(tid),
            "action": t["action"], "reviewer": (first.get("author") or {}).get("login", "?"),
            "risk": first_sentence(first.get("body", "")) or t["path"],
            "comment": first.get("body", ""), "thread_url": first.get("url"),
            "reply_url": ours[-1]["url"] if ours else None,
            "resolved": g.get("isResolved"), "sha": t.get("sha"),
            "subject": fix_line(c["subject"]) if c.get("subject") else None,
            "commit_url": c.get("url"), "on_pr": t.get("sha") in on_pr if t.get("sha") else None,
            "files": files, "adds": sum(f["add"] for f in files),
            "dels": sum(f["del"] for f in files),
            "tests": sum(f["add"] for f in files if TEST_PATH.search(f["name"]))})
    fixes = [x for x in cards if x["action"] == "fix" and x["sha"]]
    ci_shas = [r["fix_ci"]["sha"] for r in st["rounds"] if (r.get("fix_ci") or {}).get("sha")]
    ci_commits = [commits.get(s, {}) for s in ci_shas]
    loop_files = [f for x in fixes for f in x["files"]] + [f for c in ci_commits
                                                         for f in c.get("files", [])]
    checks = raw.get("checks") or []
    buckets = [c.get("bucket") for c in checks]
    ci = ci_status(checks) if checks else "unknown"
    unresolved = [t for t in gh_threads.values() if not t["isResolved"]]
    reviewed = bool(reviews) and (reviews[-1].get("commit") or {}).get("oid") == head
    series = [{"label": f"Round {r['n']}", "head": r["head"][:8], "n": len(r.get("triage", []))}
              for r in st["rounds"] if r.get("action") == "triage"]
    if reviewed and (not st["rounds"] or st["rounds"][-1]["head"] != head):
        series.append({"label": "Final review", "head": head[:8], "n": len(unresolved)})

    recipe, ran = raw.get("recipe") or [], verify or []
    caveats, soft = [], []
    if verify is None:
        soft.append("the checks each fix had to pass were not recorded")
        caveats.append("This run did not record which local checks each fix had to pass.")
    for cmd in recipe:
        if not covered(cmd, ran):
            in_ci = cmd in (raw.get("workflows") or "")
            caveats.append(f"`{cmd}` (part of `make test`) was not run locally before each fix"
                           + ("; CI runs it, and CI passed on the head." if in_ci and ci == "pass"
                              else "."))
            if not (in_ci and ci == "pass"):
                soft.append(f"`{cmd}` was never run on the fixes")
    for v in ran:
        for skipped in re.findall(r"--(?:deselect|ignore)[= ](\S+)", v):
            in_ci = any("pytest" in r and r in (raw.get("workflows") or "") for r in recipe)
            caveats.append(f"Skipped locally: `{skipped}`" + (
                "; CI's full test run includes it and passed." if in_ci and ci == "pass"
                else "."))
    untested = [x for x in fixes if not x["tests"]]
    caveats += [f"The fix in `{x['sha'][:8]}` ({x['path']}) adds no test." for x in untested]
    if untested:
        soft.append(f"{len(untested)} fix{'es' if len(untested) > 1 else ''} without a test")
    caveats += [f"An unfinished fix from an earlier attempt is still stashed in the PR worktree "
                f"(`{ln}`). It is not part of the PR." for ln in raw.get("stash") or []]
    if "skipping" in buckets:
        caveats.append("CI skipped: " + ", ".join(
            c["name"] for c in checks if c.get("bucket") == "skipping") + ".")
    not_on_pr = [x for x in fixes if x["on_pr"] is False]

    gates = [
        (d.get("state") == "OPEN" and not d.get("isDraft"),
         "PR is open" if d.get("state") == "OPEN" else f"PR is {str(d.get('state')).lower()}"),
        (d.get("mergeable") == "MERGEABLE", "No conflicts with the base"
         if d.get("mergeable") == "MERGEABLE" else f"Mergeable: {d.get('mergeable', '?')}"),
        (ci == "pass", f"CI green on {head[:8]}" if ci == "pass" else f"CI is {ci}"),
        (reviewed, "Copilot reviewed the latest commit" if reviewed
         else "Copilot has not reviewed the latest commit"),
        (not unresolved and bool(d),
         f"{len(gh_threads) - len(unresolved)}/{len(gh_threads)} review threads resolved"),
        (not not_on_pr, "Every fix commit is on the PR" if not not_on_pr
         else f"Not on the PR: {plural(len(not_on_pr), 'fix commit')}"),
        (not outcome.startswith("BLOCKED"), outcome if outcome.startswith("BLOCKED")
         else "The loop finished without a blocker"),
    ]
    blockers = [text for ok, text in gates if not ok]
    verdict = "Hold" if blockers else "Merge"
    confidence = "low" if blockers else "medium" if soft else "high"
    return {
        "repo": repo, "pr": pr, "title": d.get("title") or f"{repo}#{pr}",
        "why": why_text(d.get("body")), "url": d.get("url") or f"https://github.com/{repo}/pull/{pr}",
        "head": head, "base": d.get("baseRefName", "?"), "outcome": outcome,
        "verdict": verdict, "confidence": confidence, "gates": gates, "blockers": blockers,
        "soft": soft, "caveats": caveats, "cards": cards, "series": series,
        "checks": checks, "ci": ci, "reviewed": reviewed, "copilot_reviews": len(reviews),
        "stats": {
            "rounds": len(st["rounds"]), "ci_attempts": st["ci_attempts"],
            "fixed": len(fixes), "replied": sum(x["action"] == "reply" for x in cards),
            "declined": sum(x["action"] == "decline" for x in cards),
            "resolved": sum(bool(x["resolved"]) for x in cards),
            "replies": sum(bool(x["reply_url"]) for x in cards),
            "commits": len(fixes) + len(ci_shas),
            "adds": sum(f["add"] for f in loop_files), "dels": sum(f["del"] for f in loop_files),
            "tests": sum(f["add"] for f in loop_files if TEST_PATH.search(f["name"])),
            "pr_adds": d.get("additions", 0), "pr_dels": d.get("deletions", 0),
            "ci_pass": buckets.count("pass"), "ci_skip": buckets.count("skipping"),
            "ci_fail": sum(b in FAILED for b in buckets), "ci_total": len(buckets)},
        "verify": ran,
    }


def summary_text(m, where=""):
    s = m["stats"]
    lines = [f"{m['repo']}#{m['pr']}: {m['title']}",
             f"  recommendation: {m['verdict'].upper()} ({m['confidence']} confidence)"
             + (f"; blocked on: {'; '.join(m['blockers'])}" if m["blockers"] else ""),
             (f"  {plural(s['rounds'], 'round')}, threads {s['fixed']} fixed / "
              f"{s['replied']} replied / {s['declined']} declined, "
              f"{s['resolved']}/{len(m['cards'])} resolved, "
              f"{plural(s['commits'], 'commit')}, CI {m['ci']}"),
             *(f"  caveat: {c}" for c in m["caveats"][:4])]
    if len(m["caveats"]) > 4:
        lines.append(f"  (+{len(m['caveats']) - 4} more caveats on the page)")
    if where:
        lines.append(f"  page: {where}")
    return "\n".join(lines)


def meta_description(m):
    s, n = m["stats"], len(m["cards"])
    name = m["repo"].split("/")[1]
    if m["verdict"] == "Merge":
        head = (f"pr-loop recommends merging {name} #{m['pr']} ({m['confidence']} confidence): "
                f"CI green, Copilot reviewed the latest commit, {s['resolved']} of {n} review "
                f"threads resolved.")
    else:
        head = f"pr-loop says hold {name} #{m['pr']}: " + "; ".join(m["blockers"]) + "."
    return head + (f" Caveats: {'; '.join(m['soft'])}." if m["soft"] else "")


# ── I/O ────────────────────────────────────────────────────────────────────────────────────

def run(cmd, cwd=None, check=True, timeout=600, stdin=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       input=stdin, check=False)
    if check and p.returncode:
        raise BlockedError(f"`{' '.join(cmd)[:120]}` failed: {(p.stderr or p.stdout)[-500:]}")
    return p.stdout


def gh_json(*args):
    return json.loads(run(["gh", *args]) or "null")


THREADS_Q = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(
number:$n){headRefOid headRefName state isDraft mergeable isCrossRepository
reviews(last:50){nodes{author{login} commit{oid}}}
reviewThreads(first:100){nodes{id isResolved path line comments(last:50){nodes{
databaseId author{login} body diffHunk}}}}}}}"""
RESOLVE_Q = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"


def observe(repo, pr, st):
    owner, name = repo.split("/")
    d = gh_json("api", "graphql", "-f", f"query={THREADS_Q}", "-f", f"o={owner}",
                "-f", f"r={name}", "-F", f"n={pr}")["data"]["repository"]["pullRequest"]
    checks = json.loads(run(["gh", "pr", "checks", str(pr), "-R", repo, "--json",
                             "name,bucket,link"], check=False) or "[]")
    head = d["headRefOid"]
    return {"head": head, "branch": d["headRefName"], "state": d["state"],
            "isDraft": d["isDraft"], "mergeable": d["mergeable"],
            "fork": d["isCrossRepository"], "checks": checks, "ci": ci_status(checks),
            "reviewed": copilot_reviewed(d["reviews"]["nodes"], head),
            "threads": pending_threads(d["reviewThreads"]["nodes"], st["threads"])}


def state_path(repo, pr):
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "pr-loop" / repo.replace("/", "__") / f"pr-{pr}.json"


def load_state(path):
    if path.exists():
        return json.loads(path.read_text())
    return {"rounds": [], "threads": {}, "ci_attempts": 0, "review_requested": None}


def save_state(path, st):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(path)


def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.with_suffix(".lock").open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise BlockedError(f"another pr_loop holds {path.with_suffix('.lock')}")
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def worktrees(cwd):
    """{branch: path} for every worktree of the checkout at cwd, plus the main one's path."""
    out, path, main = {}, None, None
    for line in run(["git", "worktree", "list", "--porcelain"], cwd=cwd).splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
            main = main or path
        elif line.startswith("branch refs/heads/"):
            out[line.removeprefix("branch refs/heads/")] = path
    return out, main


def ensure_worktree(repo, pr, obs, dry):
    """The PR branch's own clean worktree at ../<repo>-wt/pr-<N>, or the one it already has."""
    if obs["fork"]:
        raise BlockedError("PR comes from a fork; v1 only pushes to same-repo branches")
    branch, url = obs["branch"], f"https://github.com/{repo}.git"
    have, main = worktrees(os.getcwd())
    name = repo.split("/")[1]
    wt = have.get(branch) or str(Path(main).parent / f"{name}-wt" / f"pr-{pr}")
    if dry:
        print(f"  worktree: {wt} ({'reuse' if Path(wt).exists() else 'would create'})")
        return wt
    run(["git", "fetch", url, f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], cwd=main)
    if not Path(wt).exists():
        local = run(["git", "branch", "--list", branch], cwd=main).strip()
        run(["git", "worktree", "add", wt, *(["-b"] if not local else []), branch,
             *([f"origin/{branch}"] if not local else [])], cwd=main)
    if run(["git", "status", "--porcelain"], cwd=wt).strip():
        raise BlockedError(f"worktree {wt} has uncommitted changes")
    if run(["git", "rev-parse", "HEAD"], cwd=wt).strip() != obs["head"]:
        run(["git", "merge", "--ff-only", f"origin/{branch}"], cwd=wt)
    return wt


def llm(engine, wt, prompt, schema, edit):
    """One judgement call inside the worktree; returns its parsed JSON answer."""
    prompt += "\n\nEnd your answer with a single JSON object matching:\n" + json.dumps(schema)
    if engine == "codex":
        with tempfile.NamedTemporaryFile("r", suffix=".txt") as out:
            run(["codex", "exec", "-C", wt, "-s", "workspace-write" if edit else "read-only",
                 "-o", out.name, "-"], stdin=prompt, timeout=1800)
            return parse_llm_json(Path(out.name).read_text(), schema)
    res = json.loads(run(["claude", "-p", "--output-format", "json",
                          "--json-schema", json.dumps(schema),
                          "--allowedTools", *(EDIT_TOOLS if edit else READ_TOOLS),
                          "--disallowedTools", *DENY_TOOLS, "--", prompt],
                         cwd=wt, timeout=1800, stdin=""))
    return parse_llm_json(res.get("structured_output") or res.get("result", ""), schema)


def checks_for(wt):
    """The repo's own lint and test commands: make targets when it has them."""
    mk = Path(wt) / "Makefile"
    targets = re.findall(r"^(lint|test):", mk.read_text(), re.MULTILINE) if mk.exists() else []
    return [["make", t] for t in ("lint", "test") if t in targets]


def verify_and_commit(wt, verify, subject, body):
    """Commit the LLM's edit only if every verify command passes; else stash it and block."""
    message = f"{short_subject(subject)}\n\n{body.strip()}"
    if not run(["git", "status", "--porcelain"], cwd=wt).strip():
        raise BlockedError(f"LLM reported a fix but left the tree unchanged ({message})")
    for cmd in verify:
        p = subprocess.run(cmd, cwd=wt, capture_output=True, text=True, timeout=3600, check=False)
        if p.returncode:
            run(["git", "stash", "push", "-u", "-m", f"pr-loop rejected: {message}"], cwd=wt)
            raise BlockedError(f"`{' '.join(cmd)}` failed after the fix (edit stashed in {wt}):\n"
                          f"{(p.stdout + p.stderr)[-1500:]}")
    run(["git", "add", "-A"], cwd=wt)
    run(["git", "commit", "-m", f"{message}\n\n{SIGNATURE}"], cwd=wt)
    return run(["git", "rev-parse", "HEAD"], cwd=wt).strip()


def failing_log(obs, repo):
    parts = []
    for c in obs["checks"]:
        if c.get("bucket") not in FAILED:
            continue
        job = re.search(r"/job/(\d+)", c.get("link") or "")
        log = run(["gh", "run", "view", "-R", repo, "--job", job[1], "--log-failed"],
                  check=False) if job else ""
        parts.append(f"### {c['name']} ({c.get('link')})\n" +
                     "\n".join(log.splitlines()[-150:]))
    return "\n\n".join(parts)


def do_fix_ci(ctx, obs, rnd):
    ctx.st["ci_attempts"] += 1
    prompt = (f"CI is failing on this pull request. Failing job log tails:\n\n"
              f"{failing_log(obs, ctx.repo)}\n\nMake the smallest change that fixes it, then "
              f"check it with {ctx.checks_text()}. Do not commit or push. If it cannot be fixed "
              f"here (flaky infra, secrets, needs a human), answer status blocked.")
    ans = llm(ctx.engine, ctx.wt, prompt, RESULT_SCHEMA, edit=True)
    rnd["fix_ci"] = ans
    if ans["status"] != "fixed":
        raise BlockedError(f"fix_ci: {ans['summary']}")
    rnd["fix_ci"]["sha"] = verify_and_commit(ctx.wt, ctx.verify, f"Fix CI: {ans['summary']}",
                                             ans["summary"])


def thread_prompt(t, wt):
    c = t["comments"]["nodes"]
    f = Path(wt) / t["path"]
    body = f.read_text(errors="replace")[:40000] if f.is_file() else "(file not in head)"
    convo = "\n\n".join(f"{x['author']['login'] if x['author'] else '?'}: {x['body']}"
                        for x in c)
    return (f"Review thread on {t['path']} line {t['line']}.\n\nDiff hunk:\n{c[0]['diffHunk']}"
            f"\n\nConversation:\n{convo}\n\nCurrent file:\n{body}")


def do_triage(ctx, obs, rnd):
    for t in obs["threads"]:
        ans = llm(ctx.engine, ctx.wt, thread_prompt(t, ctx.wt) + (
            "\n\nDo not edit anything: only decide. `fix` if the comment is right and a "
            "code change addresses it, `reply` to answer a question or explain, `decline` "
            "if it is wrong or out of scope. reply_text is what we post on the thread "
            "(one short paragraph)."),
            TRIAGE_SCHEMA, edit=False)
        entry = {**ans, "path": t["path"], "comment_id": t["comments"]["nodes"][0]["databaseId"],
                 "last_comment": t["comments"]["nodes"][-1]["databaseId"], "step": "triaged"}
        ctx.st["threads"][t["id"]] = entry
        rnd.setdefault("triage", []).append(t["id"])
        ctx.save()
        if ans["action"] == "fix":
            res = llm(ctx.engine, ctx.wt, thread_prompt(t, ctx.wt) +
                      f"\n\nApply this review fix: {ans['rationale']}\nMake the smallest "
                      f"change, check it with {ctx.checks_text()}, do not commit or push.",
                      RESULT_SCHEMA, edit=True)
            if res["status"] != "fixed":
                entry["step"] = "blocked"
                raise BlockedError(f"fix for thread {t['id']}: {res['summary']}")
            entry["sha"] = verify_and_commit(ctx.wt, ctx.verify, f"Review: {res['summary']}",
                                             res["summary"])
            entry["step"] = "committed"
            ctx.save()


def finish(ctx):
    """Push committed work, then reply and resolve. Each step is recorded, so a rerun resumes."""
    branch = ctx.branch
    ahead = run(["git", "rev-list", f"origin/{branch}..HEAD"], cwd=ctx.wt).strip()
    if ahead:
        run(["git", "push", f"https://github.com/{ctx.repo}.git", f"HEAD:refs/heads/{branch}"],
            cwd=ctx.wt)
        run(["git", "fetch", f"https://github.com/{ctx.repo}.git",
             f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], cwd=ctx.wt)
    for tid, t in ctx.st["threads"].items():
        if t["step"] in ("triaged", "committed") and (t["action"] != "fix" or t.get("sha")):
            run(["gh", "api", f"repos/{ctx.repo}/pulls/{ctx.pr}/comments/{t['comment_id']}/replies",
                 "-f", f"body={reply_body(t['reply_text'], t.get('sha'))}"])
            t["step"] = "replied"
            ctx.save()
        if t["step"] == "replied" and t["action"] == "fix":
            run(["gh", "api", "graphql", "-f", f"query={RESOLVE_Q}", "-f", f"id={tid}"])
            t["step"] = "resolved"
            ctx.save()
    return bool(ahead)


FACTS_Q = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(
number:$n){title body url state isDraft mergeable additions deletions baseRefName headRefOid
commits(last:100){nodes{commit{oid}}} reviews(last:50){nodes{author{login} commit{oid}}}
reviewThreads(first:100){nodes{id isResolved path line comments(first:50){nodes{
author{login} body url}}}}}}}"""


def make_recipe(wt, target):
    """The command lines of a Makefile target, to tell which of them the verify gate ran."""
    mk = Path(wt) / "Makefile"
    m = re.search(rf"^{target}:.*\n((?:\t.*\n?)*)", mk.read_text() if mk.exists() else "",
                  re.MULTILINE)
    return [ln.strip().lstrip("@-") for ln in m[1].splitlines()] if m else []


def gather(repo, pr, st, wt):
    """What GitHub and the worktree say now, for the decision page. Read-only."""
    owner, name = repo.split("/")
    raw = {"pr": gh_json("api", "graphql", "-f", f"query={FACTS_Q}", "-f", f"o={owner}",
                         "-f", f"r={name}", "-F", f"n={pr}")["data"]["repository"]["pullRequest"],
           "checks": json.loads(run(["gh", "pr", "checks", str(pr), "-R", repo, "--json",
                                     "name,bucket,link"], check=False) or "[]"),
           "commits": {}}
    shas = [t["sha"] for t in st["threads"].values() if t.get("sha")]
    shas += [r["fix_ci"]["sha"] for r in st["rounds"] if (r.get("fix_ci") or {}).get("sha")]
    for sha in shas:
        c = gh_json("api", f"repos/{repo}/commits/{sha}")
        raw["commits"][sha] = {"subject": c["commit"]["message"].splitlines()[0],
                               "url": c["html_url"],
                               "files": [{"name": f["filename"], "add": f["additions"],
                                          "del": f["deletions"]} for f in c.get("files", [])]}
    if wt and Path(wt).exists():
        raw["recipe"] = make_recipe(wt, "test")
        raw["workflows"] = "\n".join(p.read_text() for p in
                                     sorted(Path(wt).glob(".github/workflows/*.y*ml")))
        raw["stash"] = [ln for ln in run(["git", "stash", "list"], cwd=wt, check=False)
                        .splitlines() if "pr-loop" in ln]
    return raw


# ── The gate's page: one self-contained HTML file, no scripts, no network ──────────────────

BRAND_DIR = Path.home() / ".config/agents/brand"  # $PR_LOOP_BRAND_DIR; not in this repo
FONTS = [("Beausite Classic", "beausite-classic", (500, 600)),
         ("Suisse Intl", "suisse-intl", (400, 500, 600)),
         ("Matter Mono", "matter-mono", (400, 500))]


def brand_kit(base=None):
    """Fonts and wordmark from the local brand kit, embedded; system fonts if it is absent."""
    base = Path(base or os.environ.get("PR_LOOP_BRAND_DIR") or BRAND_DIR)
    css = []
    for family, stem, weights in FONTS:
        for w in weights:
            f = base / "fonts" / f"{stem}-{w}.woff2"
            if f.is_file():
                b64 = base64.b64encode(f.read_bytes()).decode()
                css.append(f'@font-face{{font-family:"{family}";font-weight:{w};'
                           f'font-display:swap;src:url(data:font/woff2;base64,{b64}) '
                           f'format("woff2")}}')
    mark = base / "qs-wordmark-cloud.svg"
    svg = mark.read_text() if mark.is_file() else ""
    svg = svg.replace("<svg ", '<svg class="logo" role="img" aria-label="QueryStory" ', 1)
    return {"fonts": "\n".join(css), "wordmark": svg}


def inline(text):
    """Escaped text with `code` and **bold** spans: enough for review comments and PR bodies."""
    out = html.escape(text or "", quote=False)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    return re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", out)


def link(url, text):
    return f'<a href="{html.escape(url)}">{text}</a>' if url else text


PAGE_CSS = """
:root{--midnight:#0e1234;--gold:#c28311;--gold-hi:#e0a93a;--gold-deep:#835f21;--pale:#e2cda3;
--slate:#1f2433;--lift:#b3cdff;--canvas:#f2f0eb;--card:#fff;--ink:#0e1234;--ink2:#3c4256;
--muted:#6b7085;--rule:rgba(14,18,52,.09);--rule2:rgba(14,18,52,.16);--chip:#f6f3ec;
--track:#ebe6da;--good:#2f8a5b;--warn:#b5760c;--bad:#c2452d;--node:#fff;
--nodes:rgba(14,18,52,.22);--arrow:#8a8fa3;
--fh:"Beausite Classic",ui-sans-serif,system-ui,sans-serif;
--fb:"Suisse Intl",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
--fm:"Matter Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme:dark){:root{--canvas:#0a0d24;--card:#131838;--ink:#eef0f7;
--ink2:#c3c7d6;--muted:#8d93ab;--rule:rgba(179,205,255,.10);--rule2:rgba(179,205,255,.18);
--chip:#1b2147;--track:#1f2650;--good:#5cc08b;--warn:#e0a93a;--bad:#f07a62;--node:#1a2046;
--nodes:rgba(179,205,255,.28);--arrow:#7d84a3}}
*{box-sizing:border-box}
body{margin:0;background:var(--canvas);color:var(--ink);font:400 16px/1.6 var(--fb);
overflow-x:hidden}
code{font-family:var(--fm);font-size:.84em;background:var(--chip);padding:.1em .38em;
border-radius:5px;overflow-wrap:anywhere}
a{color:inherit;text-decoration-color:var(--gold);text-underline-offset:3px}
.hero{background:var(--midnight);color:#fff;position:relative;overflow:hidden}
.hero:before{content:"";position:absolute;inset:0;background-image:radial-gradient(
rgba(226,205,163,.16) 1px,transparent 1.4px);background-size:22px 22px;
-webkit-mask-image:linear-gradient(180deg,#000,rgba(0,0,0,.5) 70%,transparent);
mask-image:linear-gradient(180deg,#000,rgba(0,0,0,.5) 70%,transparent)}
.hero:after{content:"";position:absolute;right:-180px;top:-200px;width:620px;height:620px;
border-radius:50%;background:radial-gradient(circle,rgba(224,169,58,.18),transparent 65%)}
.hin{position:relative;z-index:1;max-width:1100px;margin:0 auto;padding:28px 24px 52px}
.logo{display:block;height:28px;width:auto}
.brand{display:flex;align-items:center;gap:14px;font:500 12px/1 var(--fm);
letter-spacing:.16em;text-transform:uppercase;color:var(--pale)}
.eyebrow{margin:48px 0 14px;font:500 12px/1.5 var(--fm);letter-spacing:.16em;
text-transform:uppercase;color:var(--pale)}
.eyebrow a{text-decoration:none}
h1{font:500 clamp(40px,6.4vw,72px)/1.02 var(--fh);letter-spacing:-.025em;margin:0 0 16px}
h1 em{font-style:normal;background:linear-gradient(90deg,var(--gold-hi),var(--gold));
-webkit-background-clip:text;background-clip:text;color:transparent}
.ptitle{font:500 clamp(19px,2.2vw,24px)/1.3 var(--fh);margin:0 0 10px;max-width:46ch}
.lede{font-size:16.5px;line-height:1.55;color:rgba(255,255,255,.76);max-width:70ch;margin:0}
.hero code{background:rgba(179,205,255,.12);color:#fff}
.decide{display:grid;grid-template-columns:1.25fr 1fr;gap:14px;margin-top:32px}
.panel{border-radius:16px;padding:22px 24px;border:1px solid rgba(179,205,255,.16);
background:rgba(31,36,80,.55)}
.panel .k{font:500 10.5px/1 var(--fm);letter-spacing:.16em;text-transform:uppercase;
color:var(--lift);display:block;margin-bottom:12px}
.verdict{font:600 30px/1.1 var(--fh);margin:0 0 6px}
.verdict.Merge{color:#8fe0b4}.verdict.Hold{color:#ffb4a3}
.conf{display:inline-block;font:500 11px/1 var(--fm);letter-spacing:.12em;
text-transform:uppercase;padding:6px 9px;border-radius:99px;margin-left:10px;
vertical-align:6px;background:rgba(226,205,163,.16);color:var(--pale)}
ul.ev{list-style:none;margin:14px 0 0;padding:0;font-size:14.5px;
color:rgba(255,255,255,.84)}
ul.ev li{padding:4px 0 4px 26px;position:relative}
ul.ev li:before{position:absolute;left:0;top:4px;font:600 13px/1.6 var(--fm)}
ul.ev li.ok:before{content:"✓";color:#8fe0b4}ul.ev li.no:before{content:"✕";color:#ffb4a3}
ul.ev li.warn:before{content:"!";color:var(--gold-hi);left:4px}
.act{background:linear-gradient(160deg,rgba(224,169,58,.22),rgba(194,131,17,.10));
border-color:rgba(224,169,58,.45)}
.act ol{margin:0;padding-left:20px;font-size:15px;color:rgba(255,255,255,.88)}
.act li{margin:0 0 12px}.act li:last-child{margin:0}
.cmd{display:block;margin-top:6px;font:500 14px/1.5 var(--fm);background:rgba(14,18,52,.65);
border:1px solid rgba(226,205,163,.3);border-radius:10px;padding:9px 12px;color:#fff;
overflow-wrap:anywhere}
.act p{font-size:13px;color:rgba(255,255,255,.6);margin:14px 0 0}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:14px}
.stat{background:rgba(31,36,80,.55);border:1px solid rgba(179,205,255,.16);border-radius:14px;
padding:16px 18px}
.stat .k{font:500 10px/1 var(--fm);letter-spacing:.16em;text-transform:uppercase;
color:var(--lift);display:block;margin-bottom:10px}
.stat b{display:block;font:500 clamp(26px,3.4vw,38px)/1 var(--fh);letter-spacing:-.02em;
font-variant-numeric:tabular-nums}
.stat b small{font-size:.5em;color:var(--pale);margin-left:3px}
.stat span{display:block;margin-top:8px;font-size:13px;color:rgba(255,255,255,.64)}
.wrap{max-width:1100px;margin:0 auto;padding:0 24px 80px}
section{padding-top:56px}
.sh{display:flex;align-items:baseline;gap:14px;margin:0 0 8px}
.sh .n{font:500 15px/1 var(--fm);color:var(--gold)}
h2{font:500 clamp(24px,3vw,32px)/1.15 var(--fh);letter-spacing:-.015em;margin:0}
.intro{color:var(--ink2);max-width:72ch;margin:4px 0 22px}
.card{background:var(--card);border:1px solid var(--rule);border-radius:16px;padding:22px 24px;
margin:0 0 16px;box-shadow:0 1px 0 rgba(14,18,52,.03),0 8px 28px -18px rgba(14,18,52,.18)}
.lab{font:500 10.5px/1.4 var(--fm);letter-spacing:.16em;text-transform:uppercase;
color:var(--muted);margin:0 0 14px}
.grid2{display:grid;grid-template-columns:1.7fr 1fr;gap:16px}
.grid2 .card{margin:0}
.dg{overflow-x:auto;margin:0 -6px}
.dg svg{display:block;width:100%;min-width:720px;height:auto}
.ch svg{display:block;width:100%;max-width:440px;height:auto;margin:8px auto 0}
svg text{font-family:var(--fb);fill:var(--ink)}
svg .nd{fill:var(--node);stroke:var(--nodes);stroke-width:1.2}
svg .nd.hl{stroke:var(--gold);stroke-width:2}
svg .nd.dim{opacity:.55;stroke-dasharray:4 4}
svg .nd.dk{fill:var(--midnight);stroke:var(--gold);stroke-width:2}
svg .t1{font-size:14px;font-weight:600}
svg .t2{font-size:11.5px;fill:var(--muted)}
svg .t2.g{fill:var(--gold);font-weight:600}
svg .dk1{fill:#fff;font-size:14px;font-weight:600}
svg .dk2{fill:var(--pale);font-size:11.5px}
svg .ln{stroke:var(--arrow);stroke-width:1.5;fill:none}
svg .ln.g{stroke:var(--gold);stroke-width:2.2}
svg .ln.dim{stroke-dasharray:4 5;opacity:.6}
svg .ah{fill:var(--arrow)} svg .ah.g{fill:var(--gold)}
svg .al{font-size:11.5px;fill:var(--muted)} svg .al.g{fill:var(--gold);font-weight:600}
svg .bar{fill:url(#gold)} svg .base{stroke:var(--rule2);stroke-width:1}
svg .bv{font:600 20px var(--fh)} svg .bx{font-size:12.5px;font-weight:600}
svg .bh{font:500 10.5px var(--fm);fill:var(--muted)}
svg .zero{fill:none;stroke:var(--good);stroke-width:2}
svg .zt{fill:var(--good);font:600 13px var(--fm)}
.legend{display:flex;flex-wrap:wrap;gap:16px;font-size:12.5px;color:var(--muted);margin-top:8px}
.legend i{display:inline-block;width:18px;height:3px;border-radius:2px;margin-right:6px;
vertical-align:3px;background:var(--gold)}
.legend i.o{background:var(--arrow);opacity:.6}
.th{display:grid;gap:12px}
.tc{background:var(--card);border:1px solid var(--rule);border-radius:16px;padding:18px 22px;
position:relative}
.tc:before{content:"";position:absolute;left:0;top:18px;bottom:18px;width:3px;
border-radius:0 3px 3px 0;background:linear-gradient(180deg,var(--gold-hi),var(--gold))}
.tags{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px}
.tag{font:500 10.5px/1 var(--fm);letter-spacing:.1em;text-transform:uppercase;padding:5px 8px;
border-radius:6px;background:var(--chip);color:var(--ink2);border:1px solid var(--rule)}
.tag.bug{background:linear-gradient(90deg,var(--gold-hi),var(--gold));color:#fff;
border-color:transparent}
.tag.q{background:var(--lift);color:var(--midnight);border-color:transparent}
.tag.ok{color:var(--good)} .tag.warn{color:var(--warn)} .tag.no{color:var(--bad)}
.tag.n{margin-left:auto;background:none;border:0;color:var(--muted)}
.tc h3{font:600 17px/1.4 var(--fh);margin:0 0 8px;letter-spacing:-.005em}
.fx{font-size:14.5px;color:var(--ink2);margin:0 0 8px}
.fx b{font:500 10.5px var(--fm);letter-spacing:.12em;text-transform:uppercase;
color:var(--muted);margin-right:6px}
.meta{display:flex;flex-wrap:wrap;gap:6px 18px;font:500 12.5px/1.5 var(--fm);color:var(--muted)}
.meta a{color:var(--ink2)}
details{margin-top:10px;font-size:14px;color:var(--ink2)}
summary{cursor:pointer;font:500 11px var(--fm);letter-spacing:.12em;text-transform:uppercase;
color:var(--gold)}
.quote{border-left:3px solid var(--rule2);padding:2px 0 2px 14px;margin:10px 0;
white-space:pre-wrap;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;font-size:13.5px;font-variant-numeric:tabular-nums}
th{font:500 10px/1.4 var(--fm);letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
text-align:left;padding:8px 10px;border-bottom:1px solid var(--rule2)}
td{padding:8px 10px;border-bottom:1px solid var(--rule);vertical-align:top}
tr:last-child td{border-bottom:0}
td.num{text-align:right;white-space:nowrap;font-family:var(--fm)}
.add{color:var(--good)} .del{color:var(--bad)}
.cav{border-radius:16px;padding:20px 24px;background:var(--chip);border:1px solid var(--rule)}
.cav ul{margin:0;padding:0;list-style:none}
.cav li{position:relative;padding:9px 0 9px 24px;border-bottom:1px solid var(--rule);
color:var(--ink2);font-size:15px}
.cav li:last-child{border-bottom:0}
.cav li:before{content:"";position:absolute;left:2px;top:18px;width:8px;height:8px;
border-radius:2px;background:linear-gradient(135deg,var(--gold-hi),var(--gold))}
.tw{overflow-x:auto}
.ctx{margin:18px 0 0;padding:12px 16px;border-left:3px solid var(--gold);max-width:70ch;
background:rgba(226,205,163,.07);border-radius:0 10px 10px 0;font-size:15px;
color:rgba(255,255,255,.86)}
.grid2{margin-top:16px}
ol.rounds{list-style:none;margin:0;padding:0}
ol.rounds li{padding:10px 0;border-bottom:1px solid var(--rule);font-size:14.5px;color:var(--ink2)}
ol.rounds li:last-child{border-bottom:0}
ol.rounds b{display:block;font:600 15px/1.4 var(--fh);color:var(--ink)}
ol.rounds b span{font:500 11px var(--fm);color:var(--muted);margin-left:8px}
ul.runs{list-style:none;margin:0;padding:0}
ul.runs li{margin:0 0 8px}
ul.runs code{display:block;padding:8px 10px;white-space:pre-wrap}
footer{max-width:1100px;margin:0 auto;padding:0 24px 40px;font:500 11px/1.6 var(--fm);
letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
@media (max-width:900px){.decide,.grid2{grid-template-columns:1fr}
.stats{grid-template-columns:1fr 1fr}}
@media (max-width:640px){.hin{padding:22px 16px 40px}.wrap,footer{padding-left:16px;
padding-right:16px}.eyebrow{margin-top:36px}.panel,.card,.tc{padding:18px 16px}
.stats{gap:8px}.stat{padding:14px}.tag.n{margin-left:0}}
"""


def node(x, y, w, h, title, sub, cls="", sub_cls=""):
    dark = "dk" in cls
    return (f'<rect class="nd {cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>'
            f'<text class="{"dk1" if dark else "t1"}" x="{x + w / 2}" y="{y + h / 2 - 3}" '
            f'text-anchor="middle">{html.escape(title)}</text>'
            f'<text class="{"dk2" if dark else "t2 " + sub_cls}" x="{x + w / 2}" '
            f'y="{y + h / 2 + 15}" text-anchor="middle">{html.escape(sub)}</text>')


def arrow(path, on, end, label="", at=(0, 0)):
    """A connector; `end` is (x, y, direction) of its arrowhead tip."""
    g = " g" if on else " dim"
    x, y, dr = end
    tip = {"r": f"{x},{y} {x - 8},{y - 5} {x - 8},{y + 5}",
           "d": f"{x},{y} {x - 5},{y - 8} {x + 5},{y - 8}",
           "u": f"{x},{y} {x - 5},{y + 8} {x + 5},{y + 8}"}[dr]
    lab = (f'<text class="al{" g" if on else ""}" x="{at[0]}" y="{at[1]}" text-anchor="middle">'
           f"{html.escape(label)}</text>") if label else ""
    return (f'<path class="ln{g}" d="{path}"/>'
            f'<polygon class="ah{" g" if on else ""}" points="{tip}"/>{lab}')


def loop_svg(m):
    """The graph, with the path this run took in gold and its counts on the nodes."""
    s, w, h, y = m["stats"], 128, 64, 96
    xs = [10 + i * 146 for i in range(7)]
    fixed, replies = s["fixed"] > 0, s["replies"] > 0
    counts = " → ".join(str(p["n"]) for p in m["series"]) or "0"
    ci_sub = (f"{s['ci_pass']}/{s['ci_total']} passed" if s["ci_total"] else m["ci"])
    nodes = [("Preflight", "worktree · lock", True), ("CI", ci_sub, True),
             ("Copilot review", plural(m["copilot_reviews"], "review"), m["copilot_reviews"] > 0),
             ("Triage", f"threads {counts}", bool(m["series"])),
             ("Fix", f"{plural(s['fixed'], 'fix')}", fixed),
             ("Verify gate", f"{plural(len(m['verify']) or '?', 'local check')}", fixed),
             ("Push · reply", f"{s['resolved']}/{len(m['cards'])} resolved", replies)]
    out = [node(x, y, w, h, t, sub, "hl" if on else "dim") for x, (t, sub, on) in
           zip(xs, nodes, strict=True)]
    for i in range(6):
        on = nodes[i][2] and nodes[i + 1][2]
        out.append(arrow(f"M{xs[i] + w} {y + h / 2}H{xs[i + 1] - 2}", on,
                         (xs[i + 1] - 1, y + h / 2, "r")))
    loops = max(s["rounds"] - 1, 0) + (1 if replies else 0)
    out.append(arrow(f"M{xs[6] + w / 2} {y}V52Q{xs[6] + w / 2} 40 {xs[6] + w / 2 - 12} 40"
                     f"H{xs[1] + w / 2 + 12}Q{xs[1] + w / 2} 40 {xs[1] + w / 2} 52V{y - 2}",
                     loops > 0, (xs[1] + w / 2, y - 1, "d"),
                     f"loop: new head, CI and Copilot again ×{loops}",
                     ((xs[1] + xs[6] + w) / 2, 32)))
    ci_on = s["ci_attempts"] > 0
    out.append(node(xs[1], 216, w, 56, "Fix CI", f"{plural(s['ci_attempts'], 'attempt')}",
                    "hl" if ci_on else "dim"))
    out.append(arrow(f"M{xs[1] + w / 2} {y + h}V214", ci_on, (xs[1] + w / 2, 214, "d"),
                     "CI red", (xs[1] + w / 2 + 30, 196)))
    out.append(arrow(f"M{xs[1] + w / 2} 272V300H{xs[5] + w / 2}V{y + h + 2}", ci_on,
                     (xs[5] + w / 2, y + h + 1, "u")))
    gx = xs[3] - 20
    out.append(node(gx, 216, w + 40, 56, "Human gate", "you are here", "dk"))
    out.append(arrow(f"M{xs[3] + w / 2} {y + h}V214", on=True, end=(xs[3] + w / 2, 214, "d"),
                     label="0 threads, CI green", at=(xs[3] + w / 2 + 70, 196)))
    return (f'<svg viewBox="0 0 1050 316" role="img" aria-label="The pr-loop graph, with this '
            f'run\'s path highlighted">{"".join(out)}</svg>')


def rounds_svg(series):
    """Unresolved review threads each round found, converging to zero."""
    if not series:
        return '<p class="intro">No review rounds ran.</p>'
    top = max(p["n"] for p in series) or 1
    bw, gap, base, hgt = 70, 46, 170, 120
    width = 40 + len(series) * (bw + gap)
    out = [('<defs><linearGradient id="gold" x1="0" y1="0" x2="0" y2="1"><stop offset="0" '
            'stop-color="#e0a93a"/><stop offset="1" stop-color="#c28311"/></linearGradient>'
            "</defs>"),
           f'<line class="base" x1="10" y1="{base}" x2="{width - 10}" y2="{base}"/>']
    for i, p in enumerate(series):
        x = 30 + i * (bw + gap)
        cx = x + bw / 2
        if p["n"]:
            bh = hgt * p["n"] / top
            out.append(f'<rect class="bar" x="{x}" y="{base - bh}" width="{bw}" height="{bh}" '
                       f'rx="6"/><text class="bv" x="{cx}" y="{base - bh - 8}" '
                       f'text-anchor="middle">{p["n"]}</text>')
        else:
            out.append(f'<circle class="zero" cx="{cx}" cy="{base - 16}" r="14"/><text class="zt" '
                       f'x="{cx}" y="{base - 11.5}" text-anchor="middle">0</text>')
        out.append(f'<text class="bx" x="{cx}" y="{base + 20}" text-anchor="middle">'
                   f'{html.escape(p["label"])}</text><text class="bh" x="{cx}" y="{base + 36}" '
                   f'text-anchor="middle">@{html.escape(p["head"])}</text>')
    return (f'<svg viewBox="0 0 {width} 216" role="img" aria-label="Review threads per round: '
            f'{", ".join(str(p["n"]) for p in series)}">{"".join(out)}</svg>')


def context_line(m):
    s = m["stats"]
    did = (f"fixed {plural(s['fixed'], 'review thread')} in {plural(s['rounds'], 'round')}"
           if s["fixed"] else f"ran {plural(s['rounds'], 'round')}")
    return (f"pr-loop drove this PR through CI and Copilot review, {did}, and stopped here: "
            "merging is a human decision.")


def timeline(m):
    """One line per round: its head, the threads it triaged and the commits that fixed them."""
    out = []
    for p in m["series"]:
        n = int(p["label"].split()[-1]) if p["label"].startswith("Round") else None
        cards = [x for x in m["cards"] if n is not None and x["round"] == n]
        shas = ", ".join(link(x["commit_url"], f"<code>{x['sha'][:8]}</code>")
                         for x in cards if x["sha"])
        if n is None:
            body = ("Copilot found nothing new." if not p["n"]
                    else f"{plural(p['n'], 'thread')} still open.")
        else:
            acts = ", ".join(f"{sum(x['action'] == a for x in cards)} {v}"
                             for a, v in (("fix", "to fix"), ("reply", "to answer"),
                                          ("decline", "declined"))
                             if any(x["action"] == a for x in cards))
            body = (f"{plural(p['n'], 'thread')} triaged: {acts}."
                    + (f" Fixed in {shas}." if shas else ""))
        out.append(f"<li><b>{html.escape(p['label'])}<span>@{html.escape(p['head'])}</span></b>"
                   f"{body}</li>")
    return "".join(out) or "<li>No review rounds ran.</li>"


def thread_card(x):
    kind = {"fix": ("Bug", "bug"), "reply": ("Question", "q"), "decline": ("Declined", "")}
    label, cls = kind.get(x["action"], (x["action"], ""))
    res = {True: ("Resolved", "ok"), False: ("Open", "warn")}.get(x["resolved"], ("Unknown", ""))
    tags = [f'<span class="tag {cls}">{label}</span>',
            f'<span class="tag {res[1]}">{res[0]}</span>']
    if x["action"] == "fix":
        tags.append(f'<span class="tag ok">+{x["tests"]} test lines</span>' if x["tests"]
                    else '<span class="tag warn">no test</span>')
    tags.append(f'<span class="tag n">{"round " + str(x["round"]) if x["round"] else ""}</span>')
    where = f"{x['path']}:{x['line']}" if x["line"] else x["path"]
    meta = [f"<span>{html.escape(where)}</span>"]
    if x["sha"]:
        meta.append(f"<span>commit {link(x['commit_url'], x['sha'][:8])}"
                    f' <span class="add">+{x["adds"]}</span> <span class="del">−{x["dels"]}</span>'
                    "</span>")
    meta += [f"<span>{link(x['thread_url'], 'review thread')}</span>",
             f"<span>{link(x['reply_url'], 'our reply') if x['reply_url'] else 'no reply'}</span>"]
    rows = "".join(f'<tr><td><code>{html.escape(f["name"])}</code></td><td class="num add">'
                   f'+{f["add"]}</td><td class="num del">−{f["del"]}</td></tr>'
                   for f in x["files"])
    fix = (f'<p class="fx"><b>Fix</b>{inline(x["subject"])}</p>' if x["subject"] else "")
    files = (f'<div class="tw"><table><tr><th>File</th><th class="num">Added</th>'
             f'<th class="num">Removed</th></tr>{rows}</table></div>' if rows else "")
    return (f'<article class="tc"><div class="tags">{"".join(tags)}</div>'
            f'<h3>{inline(x["risk"])}</h3>{fix}<div class="meta">{"".join(meta)}</div>'
            f'<details><summary>Details</summary><p>{html.escape(x["reviewer"])} wrote:</p>'
            f'<div class="quote">{inline(x["comment"])}</div>{files}</details></article>')


def render_page(m, kit=None, when=""):
    kit = kit or {"fonts": "", "wordmark": ""}
    s, repo, pr = m["stats"], m["repo"], m["pr"]
    esc = html.escape
    ev = [f'<li class="{"ok" if ok else "no"}">{inline(t)}</li>' for ok, t in m["gates"]]
    ev += [f'<li class="warn">{inline(t[0].upper() + t[1:])}</li>' for t in m["soft"]]
    if m["verdict"] == "Merge":
        steps = (f'<li>Tell the agent<span class="cmd">merge {pr}</span></li>'
                 f'<li>or run it yourself<span class="cmd">gh pr merge {pr} --squash -R '
                 f'{esc(repo)}</span></li>')
        rec = "Merge it"
    else:
        steps = (f'<li>Fix what blocks it, or tell the agent<span class="cmd">rerun pr-loop {pr}'
                 f'</span></li><li>Look at the PR<span class="cmd">gh pr view {pr} --web -R '
                 f'{esc(repo)}</span></li>')
        rec = "Hold"
    stat = [("Rounds", s["rounds"], "", f"{plural(s['ci_attempts'], 'CI fix attempt')}"),
            ("Threads fixed", s["fixed"], f"/{len(m['cards'])}",
             f"{s['replied']} replied · {s['declined']} declined"),
            ("Commits", s["commits"], "", f"{s['resolved']} threads resolved"),
            ("CI", "green" if m["ci"] == "pass" else m["ci"], "",
             f"{s['ci_pass']} passed · {s['ci_skip']} skipped · {s['ci_fail']} failed"),
            ("Loop's diff", f"+{s['adds']}", f" −{s['dels']}",
             f"whole PR +{s['pr_adds']} −{s['pr_dels']}"),
            ("Tests added", f"+{s['tests']}", " lines",
             f"in {sum(1 for x in m['cards'] if x['tests'])} of {s['fixed']} fixes"),
            ("Copilot", m["copilot_reviews"], " reviews",
             "latest commit reviewed" if m["reviewed"] else "latest commit not reviewed"),
            ("Replies", s["replies"], f"/{len(m['cards'])}", "posted on GitHub")]
    stats = "".join(f'<div class="stat"><span class="k">{k}</span><b>{v}<small>{esc(u)}</small>'
                    f"</b><span>{esc(d)}</span></div>" for k, v, u, d in stat)
    tone = {"pass": "ok", "fail": "no", "cancel": "no", "pending": "warn"}
    checks = "".join(
        f'<tr><td>{link(c.get("link"), esc(c["name"]))}</td><td><span class="tag '
        f'{tone.get(c.get("bucket"), "")}">{esc(str(c.get("bucket")))}</span></td></tr>'
        for c in m["checks"])
    cav = "".join(f"<li>{inline(c)}</li>" for c in m["caveats"]) or "<li>Nothing.</li>"
    checks_ran = "".join(f"<li><code>{esc(v)}</code></li>" for v in m["verify"])
    wm = kit["wordmark"] or '<b style="font:600 20px var(--fh)">QueryStory</b>'
    title = f"Merge {repo.split('/')[1]} #{pr}?"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{kit["fonts"]}{PAGE_CSS}</style></head><body>
<header class="hero"><div class="hin">
<div class="brand">{wm}<span>pr-loop · human gate</span></div>
<p class="eyebrow">{link(m["url"], esc(f"{repo} #{pr}"))} · into {esc(m["base"])} · head
{esc(m["head"][:8])}</p>
<h1>Merge <em>#{pr}</em>?</h1>
<p class="ptitle">{inline(m["title"])}</p>
<p class="lede">{inline(m["why"])}</p>
<p class="ctx">{inline(context_line(m))}</p>
<div class="decide">
<div class="panel"><span class="k">The loop recommends</span>
<p class="verdict {m["verdict"]}">{rec}<span class="conf">{m["confidence"]} confidence</span></p>
<ul class="ev">{"".join(ev)}</ul></div>
<div class="panel act"><span class="k">How to act</span><ol>{steps}</ol>
<p>pr-loop never merges: the merge is yours. This page has no buttons on purpose.</p></div>
</div>
<div class="stats">{stats}</div>
</div></header>
<main class="wrap">
<section id="loop"><div class="sh"><span class="n">01</span><h2>What the loop did</h2></div>
<p class="intro">pr-loop walks this graph until CI is green, Copilot has reviewed the latest
commit and no review thread is left, then stops here for you. Code does the plumbing; a model
only triages a thread or writes a fix, and every fix must pass the verify gate before it is
committed.</p>
<div class="card"><p class="lab">The graph, this run's path in gold</p>
<div class="dg">{loop_svg(m)}</div><div class="legend"><span><i></i>taken this run</span>
<span><i class="o"></i>not needed</span></div></div>
<div class="grid2"><div class="card ch"><p class="lab">Review threads found per round</p>
{rounds_svg(m["series"])}</div>
<div class="card"><p class="lab">Round by round</p><ol class="rounds">{timeline(m)}</ol></div></div>
</section>
<section id="threads"><div class="sh"><span class="n">02</span><h2>Review threads</h2></div>
<p class="intro">One card per thread the loop handled. The headline is the reviewer's own
finding; the fix line is the commit's subject. Status is the thread's state on GitHub now.</p>
<div class="th">{"".join(thread_card(x) for x in m["cards"]) or "<p>No threads.</p>"}</div>
</section>
<section id="caveats"><div class="sh"><span class="n">03</span><h2>Not verified · caveats</h2>
</div><p class="intro">What this run did not check, or left behind. Only things it observed.
</p><div class="cav"><ul>{cav}</ul></div>
</section>
<section id="evidence"><div class="sh"><span class="n">04</span><h2>Evidence</h2></div>
<div class="grid2"><div class="card"><p class="lab">CI on {esc(m["head"][:8])}</p>
<div class="tw"><table><tr><th>Check</th><th>Result</th></tr>{checks}</table></div></div>
<div class="card"><p class="lab">Local checks every fix had to pass</p>
<ul class="runs">{checks_ran or "<li>Not recorded for this run.</li>"}</ul>
<p class="lab" style="margin-top:18px">Run outcome</p><p>{inline(m["outcome"])}</p></div></div>
</section></main>
<footer>Built by pr-loop from GitHub and the run's state{" · " + esc(when) if when else ""}
</footer></body></html>
"""


def gate(ctx, outcome):
    """The human node: a decision page on scratch, and a short summary on stdout."""
    verify = ([shlex.join(c) for c in ctx.verify] if ctx.verify else ctx.st.get("verify"))
    try:
        raw = gather(ctx.repo, ctx.pr, ctx.st, ctx.wt)
    except BlockedError as e:  # gh itself may be what blocked the run
        print(f"(page built from state only: {e})", file=sys.stderr)
        raw = {}
    m = decide(ctx.repo, ctx.pr, ctx.st, raw, verify, outcome)
    scratch = os.environ.get("TMUXRC_SCRATCH_DIR")
    where = ""
    if scratch and not ctx.dry:
        folder = Path(scratch) / f"pr-loop-{ctx.repo.replace('/', '-')}-{ctx.pr}"
        folder.mkdir(parents=True, exist_ok=True)
        when = run(["date", "-Iseconds"]).strip()
        meta = {"title": f"Merge {ctx.repo.split('/')[1]} #{ctx.pr}?",
                "description": meta_description(m),
                "pane": os.environ.get("TMUX_PANE", ""), "created": when}
        (folder / "meta.json").write_text(json.dumps(meta))  # first: the link shows "building"
        (folder / "index.html").write_text(render_page(m, brand_kit(), when))
        (folder / "index.md").unlink(missing_ok=True)  # an older run's page
        url = os.environ.get("TMUXRC_SCRATCH_URL")
        where = f"{url}/{folder.name}/" if url else str(folder)
    print("\n" + summary_text(m, where))
    return m


class Ctx:
    def __init__(self, args, repo):
        self.repo, self.pr, self.engine, self.dry = repo, args.pr, args.engine, args.dry_run
        self.path = state_path(repo, args.pr)
        self.st = load_state(self.path)
        self.wt = self.branch = self.lock = None
        self.obs = {}
        self.verify = []

    def checks_text(self):
        """The verify gate's own commands, so the model runs what will judge its fix."""
        return "; ".join(f"`{shlex.join(c)}`" for c in self.verify) or "the repo's lint and tests"

    def save(self):
        if not self.dry:
            save_state(self.path, self.st)


def plan(ctx, obs, action, reason):
    print(f"next: {action} ({reason})")
    if action == "triage":
        for t in obs["threads"]:
            c = t["comments"]["nodes"][-1]
            print(f"  would triage {t['id']} {t['path']}:{t['line']} "
                  f"[{c['author']['login'] if c['author'] else '?'}] {c['body'][:90]!r}")
        print("  then: fix -> LLM edit, verify, commit, push, reply, resolve; "
              "reply/decline -> post reply, leave open")
    elif action == "fix_ci":
        print(f"  would ask {ctx.engine} to fix: "
              f"{[c['name'] for c in obs['checks'] if c.get('bucket') in FAILED]}")
    elif action == "request_review":
        print(f"  would request {COPILOT}[bot] on {obs['head'][:8]}")
    print(f"  verify with: {ctx.verify}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pr", type=int)
    ap.add_argument("--repo")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--max-ci-attempts", type=int, default=2)
    ap.add_argument("--engine", choices=["claude", "codex"], default="claude")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds to wait on CI/review")
    ap.add_argument("--check", action="append", help="verify command run after each fix "
                    "(repeatable; default: the repo's `make lint` and `make test`)")
    ap.add_argument("--summary-only", action="store_true", help="rebuild the human gate's "
                    "page from the saved state and GitHub; changes nothing else")
    args = ap.parse_args(argv)
    repo = args.repo or run(["gh", "repo", "view", "--json", "nameWithOwner", "-q",
                             ".nameWithOwner"]).strip()
    ctx = Ctx(args, repo)
    if args.summary_only:
        return summarize(ctx, args)
    try:
        return drive(ctx, args)
    except BlockedError as e:
        ctx.st["status"] = f"BLOCKED: {e}"
        ctx.save()
        gate(ctx, f"BLOCKED: {e}")
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 2


def summarize(ctx, args):
    """The human gate again, for a finished run: no lock, no LLM, no push, state untouched."""
    if not ctx.path.exists():
        print(f"no saved run at {ctx.path}", file=sys.stderr)
        return 2
    obs = ctx.obs = observe(ctx.repo, ctx.pr, ctx.st)
    try:
        ctx.wt = worktrees(os.getcwd())[0].get(obs["branch"])
    except BlockedError:  # not run from a checkout of the repo: no worktree facts
        ctx.wt = None
    ctx.verify = [shlex.split(c) for c in args.check or []]
    status = ctx.st.get("status") or "no outcome recorded"
    gate(ctx, status if status.startswith("BLOCKED") else
         f"ready for your merge decision ({status})")
    return 0


def drive(ctx, args):
    """Walk the graph from preflight to the human gate; a gate raises BlockedError."""
    run(["gh", "auth", "status"])
    ctx.lock = None if ctx.dry else lock(ctx.path)  # held until the process exits
    obs = ctx.obs = observe(ctx.repo, ctx.pr, ctx.st)
    ctx.branch = obs["branch"]
    print(f"{ctx.repo}#{ctx.pr} {obs['branch']}@{obs['head'][:8]} ci={obs['ci']} "
          f"copilot_reviewed={obs['reviewed']} threads={len(obs['threads'])}")
    ctx.wt = ensure_worktree(ctx.repo, ctx.pr, obs, ctx.dry)
    ctx.verify = ([shlex.split(c) for c in args.check] if args.check else
                  checks_for(ctx.wt) if Path(ctx.wt).exists() else [])
    ctx.st["verify"] = [shlex.join(c) for c in ctx.verify]  # what the page says fixes passed
    if not ctx.dry and finish(ctx):  # resume: push/reply anything a crash left behind
        obs = ctx.obs = observe(ctx.repo, ctx.pr, ctx.st)
    waited = 0
    while True:
        action, reason = next_action(obs, ctx.st, args.max_rounds, args.max_ci_attempts)
        if ctx.dry:
            plan(ctx, obs, action, reason)
            return 0
        print(f"[{time.strftime('%H:%M:%S')}] {action}: {reason}", flush=True)
        if action == "blocked" or (action == "wait" and waited >= args.timeout):
            raise BlockedError(reason if action == "blocked" else f"timed out: {reason}")
        if action == "done":
            ctx.st["status"] = reason
            ctx.save()
            gate(ctx, f"ready for your merge decision ({reason})")
            return 0
        if action == "wait":
            time.sleep(POLL)
            waited += POLL
        elif action == "request_review":
            run(["gh", "api", f"repos/{ctx.repo}/pulls/{ctx.pr}/requested_reviewers", "-X",
                 "POST", "-f", f"reviewers[]={COPILOT}[bot]"])
            ctx.st["review_requested"] = obs["head"]
            ctx.save()
        else:
            rnd = {"n": len(ctx.st["rounds"]) + 1, "head": obs["head"], "action": action}
            ctx.st["rounds"].append(rnd)
            (do_fix_ci if action == "fix_ci" else do_triage)(ctx, obs, rnd)
            ctx.save()
            if finish(ctx):
                waited = 0
                time.sleep(POLL)  # let GitHub register the new head's checks
        obs = ctx.obs = observe(ctx.repo, ctx.pr, ctx.st)

if __name__ == "__main__":
    sys.exit(main())
