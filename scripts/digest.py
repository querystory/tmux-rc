"""The daily and weekly digest pages on scratch, built by a timer outside the daemon.

Code gathers every fact, count, join and link. One model call groups the work into themes
and writes their outcome lines and the two verdicts. docs/design/digest.md says why.
"""
from __future__ import annotations

import html
import json
import os
import statistics
import subprocess
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

from openbus.config import dotenv_path
from openbus.pr_titles import run_gh
from openbus.repository import github_repository
from openbus.scratch_markdown import tokens

DAY = 86400
HERE = Path(__file__).parent
# Holds themes.json, the registry: plain JSON the user may edit (docs/design/digest.md).
STATE = Path.home() / ".local/share/tmux-rc/digest"
STAGES = {"main": "on main", "held": "on your machine", "ready": "approved, waiting",
          "open": "in progress", "stuck": "stuck", "dropped": "closed"}
STATES = {"stuck": "Stuck", "held": "Live on your machine, not on main",
          "open": "In progress", "main": "Live on main"}
NEEDS = {"rebase": ("needs a rebase", "it can't reach main until then"),
         "merge": ("can merge", "approved and green")}
SIZES = ((.6, "most"), (.45, "half"), (.3, "a third"), (.24, "a quarter"), (.17, "a fifth"),
         (.1, "an eighth"), (0, "small"))
# List $ per 1M tokens (input, output, cache read) by model family: effort is for comparing
# themes, not for paying. A cache write costs 1.25x input, 2x when kept for an hour.
PRICE = {"opus": (4, 20, .2), "sonnet": (2, 10, .1), "haiku": (.1, .5, .01), "fable": (8, 40, .2)}
FIELDS = """number title url state isDraft updatedAt mergedAt closedAt mergeable reviewDecision
headRefName headRefOid baseRefName bodyText
repository { nameWithOwner defaultBranchRef { name } }
commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
reviews(first: 40) { nodes { author { login } } }"""
FALLBACK = "The model could not be reached, so this page has no summary. The pictures are current."


def ts(stamp: str | None) -> float:
    return datetime.fromisoformat(stamp).timestamp() if stamp else 0.0


def search(query: str) -> list[dict]:
    """Every PR a GitHub search finds, a page at a time: one large request outlasts GitHub."""
    nodes, after = [], ""
    while after is not None:
        body = (f'query {{ s: search(type: ISSUE, first: 40, query: "{query}"{after}) {{ pageInfo '
                f"{{ hasNextPage endCursor }} nodes {{ ... on PullRequest {{ {FIELDS} }} }} }} }}")
        reply = run_gh(["api", "graphql", "-f", f"query={body}"], 90)
        found = json.loads(reply or "{}")["data"]["s"]
        nodes += [n for n in found["nodes"] if n]
        page = found["pageInfo"]
        after = f', after: "{page["endCursor"]}"' if page["hasNextPage"] else None
    return nodes


def change(n: dict, now: float, held: bool) -> dict:
    """One PR as the page needs it. Its stage, and whether it needs the user, are rules here."""
    commit = (n["commits"]["nodes"] or [{}])[0].get("commit") or {}
    checks = (commit.get("statusCheckRollup") or {}).get("state")
    green = (n["reviewDecision"], checks, n["mergeable"]) == ("APPROVED", "SUCCESS", "MERGEABLE")
    # Merged into another PR's branch is not on main yet: it rides with that PR.
    onto_main = n["baseRefName"] == (n["repository"]["defaultBranchRef"] or {}).get("name")
    stage = ("main" if n["state"] == "MERGED" and onto_main else
             "open" if n["state"] == "MERGED" else "dropped" if n["state"] == "CLOSED" else
             "held" if held else "stuck" if checks in ("FAILURE", "ERROR") else
             "ready" if green and not n["isDraft"] else "open")
    moved = ts(n["mergedAt"] or n["closedAt"] or commit.get("committedDate"))
    repo = n["repository"]["nameWithOwner"]
    return {"key": f'{repo}#{n["number"]}', "repo": repo, "url": n["url"],
            "head": n["headRefName"], "oid": n["headRefOid"], "title": hidden(repo) or n["title"],
            # Its first real sentence: a heading says nothing.
            "about": hidden(repo) or next((x for x in n["bodyText"].splitlines() if len(x) > 40),
                                          "")[:240],
            "stage": stage, "moved": moved >= now - DAY,
            "onto": "" if onto_main or n["state"] != "MERGED" else n["baseRefName"],
            "at": ts(n["mergedAt"] or n["closedAt"] or n["updatedAt"]),
            "need": ("rebase" if stage == "held" and n["mergeable"] == "CONFLICTING" else
                     "merge" if stage == "ready" else ""),
            "rounds": sum("opilot" in ((r.get("author") or {}).get("login") or "")
                          for r in n["reviews"]["nodes"])}


def hidden(repo: str | None) -> str:
    """A generic label for work in the repositories TMUXRC_DIGEST_HIDE names: scratch is
    shared, so neither the model nor the page gets their words. The registry keeps what it
    already holds; edit it after changing the list."""
    names = os.environ.get("TMUXRC_DIGEST_HIDE", "").split(",")
    return "Private work" if repo and repo.split("/")[-1] in names else ""


def cost(model: str, u: dict) -> float:
    i, o, r = next((v for k, v in PRICE.items() if k in model), PRICE["opus"])
    write = u.get("cache_creation_input_tokens", 0)
    hour = (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0)
    return (u.get("input_tokens", 0) * i + u.get("output_tokens", 0) * o
            + u.get("cache_read_input_tokens", 0) * r + (write * 1.25 + hour * .75) * i) / 1e6


def spend(dirs: list[str], paths: list[str], now: float) -> list[dict]:
    """One row per Claude Code transcript: its list-price cost in the last day (d), the rest
    of the week (w) and the week before (p), and where it worked: the worktree most of its
    tool calls name (`paths`: those that are not a root, longest first). Its git-branch field
    is never read: a session sits at the repo root, so that is the root's branch."""
    rows, seen = [], set()  # a session moved between accounts keeps a copy of its calls
    for d in dirs:
        for f in [*Path(d, "projects").glob("*/*.jsonl"),
                  *Path(d, "projects").glob("*/*/subagents/*.jsonl")]:
            if f.stat().st_mtime < now - 14 * DAY:
                continue
            row, votes, links, cwd = {"d": 0.0, "w": 0.0, "p": 0.0}, Counter(), set(), ""
            with f.open(errors="ignore") as lines:
                for line in lines:
                    if '"assistant"' not in line and '"pr-link"' not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("type") == "pr-link":
                        links.add(f'{r.get("prRepository")}#{r.get("prNumber")}')
                    msg = r.get("message")
                    if r.get("type") != "assistant" or not isinstance(msg, dict):
                        continue
                    cwd = r.get("cwd") or cwd
                    used = json.dumps([cwd, *(
                        b.get("input") for b in msg.get("content") or []
                        if isinstance(b, dict) and b.get("type") == "tool_use")])
                    votes.update([p for p in paths if p in used][:1])
                    usage, ago = msg.get("usage"), now - ts(r.get("timestamp"))
                    call = msg.get("id") or r.get("requestId")
                    if usage and call not in seen and ago < 14 * DAY:
                        seen.add(call)
                        row["d" if ago < DAY else "w" if ago < 7 * DAY else "p"] += cost(
                            msg.get("model") or "", usage)
            if any(row.values()):
                rows.append({**row, "acct": d, "cwd": cwd,
                             "wt": votes.most_common(1)[0][0] if votes else None,
                             "pr": next(iter(links)) if len(links) == 1 else None})
    return rows


def inside(trees: list[dict], cwd: str | None) -> dict:
    """The innermost worktree holding cwd, or {}."""
    return max((w for w in trees if ((cwd or "") + "/").startswith(w["path"] + "/")),
               key=lambda w: len(w["path"]), default={})


def join(rows: list[dict], trees: list[dict], changes: list[dict], names: dict) -> dict:
    """The rows' effort four ways. "effort": per change, per worktree that has no change
    ("wt:repo/name") and "" for the rest, which is coordination; a run belongs to its
    worktree's branch's PR, else to the one PR its session links. Then by account, repo
    and worktree, for the explorer; "" there is effort with no such place."""
    head = {(c["repo"], c["head"]): c["key"] for c in changes}
    at = {w["path"]: w for w in trees}
    out = {cut: {} for cut in ("effort", "account", "repo", "worktree")}
    for r in rows:
        w = at.get(r["wt"]) or {}
        home = w or inside(trees, r["cwd"])
        private, name = hidden(home.get("repo")), w and Path(w["path"]).name
        labels = {
            "effort": head.get((w.get("repo"), w.get("branch")))
            or (r["pr"] in head.values() and r["pr"])
            or (w and not private and f'wt:{w["repo"]}/{name}') or "",
            "account": names.get(r["acct"], r["acct"]), "worktree": private or name or "",
            "repo": private or (home.get("repo") or "").rpartition("/")[2]}
        for cut, label in labels.items():
            slot = out[cut].setdefault(label, {"d": 0.0, "w": 0.0, "p": 0.0})
            for k in slot:
                slot[k] += r[k]
    return out


def git(*args: str) -> str | None:
    """No optional locks: a status here must never take an index.lock an agent then hits."""
    done = subprocess.run(["git", "--no-optional-locks", *args], capture_output=True, text=True,
                          timeout=30, check=False)
    return None if done.returncode else done.stdout.strip()


def worktrees(cwds: list[str]) -> list[dict]:
    """Every worktree of every repository a pane sits in, with what the cleanup gates read."""
    out = []
    for common in sorted({git("-C", c, "rev-parse", "--path-format=absolute", "--git-common-dir")
                          for c in set(cwds)} - {None, ""}):
        repo = None
        listing = git("--git-dir", common, "worktree", "list", "--porcelain") or ""
        for block in listing.split("\n\n"):
            fields = dict([*line.split(" ", 1), ""][:2] for line in block.splitlines())
            path = fields.get("worktree")
            if path and os.path.isdir(path):
                repo = repo or github_repository(path)
                out.append({"path": path, "common": common, "repo": repo, "oid": fields.get("HEAD"),
                            "branch": fields.get("branch", "").removeprefix("refs/heads/"),
                            "dirty": git("-C", path, "status", "--porcelain") != "",
                            # When HEAD last moved: a fresh checkout of an old commit is new.
                            "active_at": float((git("-C", path, "log", "-g", "-1", "--date=unix",
                                                    "--format=%gd") or "").partition("{")[2][:-1]
                                               or time.time())})
    return out


def prunable(trees: list[dict], changes: list[dict], cwds: list[str], now: float) -> list[str]:
    """Worktrees safe to remove, by gates alone (the model never adds to this list): clean,
    at exactly the commit its branch's PR merged, no other PR of that branch unfinished, no
    pane in it, and idle for a day."""
    merged = {(c["repo"], c["head"], c["oid"]) for c in changes if c["stage"] == "main"}
    unfinished = {(c["repo"], c["head"]) for c in changes if c["stage"] not in ("main", "dropped")}
    return [w["path"] for w in trees
            if w["common"] != w["path"] + "/.git" and not w["dirty"] and not hidden(w["repo"])
            and (w["repo"], w["branch"], w["oid"]) in merged and w["active_at"] < now - DAY
            and (w["repo"], w["branch"]) not in unfinished
            and not any((cwd + "/").startswith(w["path"] + "/") for cwd in cwds)]


def gather(now: float) -> dict:
    def api(path):
        port = os.environ.get("TMUXRC_PORT", "18030")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/{path}", timeout=30) as reply:
            return json.load(reply)

    panes, accounts = api("state")["panes"], api("usage")["accounts"]
    names = {p["dir"]: p["name"] for p in api("accounts")["profiles"]}  # as the Usage tab says
    cwds = [p["cwd"] for p in panes if p.get("cwd")]
    trees = worktrees(cwds)
    common = {w["repo"]: w["common"] for w in trees}

    def held(n):  # its head is in the integration branch the user's machine runs
        root = common.get(n["repository"]["nameWithOwner"])
        return bool(root) and n["state"] == "OPEN" and git(
            "--git-dir", root, "merge-base", "--is-ancestor", n["headRefOid"],
            "origin/integration") is not None

    since = time.strftime("%Y-%m-%d", time.gmtime(now - 14 * DAY))
    changes = [change(n, now, held(n))
               for n in search(f"is:pr author:@me archived:false updated:>={since}")]
    for _ in range(3):  # a stack: merged into a branch whose own PR then reached main
        landed = {(c["repo"], c["head"]): c["at"] for c in changes if c["stage"] == "main"}
        for c in changes:
            if c["onto"] and c["at"] <= landed.get((c["repo"], c["onto"]), 0):
                c.update(stage="main", onto="")
    paths = sorted((w["path"] for w in trees if w["common"] != w["path"] + "/.git"),
                   key=len, reverse=True)
    rows = spend([d for a in accounts if a["provider"] == "claude" for d in a["dirs"]], paths, now)
    return {
        "now": now, "changes": changes, **join(rows, trees, changes, names),
        "asked": len(search("is:pr is:open review-requested:@me archived:false")),
        "waiting": [{"id": p["pane_id"], "title": private or p.get("title") or p.get("label") or "",
                     "headline": private or p.get("headline") or ""} for p in panes
                    if p.get("activity") == "waiting" and p.get("waiting_on") != "external"
                    for private in [hidden(inside(trees, p.get("cwd")).get("repo"))]],
        "usage": [{"name": names.get(a["dirs"][0], a["provider"]), "pct": w["pct"],
                   "projected": w["projected"],
                   "pace": (now * 1000 - w["start"]) / (w["resets_at"] - w["start"]) * 100}
                  for a in accounts for w in a["windows"]
                  if w["window"] == "7d" and w.get("start")],
        "cleanup": prunable(trees, changes, cwds, now)}


def sentences(text: object, repos: set[str], leaky: bool = False) -> list[str]:
    """The text's sentences that name no change number and no repository, or (leaky) the ones
    that do: those never reach the page, and the eval counts them."""
    return [s for s in str(text or "").split(". ") if s.strip() and leaky is (
        "#" in s or bool(repos & {word.strip(".,:;!?()'\"") for word in s.split()}))]


def lay(reg: dict, facts: dict) -> dict:
    """The registry's themes as the pages draw them. Every piece, state word, share, action
    and figure here is a rule over the facts; the registry holds the model's words."""
    now, effort = facts["now"], facts["effort"]

    def week(keys, spans="dw"):
        return sum(effort.get(k, {}).get(x, 0) for k in keys for x in spans)

    def figures(spent, landed):
        return {"per": spent / max(len(landed), 1), "landed": len(landed),
                "rounds": statistics.median([c["rounds"] for c in landed] or [0])}

    total = week(effort) or 1
    # The week's work, and whatever still waits on the user however long it has sat.
    live = {c["key"]: c for c in facts["changes"] if c["at"] >= now - 7 * DAY or c["need"]}
    themes = []
    for tid, t in reg["themes"].items():
        # Written by hand, a theme may be just its name.
        t.update({"members": [], "last": now, "outcome": "", "short": t["name"]} | t)
        pieces = sorted((live[k] for k in t["members"] if k in live),
                        key=lambda c: (list(STAGES).index(c["stage"]), not c["need"], c["key"]))
        active = [p["stage"] for p in pieces if p["stage"] != "dropped"]
        rest = ["open" if stage == "ready" else stage for stage in active if stage != "main"]
        spent = week(t["members"])
        if pieces or spent:
            t["last"] = now
        themes.append({
            "id": tid, **{k: t[k] for k in ("name", "short", "outcome", "members")},
            "pieces": pieces, "effort": spent, "part": spent / total,
            "day": week(t["members"], "d"),
            "share": next(word for least, word in SIZES if spent / total >= least),
            # On main only when nearly all of it is; else the state most of the outstanding
            # work is in (a tie: stuck, then held, then in progress). One red change among
            # many is its own red dot, not the theme's word.
            "holds": t.get("holds", ""),
            "state": "main" if active and len(rest) <= len(active) / 10 and "held" not in rest
            else max(("stuck", "held", "open"), key=rest.count) if rest else "open"})
    themes.sort(key=lambda t: (not any(p["need"] for p in t["pieces"]), -t["effort"]))
    placed = {k for t in themes for k in t["members"]}
    loose = [c for k, c in live.items() if k not in placed]
    # A rebase is one ask for a theme's whole stack; each change that can merge is its own.
    # The words are the model's once it has written them (reg["asks"]); the rest is rule.
    actions = [{"key": f'{kind}:{hit[0]["key"]}', "sub": NEEDS[kind][1], "btn": kind.title(),
                "text": reg.get("asks", {}).get(f'{kind}:{hit[0]["key"]}')
                or f'{t["short"]} {NEEDS[kind][0]}',
                "work": [p["title"] for p in hit], "url": hit[0]["url"], "theme": t.get("id")}
               for kind in NEEDS for t in [*themes, {"short": "Other work", "pieces": loose}]
               for hit in [[p for p in t["pieces"] if p["need"] == kind]][:kind == "rebase"]
               + [[p] for p in t["pieces"] if p["need"] == kind == "merge"] if hit]
    actions += [{"text": p["title"], "sub": ". ".join(sentences(p["headline"], set())),
                 "btn": "Open", "url": f'/m#pane={quote(p["id"])}'} for p in facts["waiting"]]
    if facts["asked"]:
        actions.append({"text": "A teammate asks for your review", "sub": "they are waiting",
                        "btn": "Review", "url": "https://github.com/pulls/review-requested"})

    def landed(newer, older):
        return [c for c in facts["changes"]
                if c["stage"] == "main" and now - older * DAY <= c["at"] < now - newer * DAY]

    def monday(t):
        return time.strftime("%G-W%V", time.localtime(t))

    # Both rewritten each run, so the two newest points are adjacent weeks, never overlapping.
    weeks = reg.setdefault("weeks", {})
    weeks[monday(now - 7 * DAY)] = figures(sum(e["p"] for e in effort.values()), landed(7, 14))
    weeks[monday(now)] = figures(total, landed(0, 7))
    # The same effort four ways, for yesterday and the week: [label, yesterday, week].
    cuts = {"Theme": [*([t["name"], t["day"], t["effort"]] for t in themes),
                      ["", week([""], "d"), week([""])]],
            **{cut.title(): [[label, e["d"], e["d"] + e["w"]] for label, e in rows.items()]
               for cut in ("account", "repo", "worktree") if (rows := facts.get(cut))}}
    return {"now": now, "themes": themes, "actions": actions, "coordination": week([""]),
            "cuts": cuts,
            "landed": sum(c["moved"] for c in landed(0, 7)), "weeks": weeks,
            "usage": facts["usage"], "cleanup": facts["cleanup"],
            "pace": ("Running hot" if any(u["projected"] >= 100 for u in facts["usage"])
                     else "On pace"),
            # What has no theme yet: changes, and worktrees with real effort and no change.
            "new": loose + [{"key": k, "title": f"worktree {k.rpartition('/')[2]}"} for k in effort
                            if k.startswith("wt:") and k not in placed
                            and week([k]) >= total / 100]}


def build(facts: dict, reg: dict, ask) -> dict:
    """One model call, folded into the registry under its identity rules: a placed change
    stays put, a new theme needs two changes, a theme keeps its id and its name, and a theme
    with nothing in it for two weeks retires. `ask(system, text)` returns the model's JSON,
    or None."""
    reg.setdefault("themes", {})
    d = lay(reg, facts)
    reply = ask((HERE / "digest_prompt.txt").read_text(), json.dumps({
        "themes": [{**{k: t[k] for k in ("id", "name", "short", "holds", "outcome")},
                    "state": STATES[t["state"]], "share": t["share"],
                    "work": [{"repo": hidden(p["repo"]) or p["repo"], "title": p["title"],
                              "stage": p["stage"]} for p in t["pieces"]]}
                   for t in d["themes"]],
        "new": [{"n": n, **{k: c[k] for k in ("title", "about", "stage") if k in c}, **(
            {} if hidden(c.get("repo")) else {"repo": c.get("repo"), "branch": c.get("head")})}
                for n, c in enumerate(d["new"])],
        "needs_you": [{"n": n, **{k: a[k] for k in ("text", "work") if k in a}}
                      for n, a in enumerate(d["actions"])],
        "largest": [t["short"] for t in sorted(d["themes"], key=lambda t: -t["effort"])[:2]
                    if t["effort"]],
        "landed_yesterday": d["landed"]})) or {}
    repos = {x for c in facts["changes"] for x in (c["repo"], c["repo"].split("/")[1])}
    placed = {k for t in reg["themes"].values() for k in t["members"]}
    leaks = sentences(f'{reply.get("daily")}. {reply.get("weekly")}', repos, leaky=True)
    for t in reply.get("themes") or []:
        add = [k for k in dict.fromkeys(d["new"][n]["key"] for n in t.get("add") or []
                                        if isinstance(n, int) and 0 <= n < len(d["new"]))
               if k not in placed]
        words = {k: ". ".join(sentences(t.get(k), repos)) for k in ("name", "short", "outcome")}
        leaks += [s for k in words for s in sentences(t.get(k), repos, leaky=True)]
        if not isinstance(t.get("id"), str) or (
                t["id"] not in reg["themes"] and (len(add) < 2 or not all(words.values()))):
            continue
        theme = reg["themes"].setdefault(t["id"], {"members": [], "last": facts["now"]})
        # A theme's name is the registry's: the model names a new theme once, and a name
        # edited in the file stays. Its outcome line is rewritten each run.
        theme.update({k: v for k, v in words.items() if v and (k == "outcome" or k not in theme)})
        theme["members"] += add
        placed.update(add)
    reg["asks"] = {d["actions"][a["n"]]["key"]: ". ".join(sentences(a.get("text"), repos))
                   for a in reply.get("asks") or [] if isinstance(a, dict)
                   and isinstance(a.get("n"), int) and 0 <= a["n"] < len(d["actions"])
                   and "key" in d["actions"][a["n"]]} or reg.get("asks", {})
    d = lay(reg, facts)  # before retiring: a theme with live work was just seen
    reg["themes"] = {k: t for k, t in reg["themes"].items()
                     if t["last"] >= facts["now"] - 14 * DAY}
    return {**d, "leaks": len(leaks), **{
        k: ". ".join(sentences(reply.get(k), repos)) or FALLBACK for k in ("daily", "weekly")}}


def shown(p: dict) -> str:
    """A change's title behind a tap, without its conventional-commit prefix."""
    kind, colon, rest = p["title"].partition(": ")
    return html.escape(rest if colon and kind.islower() and " " not in kind else p["title"])


def dot(p: dict, quiet: bool = False) -> str:
    dim = quiet and not p["moved"] and not p["need"]
    return (f'<button class="d {p["stage"]}{" need" * bool(p["need"])}{" quiet" * dim}" '
            f'data-t="{shown(p)}" data-s="{STAGES[p["stage"]]}" data-u="{html.escape(p["url"])}" '
            f'aria-label="{STAGES[p["stage"]]}"></button>')


def button(a: dict, quiet: bool = True) -> str:
    return (f'<a class="btn{" q" * quiet}" href="{html.escape(a["url"])}" target="_blank" '
            f'rel="noopener">{a["btn"]}</a>')


def panel(title: str, body: str) -> str:
    return f'<div class="panel"><h4>{title}</h4>{body}</div>'


def key(last: str) -> str:
    return '<div class="key">' + "".join(f'<span><i class="d {s}"></i>{STAGES[s]}</span>' for s in (
        "main", "held", "open", "stuck")) + last + "</div>"


def daily(d: dict) -> str:
    """Direction A of the design: the picture first, one dot per change by theme and stage."""
    columns = {"In progress": ("open", "ready", "stuck"), "Your machine": ("held",),
               "Landed": ("main",)}
    lanes = "".join(f'<div class="ch">{name}</div>' for name in columns)
    for t in d["themes"]:
        cells = [[p for p in t["pieces"] if p["stage"] in stages and (p["moved"] or s != "Landed")]
                 for s, stages in columns.items()]
        if any(cells):
            lanes += f'<div class="tn">{html.escape(t["name"])}</div>' + "".join(
                f'<div class="dots">{"".join(dot(p, quiet=True) for p in c)}</div>' for c in cells)
    acts = [f'<div class="act"><p>{html.escape(a["text"])}<small>{html.escape(a["sub"])}</small>'
            f"</p>{button(a, quiet=bool(n))}</div>" for n, a in enumerate(d["actions"])]
    if acts[3:]:
        acts[3:] = [f"<details><summary>{len(acts) - 3} more can wait</summary>",
                    *acts[3:], "</details>"]
    bars = "".join(
        f'<div>{html.escape(u["name"])}<span class="ubar"><i class="ahead" style="width:'
        f'{min(u["projected"], 100)}%"></i><i style="width:{u["pct"]}%"></i><i class="tick" '
        f'style="left:{u["pace"]:.0f}%"></i></span></div>' for u in d["usage"])
    spare = "".join(f"<li>{html.escape(Path(p).name)}</li>" for p in d["cleanup"])
    return (panel("Where everything stands", f'<div class="lanes">{lanes}</div><p class="pick">'
                  "Bright dots moved yesterday. Tap one to see what it is.</p>"
                  + key('<span><i class="d held need"></i>&nbsp;needs you</span>'))
            + panel("Needs you", "".join(acts) or '<p class="pick">Nothing is waiting on you.</p>')
            + (bars and panel("Plan usage", f'<div class="pace"><b>{d["pace"]}</b>'
                              f'<div class="bars">{bars}</div></div>'))
            + (spare and f"<details><summary>{len(d['cleanup'])} worktrees are safe to remove"
               '</summary><p class="pick">Clean, merged, no open change, no pane, idle for a day.'
               f"</p><ul>{spare}</ul></details>"))


def spark(v: list[float]) -> str:
    low, span = min(v), (max(v) - min(v)) or 1
    pts = [(4 + n * 62 / (len(v) - 1), 23 - (q - low) / span * 18) for n, q in enumerate(v)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return (f'<svg width="72" height="28" viewBox="0 0 72 28"><polyline points="{line}" '
            'fill="none" stroke="var(--nm)" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round"/><circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="4" '
            'fill="var(--fg)" stroke="var(--surface)" stroke-width="2"/></svg>')


def weekly(d: dict) -> str:
    """The themes are the page: block size is effort, colour is state, one card per theme."""
    blocks = sorted([(t["effort"], t["state"], t["short"]) for t in d["themes"] if t["effort"]]
                    + [(d["coordination"], "none", "Coordination")], reverse=True)
    tm = "".join(f'<div style="flex-grow:{sum(b[0] for b in row)}">' + "".join(
        f'<div class="s-{state}" style="flex-grow:{spent}"><i class="d {state}"></i>'
        f"{html.escape(name)}</div>"
        for spent, state, name in row) + "</div>" for row in (blocks[:2], blocks[2:4], blocks[4:])
        if row)
    cards = "".join(
        f'<div class="tcard"><div class="hd"><h5>{html.escape(t["name"])}</h5><span>{t["share"]}'
        f'</span></div><div class="state s-{t["state"]}">{STATES[t["state"]]}</div>'
        f'<p>{html.escape(t["outcome"])}</p><div class="dots">{"".join(map(dot, t["pieces"]))}'
        '</div><p class="pick" hidden></p><div class="foot"><details><summary>See the changes'
        "</summary>" + "".join(
            f'<a href="{html.escape(p["url"])}" target="_blank" rel="noopener">'
            f'<i class="d {p["stage"]}"></i>{shown(p)}</a>' for p in t["pieces"])
        + "</details>" + "".join([button(a) for a in d["actions"] if a.get("theme") == t["id"]][:1])
        + "</div></div>" for t in d["themes"] if t["pieces"] or t["effort"])
    versus = ""
    for field, fewer, more, good, what in (
            ("per", "Cheaper per change", "Dearer per change", "↓", "effort for each one landed"),
            ("landed", "Fewer reached main", "More reached main", "↑", "changes in the week"),
            ("rounds", "Shorter review loops", "Longer review loops", "↓", "rounds to land")):
        series = [w[field] for _, w in sorted(d["weeks"].items())[-5:]]
        if series[-1] == series[-2]:
            continue  # nothing moved: no arrow to draw
        arrow = "↓" if series[-1] < series[-2] else "↑"
        versus += (f'<div class="vs"><b class="{"good" if arrow == good else "bad"}">{arrow}</b>'
                   f"<p>{fewer if arrow == '↓' else more}<small>{what}</small></p>"
                   f"{spark(series)}</div>")
    return (panel("Where the effort went", f'<div class="tm">{tm}</div>' + key(""))
            + f'<div class="panel">{cards}</div>' + panel("Against last week", versus)
            + panel("Slice it", explorer(d["cuts"])))


def explorer(cuts: dict) -> str:
    """The same effort by theme, account, repo or worktree, yesterday or this week: a sentence,
    a proportion bar and small numbers. Every view is drawn here; the page shows one."""
    def seg(group):
        return '<div class="seg">' + "".join(
            f'<button data-v="{v}" aria-pressed="{"false" if n else "true"}">{v}</button>'
            for n, v in enumerate(group)) + "</div>"

    views = ""
    for at, when in enumerate(("Yesterday", "This week"), 1):
        for cut, table in cuts.items():
            rows = sorted(([r[0], r[at]] for r in table if r[at]), key=lambda r: -r[1])
            total = sum(v for _, v in rows) or 1
            top = next((r for r in rows if r[0]), ["nothing", 0])
            size = next(word for least, word in SIZES if top[1] / total >= least).replace(
                "small", "a small part").capitalize()
            none = "Coordination" if cut == "Theme" else f"No {cut.lower()}"
            lines = "".join(
                f'<div class="xrow{" un" * (not label)}"><span>{html.escape(label or none)}</span>'
                f'<small>{v / total:.0%}</small><i style="width:{v / rows[0][1]:.0%}"></i></div>'
                for label, v in rows[:7])
            views += (
                f'<div class="x" data-k="{when}-{cut}"{" hidden" * (at == 1 or cut != "Theme")}>'
                f'<p class="xsay">{size} of {when.lower()}’s '
                f'effort went into “{html.escape(top[0])}”.</p><div class="prop">' + "".join(
                    f'<i class="{"un" * (not label)}" style="flex-grow:{v}"></i>'
                    for label, v in rows) + f"</div>{lines}</div>")
    return seg(("This week", "Yesterday")) + seg(cuts) + views


def publish(d: dict, root: Path) -> None:
    """Both folders, each one's meta.json first (its description is the push's teaser), and
    summary.json for the dashboard's card. One page holds both views, switched in place: the
    app's viewer runs no script in a page its frame navigates to."""
    page = (HERE / "digest.html").read_text().replace(
        "{tokens}", tokens(HERE.parent / "web/m/style.css"))
    when = datetime.fromtimestamp(d["now"]).astimezone()
    views = {"daily": ("Today", "Daily digest", d["daily"], daily(d)),
             "weekly": ("This week", "Weekly digest", d["weekly"], weekly(d))}
    body = "<nav>" + "".join(f'<label for="{v}">{tab}</label>' for v, (tab, *_) in views.items()) \
        + "</nav>" + "".join(
        f'<section class="{v}"><div class="date">{when:%A %-d %B}</div><p class="verdict">'
        f"{html.escape(first + stop)}<em>{html.escape(rest)}</em></p>{view}</section>"
        for v, (_, _, verdict, view) in views.items()
        for first, stop, rest in [verdict.partition(". ")])  # the second sentence is the point
    for name, (_, title, verdict, _) in views.items():
        folder = root / f"digest-{name}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "meta.json").write_text(json.dumps({
            "title": title, "description": verdict.split(". ")[0],
            "pane": os.environ.get("TMUX_PANE", ""), "created": when.isoformat("T", "seconds")}))
        picks = "".join(f'<input type="radio" name="view" id="{v}"{" checked" * (v == name)}>'
                        for v in views)
        (folder / "index.html").write_text(
            page.replace("{title}", title).replace("{body}", picks + body))
    (root / "digest-daily/summary.json").write_text(json.dumps({
        "generated": when.isoformat("T", "seconds"), "pace": d["pace"],
        "verdict": ". ".join(d["daily"].split(". ")[:2]).rstrip(".") + ".",
        "needs_you": [{"text": a["text"], "href": a["url"]} for a in d["actions"][:3]],
        "more": len(d["actions"][3:]),
        "themes": [{"name": t["name"], "state": STATES[t["state"]], "share": round(t["part"], 3)}
                   for t in d["themes"] if t["pieces"] or t["effort"]]}))


def model():
    """(ask, usage): openbus.llm's own Gemini call, given the minutes one large request
    needs (its timeout is read at import), a model that writes better than the parser's fast
    one, and that model's price from the repo's rate cards."""
    load_dotenv(dotenv_path())  # the daemon's own configuration and model credentials
    load_dotenv(Path.home() / ".config/tmux-rc/openai.env")
    os.environ.setdefault("TMUXRC_LLM_TIMEOUT_MS", "600000")
    name = os.environ["TMUXRC_GEMINI_MODEL"] = os.environ.get(
        "TMUXRC_DIGEST_MODEL", "gemini-3-flash-preview")
    from openbus import live_providers, llm
    for card in (m.rates for m in live_providers.models() if m.model == name):
        llm._IN_PER_M, llm._OUT_PER_M = card.text_in, card.text_out  # noqa: SLF001 - its rates
    return (lambda system, text: llm.classify_text(system, text, kind="digest")), llm.usage_totals


def main() -> None:
    ask, usage = model()
    STATE.mkdir(parents=True, exist_ok=True)
    root = Path(os.environ.get("TMUXRC_SCRATCH_DIR") or STATE.parent / "scratch")
    for meta in (root / "digest-daily/meta.json", root / "digest-weekly/meta.json"):
        if meta.exists():  # its own two pages: scratch then announces each as it lands
            marked = json.loads(meta.read_text()) | {"updating": "Today's digest"}
            meta.write_text(json.dumps(marked))
    facts = gather(time.time())
    (STATE / "facts.json").write_text(json.dumps(facts))  # what `make digest-eval DIR=` scores
    registry = STATE / "themes.json"
    reg = json.loads(registry.read_text()) if registry.exists() else {}
    # With nothing placed yet it takes two calls: one to file the work, one to write about it.
    for _ in range(1 if any(t.get("members") for t in reg.get("themes", {}).values()) else 2):
        d = build(facts, reg, ask)
    registry.write_text(json.dumps(reg, indent=1))
    publish(d, root)
    print(f'{len(d["themes"])} themes, {len(d["actions"])} need you; model {usage()}')


if __name__ == "__main__":
    main()
