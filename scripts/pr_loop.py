"""Drive one pull request through CI and review until it is ready for a human merge decision.

The loop is a small graph: code nodes do the plumbing (gh, git, polling), an LLM is called
only for judgement (fix this CI failure, triage this review thread), gates stop the run
with a reason, and the last node is a human. It never merges. docs/design/pr-loop.md says
why it is shaped this way.

    uv run python scripts/pr_loop.py <PR> [--repo owner/name] [--dry-run] [--max-rounds N]
                                          [--engine claude|codex] [--check CMD]...
"""
from __future__ import annotations

import argparse
import fcntl
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
        if seen.get("last_comment") == comments[-1]["databaseId"] and seen.get("step") != "blocked":
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
    if obs["ci"] == "pending":
        return "wait", "CI pending"
    if not obs["reviewed"]:
        if st.get("review_requested") != obs["head"]:
            return "request_review", "Copilot has not reviewed the head commit"
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


def summary_md(repo, pr, st, obs, outcome):
    lines = [f"# {repo}#{pr}: {outcome}", "", f"<https://github.com/{repo}/pull/{pr}>", "",
             f"- Rounds: {len(st['rounds'])}", f"- CI: {obs.get('ci', '?')}",
             f"- Copilot reviewed head: {'yes' if obs.get('reviewed') else 'no'}",
             f"- CI fix attempts: {st['ci_attempts']}", "", "## Threads", ""]
    for t in st["threads"].values():
        sha = f" ({t['sha'][:8]})" if t.get("sha") else ""
        lines.append(f"- **{t['action']}** `{t['path']}`{sha}, {t['step']}: {t['rationale']}")
    if not st["threads"]:
        lines.append("- none")
    return "\n".join([*lines, "", "Merging is your call; pr_loop never merges.", ""])


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


def verify_and_commit(wt, verify, message):
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
              f"run the repo's lint and tests. Do not commit or push. If it cannot be fixed "
              f"here (flaky infra, secrets, needs a human), answer status blocked.")
    ans = llm(ctx.engine, ctx.wt, prompt, RESULT_SCHEMA, edit=True)
    rnd["fix_ci"] = ans
    if ans["status"] != "fixed":
        raise BlockedError(f"fix_ci: {ans['summary']}")
    rnd["fix_ci"]["sha"] = verify_and_commit(ctx.wt, ctx.verify,
                                             f"Fix CI: {ans['summary'][:60]}")


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
            "\n\nDecide: `fix` if the comment is right and a code change addresses it, "
            "`reply` to answer a question or explain, `decline` if it is wrong or out of "
            "scope. reply_text is what we post on the thread (one short paragraph)."),
            TRIAGE_SCHEMA, edit=False)
        entry = {**ans, "path": t["path"], "comment_id": t["comments"]["nodes"][0]["databaseId"],
                 "last_comment": t["comments"]["nodes"][-1]["databaseId"], "step": "triaged"}
        ctx.st["threads"][t["id"]] = entry
        rnd.setdefault("triage", []).append(t["id"])
        ctx.save()
        if ans["action"] == "fix":
            res = llm(ctx.engine, ctx.wt, thread_prompt(t, ctx.wt) +
                      f"\n\nApply this review fix: {ans['rationale']}\nMake the smallest "
                      "change, run the repo's lint and tests, do not commit or push.",
                      RESULT_SCHEMA, edit=True)
            if res["status"] != "fixed":
                entry["step"] = "blocked"
                raise BlockedError(f"fix for thread {t['id']}: {res['summary']}")
            entry["sha"] = verify_and_commit(ctx.wt, ctx.verify,
                                             f"Review: {res['summary'][:60]}")
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


def gate(ctx, obs, outcome):
    """The human node: a summary on stdout and, when there is one, a scratch page."""
    md = summary_md(ctx.repo, ctx.pr, ctx.st, obs, outcome)
    print("\n" + md)
    scratch = os.environ.get("TMUXRC_SCRATCH_DIR")
    if scratch and not ctx.dry:
        folder = Path(scratch) / f"pr-loop-{ctx.repo.replace('/', '-')}-{ctx.pr}"
        folder.mkdir(parents=True, exist_ok=True)
        meta = {"title": f"pr-loop {ctx.repo}#{ctx.pr}", "description": outcome,
                "pane": os.environ.get("TMUX_PANE", ""),
                "created": run(["date", "-Iseconds"]).strip()}
        (folder / "meta.json").write_text(json.dumps(meta))
        (folder / "index.md").write_text(md)
        url = os.environ.get("TMUXRC_SCRATCH_URL")
        print(f"summary: {url}/{folder.name}/" if url else f"summary: {folder}")


class Ctx:
    def __init__(self, args, repo):
        self.repo, self.pr, self.engine, self.dry = repo, args.pr, args.engine, args.dry_run
        self.path = state_path(repo, args.pr)
        self.st = load_state(self.path)
        self.wt = self.branch = self.lock = None
        self.obs = {}
        self.verify = []

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
    args = ap.parse_args(argv)
    repo = args.repo or run(["gh", "repo", "view", "--json", "nameWithOwner", "-q",
                             ".nameWithOwner"]).strip()
    ctx = Ctx(args, repo)
    try:
        return drive(ctx, args)
    except BlockedError as e:
        ctx.st["status"] = f"BLOCKED: {e}"
        ctx.save()
        gate(ctx, ctx.obs, f"BLOCKED: {e}")
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 2


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
            gate(ctx, obs, f"ready for your merge decision ({reason})")
            print("ready for your merge decision")
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
