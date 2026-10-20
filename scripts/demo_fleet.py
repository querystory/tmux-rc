"""A fictional tmux fleet for screenshots: README images, docs, and before/after diffs.

Everything here is invented — example-org, its sessions, every summary and URL — so a
screenshot taken against it can be published as-is. tests/test_demo_fleet.py keeps it that
way by failing on anything that looks like a real path, account, or credential.

Time is frozen at NOW: every timestamp is an offset from it, and the screenshot browser's
clock is pinned to the same instant, so "3m ago" and the chart's x-axis never drift.
History is generated from a seed, not stored, so it costs a few lines instead of megabytes.
"""
from __future__ import annotations

import json
import math
import os
import random
from datetime import UTC, datetime
from types import SimpleNamespace

from PIL import Image, ImageDraw

from openbus.history import COVERAGE, History

NOW = datetime(2026, 6, 16, 15, 30, tzinfo=UTC).timestamp()  # a Tuesday afternoon
HISTORY_DAYS = 37
GOAL = 12  # the shared "running agents" goal line
ORG = "github.com/example-org"
SESSIONS = ["shop-api", "ci", "research", "docs", "ops", "infra", "marketing", "misc"]

# session index | tool | run / need / idle | title | age | u = finished recently, or the
# question when need | the activity line, or the reply options when need.
ROWS = """\
0|claude|idle|release notes agent|2m||Waiting for the preview build after updating review docs.
0|codex|run|migration planner|now||Mapping table dependencies before proposing an order.
0|opencode|run|schema reviewer|now||Checking the new column constraints against live data.
0|claude|run|api contract diff|now||Drafting the v3 migration note for the two changed endpoints.
0|claude|need|preview deploy|6m|Preview is up. Run the checks yourself before I merge: ! make test|
0|shell|idle|zsh ~/src/shop-api|41m||git status: clean, on main.
0|codex|idle|seed data refresh|5h||Refreshed 12 fixtures. Tests green.
1|claude|idle|flaky test hunter|4m|u|Quarantined 2 flaky e2e specs. Issue filed for each.
1|claude|run|e2e triage|now||Re-running checkout.spec with tracing on.
1|codex|idle|lint autofix|12m|u|Fixed 38 warnings across 9 files. Ready to commit.
1|shell|idle|build cache warmer|1h||Cache warm: 214 layers, 3.1 GB.
1|shell|idle|zsh ~/src/ci|3h||make test passed in 2m 41s.
2|claude|run|eval harness|now||Scoring 120 samples against the candidate prompt.
2|codex|run|prompt A/B sweep|now||Variant C leads by 4 points after 60 of 200 runs.
2|opencode|idle|dataset dedupe|25m|u|Removed 1,204 near-duplicates. Report written.
2|shell|idle|notebook scratch|6h||Kernel idle.
2|claude|idle|paper notes|1d||Summarised sections 3 and 4.
2|codex|idle|tokenizer bench|2d||Bench done: 1.8x faster on long inputs.
3|omp|run|release notes drafter|now||Grouping merged PRs since v0.14 by area.
3|claude|idle|screenshot refresher|8m|u|Regenerated 22 screenshots for the light theme.
3|omp|idle|changelog bot|1h||Opened a draft entry for 0.15.
3|opencode|idle|api reference regen|3h||Reference rebuilt, no broken links.
3|shell|idle|zsh ~/src/docs|1d||Idle at the prompt.
4|claude|idle|alert tuning|11m||Raised the build-02 disk alert to 90%. Paging is quiet.
4|codex|run|cost report|now||Pulling last month's billing export.
4|opencode|idle|log triage|3h||No new errors since the last check.
4|claude|idle|pager rota sync|4h||Rota synced through next Friday.
4|shell|idle|zsh ~/ops|2h||Idle at the prompt.
4|shell|idle|backup verify|1d||All 6 snapshots restored and checked.
5|claude|run|terraform plan review|now||Reading a 41-resource plan; 2 replacements so far.
5|codex|idle|node pool resize|34m||Resized pool-b to 6 nodes.
5|shell|idle|zsh ~/infra|52m||Idle at the prompt.
5|claude|idle|dns cleanup|6h||Removed 9 stale records.
5|claude|idle|k8s upgrade notes|1d||Drafted the 1.31 upgrade checklist.
5|opencode|idle|cert rotation|2d||Certificates valid until March.
6|claude|idle|pricing page copy|7m|u|Draft saved with three headline options.
6|claude|idle|launch email|3h||Final copy sent for review.
6|gemini|idle|competitor scan|1d||Compared five pricing pages.
6|shell|idle|zsh ~/marketing|2d||Idle at the prompt.
7|codex|run|dependency audit|now||Checking 3 major upgrades against the changelogs.
7|claude|run|sidebar toggle|now||Wiring the group-by toggle into the sidebar.
7|shell|idle|zsh ~/src|41m||Idle at the prompt.
7|claude|idle|rss digest|5h||Summarised 12 new posts.
7|opencode|idle|invoice parser|2d||Parsed 31 invoices, 2 flagged.
7|shell|idle|dotfiles sync|3d||Synced with origin.
7|claude|idle|disk cleanup|6m||Pruned images older than 14 days. Freed 38 GB.
7|claude|run|webhook timeouts|now||Moving stock reservation into a background job.
0|claude|need|refunds|1m|PRs #413 and #414 are green and reviewed. Merge?|Merge both,Just #413
5|claude|idle|prod deploy|2m||Production is on v0.15.2. Health checks green.
"""

# Where each pane works, one per ROWS line: repository/directory@worktree (the main
# checkout when no @), or a plain folder outside any repository. The Fleet graph's Repo and
# Worktree lenses group by these; the open-loops scan finds the worktrees (worktrees()).
WHERE = """shop-api/docs shop-api/db shop-api/db shop-api@money-v3 shop-api@preview shop-api
shop-api/fixtures shop-web/e2e shop-web/e2e@checkout-flake shop-web/src ci-runner ci-runner
evals/harness evals/prompts@ab-sweep evals/data evals/notebooks /work/papers tokenizer docs-site
docs-site/static/img docs-site/changelog docs-site/reference docs-site ops-scripts/alerts
ops-scripts/billing ops-scripts/logs ops-scripts ops-scripts ops-scripts/backup
infra/terraform@plan-review infra/k8s infra infra/dns infra/k8s infra/certs site-copy/pricing
site-copy/email /work/marketing /work/marketing shop-web@deps-audit dashboard-ui/src@sidebar-toggle
/work /work/notes invoice-tools dotfiles /work shop-api@webhooks shop-api@refunds infra""".split()  # noqa: SIM905 - a table reads better than 49 quoted strings


def _where(i: int) -> tuple[str, str | None, str | None]:
    """(cwd, the worktree's path, its branch) of pane %i; no worktree outside a repository.
    A pane past ROWS (the sign-in window) sits in /work."""
    spot = WHERE[i - 1] if i <= len(WHERE) else "/work"
    if spot.startswith("/"):
        return spot, None, None
    spot, _, branch = spot.partition("@")
    repo, _, sub = spot.partition("/")
    root = f"/work/{repo}" + (f"/.worktrees/{branch}" if branch else "")
    return "/".join(filter(None, [root, sub])), root, branch or "main"


def worktrees() -> list[dict]:
    """The worktrees the panes sit in, as the open-loops scan lists them: clean and pushed,
    so none of them joins Open loops' lanes."""
    rows = {}
    for i in range(1, len(WHERE) + 1):
        _, root, branch = _where(i)
        if root:
            repo = root.split("/")[2]
            rows[root] = {"path": root, "repo": f"example-org/{repo}", "branch": branch,
                          "common": f"/work/{repo}/.git", "dirty": 0, "unpushed": 0,
                          "active_at": NOW - 3600}
    return list(rows.values())


MODELS = {"claude": ("Opus 5", "Sonnet 5"), "codex": ("GPT-6.1",), "gemini": ("Gemini 3.5 Pro",),
          "opencode": ("Claude Sonnet 5", "GPT-6.1"), "omp": ("GPT-6.1", "Claude Opus 5")}


# Plan limits, one Claude and one weekly-only Codex account, as a typical day reads: a
# light 5h window behind its even pace, a 7d window on pace to end in the 90s (amber), a
# per-model weekly limit, and Codex on pace to run out before its reset (red), with no 5h
# window at all. provider | config dir | the sessions whose panes draw on it | per window:
# name, seconds, share of it gone at NOW, % used at NOW.
PLANS = [
    ("claude", "~/.claude", SESSIONS, [("5h", 5 * 3600, 1 - 160 / 300, 16),
                                       ("7d", 7 * 86400, 1 - 7.5 / 168, 89),
                                       ("7d Fable", 7 * 86400, 1 - 7.5 / 168, 52)]),
    ("codex", "~/.codex", SESSIONS, [("7d", 7 * 86400, 1 - 127 / 168, 40)]),
]
# The two-account state (TMUXRC_DEMO_ACCOUNTS=2): a personal Claude plan beside the work
# one, its default in two sessions. Named by label, as accounts.profiles() names them.
PERSONAL = ("claude", "~/.claude-personal", ["research", "docs"],
            [("5h", 5 * 3600, 1 - 185 / 300, 34), ("7d", 7 * 86400, 1 - 74 / 168, 22),
             ("7d Fable", 7 * 86400, 1 - 74 / 168, 9)])
# Where past windows ended, cycled per meter; past 100 is a window that hit its limit before
# the reset (the curve stops at 100%). USAGE_DAYS of them, so the usage view's 30d range is
# full: weekly windows back to back, 5h ones through the working day (a 5h window opens at
# the first prompt after a reset), one a weekend day, and lighter.
PAST_PEAKS = (74, 118, 61, 92, 38, 104, 55, 83, 27, 66, 47)
USAGE_DAYS = 45
FIVE_HOUR_STARTS = (7.5, 13, 18.5)  # o'clock, UTC
PROFILES = {"~/.claude": ("Work", "Example Org", "Team", 0),
            "~/.claude-personal": ("Personal", "Sam Rivera", "Max", 1),
            "~/.codex": ("Codex", "", "", 2)}


# The add-account flow's new account (its sign-in window is sign_in_pane()).
SIGNED_IN_AFTER = 6  # seconds from opening the sign-in window to the stub "signing in"
DEMO_ACCOUNT = {"dir": "~/.claude-demo", "tilde": "~/.claude-demo", "provider": "claude",
                "name": "Demo", "label": "Demo", "who": "Demo Studio", "plan": "Pro", "color": 3,
                "default": False, "panes": ["%90"]}


def plans(two: bool = False) -> list[tuple]:
    if not two:
        return PLANS
    work = [s for s in SESSIONS if s not in PERSONAL[2]]
    return [PERSONAL, (*PLANS[0][:2], work, PLANS[0][3]), PLANS[1]]


def accounts(two: bool = False, signing_in: float | None = None) -> dict:
    """GET /api/accounts as accounts.report() answers it for this fleet; with
    `signing_in` (seconds since the add flow opened its sign-in window), Demo too, signed in
    after SIGNED_IN_AFTER."""
    panes = fleet()
    profiles = [{"dir": d, "tilde": d, "provider": tool, "name": PROFILES[d][0],
                 "label": PROFILES[d][0] if two else None, "who": PROFILES[d][1],
                 "plan": PROFILES[d][2], "color": PROFILES[d][3], "signed_in": True,
                 "default": "-" not in d,
                 "panes": [p["pane_id"] for p in panes if p["tool"] == tool and p["session"] in ss]}
                for tool, d, ss, _ in plans(two)]
    # Personal's sessions set it in their tmux environment; the rest set nothing.
    sessions = {s: {"claude": PERSONAL[1] if s in PERSONAL[2] else None, "codex": None}
                for s in SESSIONS} if two else {}
    if signing_in is not None:
        profiles.append({**DEMO_ACCOUNT, "signed_in": signing_in >= SIGNED_IN_AFTER})
    return {"profiles": profiles, "sessions": sessions}


STALE = {"claude": 360}  # a failed last read: the meter shows the reading from 6m before


# SGR helpers for the captures: the live view renders these as colored spans.
def sgr(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m"


def b(t): return sgr("1", t)
def dim(t): return sgr("2", t)
def red(t): return sgr("31", t)
def grn(t): return sgr("32", t)
def yel(t): return sgr("33", t)
def blu(t): return sgr("34", t)
def mag(t): return sgr("35", t)
def cyn(t): return sgr("36", t)


RULE = dim("─" * 78)

# Hand-made terminal screens for the panes the screenshots open; the rest get a plain one.
CAPTURES = {
    # The add-account shots' sign-in window: a stub of the provider's own sign-in, never a
    # real login screen (scripts/screenshots.mjs, demo_server's sign-in state).
    "sign in: Demo": "\n".join([
        dim("$ CLAUDE_CONFIG_DIR=~/.claude-demo claude"), "",
        f"{mag('*')} {b('Welcome to Claude Code')} {dim('(demo stub)')}", "",
        "Not signed in on ~/.claude-demo.", "", "> /login", "  Select login method:",
        grn("  > 1. Claude account with subscription"), "    2. Anthropic Console account", "",
        dim("  Opening the browser to sign in...")]),
    "e2e triage": "\n".join([
        f"{b('●')} The checkout flake reproduces only when the payment iframe loads after the",
        "  coupon request, so I added tracing and am re-running the spec 20 times.",
        "",
        f"{grn('●')} {b('Bash')}(npx playwright test checkout.spec.ts --repeat-each 20 --trace on)",
        f"  ⎿  {grn('✓')} checkout › applies a coupon before payment {dim('(4.1s)')}",
        f"     {grn('✓')} checkout › pays with a saved card {dim('(3.8s)')}",
        f"     {red('✘')} checkout › applies a coupon before payment {dim('(retry #1, 6.0s)')}",
        f"     {yel('… +37 lines')} {dim('(ctrl+o to expand)')}",
        "",
        f"{grn('●')} {b('Update')}(tests/e2e/checkout.spec.ts)",
        (f"  ⎿  Updated tests/e2e/checkout.spec.ts with {grn('6 additions')} and "
         f"{red('2 removals')}"),
        "",
        f"{yel('✻')} Tracing… {dim('(4m 12s · ↓ 18.4k tokens · esc to interrupt)')}",
        "",
        f"  {b('☒')} {dim('Reproduce the flake locally')}",
        f"  {b('☒')} {dim('Capture a trace of a failing run')}",
        "  ☐ Wait for the iframe instead of the network idle event",
        "  ☐ Open a PR with the fix",
        "", RULE, "❯ ", RULE,
        f"{cyn('~/src/shop-web')} on {mag('fix/checkout-flake')} | {blu('Opus 5')}",
        f"{dim('session:')} {grn('+64')}/{red('-12')} lines · {yel('41% ctx')} · {yel('$3.12')}",
        f"{dim('-- INSERT --')} {mag('⏵⏵ accept edits on')} {dim('(shift+tab to cycle)')}",
    ]),
    "preview deploy": "\n".join([
        f"{b('●')} Preview is up. Run the checks yourself before I merge:",
        "", f"  {blu('! make test')}", "",
        '  Or say "ship it" and I\'ll merge it behind CI.', "",
        f"{dim('✻ Worked for 41s · done 2:14 PM')}", "", RULE, "❯ ", RULE,
        f"{cyn('~/src/shop-api')} on {mag('feat/preview')} | {blu('Opus 5')}",
        f"{mag('⏵⏵ auto mode on')} {dim('(shift+tab to cycle)')}",
    ]),
    "backup verify": "\n".join([
        f"{grn('dev@ops')}$ sudo ./verify-snapshots.sh", "[sudo] password for dev: "]),
}

# Extra card fields for the panes the screenshots open, so every section has something.
DETAIL = {
    # A sudo prompt: its tty stopped echoing, so the composer is a password field.
    "backup verify": {"secret": True, "headline": "sudo is asking for a password.",
                      "status_line": "sudo is asking for a password."},
    "e2e triage": {
        "model": "Opus 5", "context_pct": 41, "cost": "$3.12", "mode": "accept-edits",
        "session_summary": "The checkout flake reproduces only when the payment iframe loads "
                        "after the coupon request. A traced 20x rerun is in progress; next is "
                        "waiting on the iframe instead of network idle, then a PR.",
        "working": {"verb": "Tracing", "elapsed": "4m 12s", "tokens": "18.4k"},
        "status_entries": ["fix/checkout-flake", "+64 / -12"],
        "tasks": [{"text": "Reproduce the flake locally", "done": True},
               {"text": "Capture a trace of a failing run", "done": True},
               {"text": "Wait for the iframe instead of the network idle event", "done": False},
               {"text": "Open a PR with the fix", "done": False}],
        "subagents": [{"label": "Diff passing and failing traces", "state": "running",
                    "elapsed": "2m", "tokens": "9.1k"},
                   {"label": "Search for other networkidle waits", "state": "done",
                    "elapsed": "1m", "tokens": "4.0k"}],
        "agents": 1,
        "prs": [{"repo": "example-org/shop-web", "number": 412,
              "title": "Stabilize checkout.spec under slow iframes"}],
        "links": [{"href": f"https://{ORG}/shop-web/actions/runs/1029384756", "text": "CI run"}]},
    "api contract diff": {
        "model": "Sonnet 5", "context_pct": 39, "cost": "$0.84", "agents": 2,
        "subagents": [{"label": "List clients of the two changed endpoints", "state": "running",
                    "elapsed": "1m", "tokens": "7.4k"},
                   {"label": "Draft the v3 migration note", "state": "running", "elapsed": "40s",
                    "tokens": "3.1k"}],
        "session_summary": "Moving money fields to a structured type changed two public "
                        "endpoints. Both break existing clients, so they move to a v3 API.",
        "prs": [{"repo": "example-org/shop-api", "number": 409,
              "title": "Structured money type for order totals"}]},
    "refunds": {
        "headline": "Refund PRs ready to merge", "status_line": "Refund PRs ready to merge",
        "model": "Opus 5", "context_pct": 47, "cost": "$1.96",
        "session_summary": "Both PRs passed CI and have an approving review. #414 builds on "
                           "#413, so merging both ships partial refunds end to end.",
        "prs": [{"repo": "example-org/shop-api", "number": 413,
                 "title": "Partial refunds in the payments service"},
                {"repo": "example-org/shop-api", "number": 414,
                 "title": "Refund button on the order page"}]},
    # A finished turn in auto mode that hands the user a command: classify()'s offers, and
    # the question its _final_ask leaves (no reply options, since the command is the answer).
    "preview deploy": {"offers": ["! make test", "ship it"],
                       "headline": "Preview deployed; the checks are yours to run.",
                       # A link the question points at: its card leads with it (and Copy link).
                       "links": [{"href": f"https://{ORG}/shop-api/deployments/preview",
                                  "text": "Preview link"}]},
    "cost report": {"activity": "compacting", "headline": "Compacting the conversation",
                        "status_line": "Compacting the conversation", "context_pct": 94},
    "terraform plan review": {
        "activity": "waiting", "waiting_on": "external", "model": "Opus 5", "context_pct": 58,
        "cost": "$2.05",
        "mode": "plan", "agents": 3,
        "subagents": [{"label": "Check replacements for data loss", "state": "running",
                    "elapsed": "3m", "tokens": "21.7k"},
                   {"label": "Cross-check IAM changes", "state": "running", "elapsed": "2m",
                    "tokens": "12.3k"},
                   {"label": "Summarize the 33 in-place changes", "state": "running",
                    "elapsed": "48s", "tokens": "6.2k"}],
        "tables": [{"title": "Plan summary", "headers": ["Action", "Count"],
                 "rows": [["add", "6"], ["change", "33"], ["replace", "2"], ["destroy", "0"]]}]},
}

# Activity feeds for the panes the screenshots open: (minutes before NOW, text).
EVENTS = {
    "e2e triage": [(48, "Started on the checkout.spec flake"),
                   (35, "Reproduced it 3 times in 20 local runs"),
                   (22, "Traced a failing run: the iframe loads after the coupon call"),
                   (12, "Swapped the networkidle wait for an iframe locator"),
                   (4, "Started a traced 20x rerun")],
    "api contract diff": [(30, "Generated the OpenAPI spec for the money type"),
                          (9, "Diffed it against the published v2 contract"),
                          (3, "Started on the v3 migration note")],
}

_UNITS = {"m": 60, "h": 3600, "d": 86400}


def _age(text: str) -> int:
    return 30 if text == "now" else int(text[:-1]) * _UNITS[text[-1]]


def _pane(i: int, line: str, window: int) -> dict:
    s, tool, st, title, age, extra, text = line.split("|")
    since = NOW - _age(age)
    pane = {
        "pane_id": f"%{i}", "session": SESSIONS[int(s)], "window_index": str(window),
        "window_name": title, "label": title, "tmux_label": title, "title": title,
        "tool": tool, "identity_tool": tool,
        "activity": {"run": "running", "need": "waiting"}.get(st, "idle"),
        "headline": text, "status_line": text, "session_summary": text,
        "state_since": since, "updated_at": NOW - 2, "parsed_at": NOW - 2,
        "idle_seconds": int(NOW - since) if st == "idle" else 0,
        "last_activity_at": since if st == "idle" else NOW - 3,
        "snapshot_id": None, "cwd": _where(i)[0],
        "tmux_active": title == "e2e triage", "session_active": window == 0,
        "events": [], "events_seq": len(EVENTS.get(title, [])), "agents": 0,
    }
    if tool in MODELS:
        pane |= {"model": MODELS[tool][i % len(MODELS[tool])], "context_pct": 12 + i * 37 % 70}
    if st == "need":
        pane |= {"headline": extra, "status_line": extra, "session_summary": "",
                 "waiting_on": "user",
                 "question": {"prompt": extra, "options": text.split(",") if text else [],
                              "answer_style": "text"}}
    return pane | DETAIL.get(title, {})


def fleet() -> list[dict]:
    """The /api/state panes, one per ROWS line, pane ids %1.. in row order."""
    panes, windows = [], {}
    for i, line in enumerate(ROWS.splitlines(), 1):
        s = line.split("|", 1)[0]
        windows[s] = windows.get(s, -1) + 1
        panes.append(_pane(i, line, windows[s]))
    return panes


def sign_in_pane() -> dict:
    """The add flow's sign-in window, in the first session, as tmux-rc opens it."""
    return _pane(90, "0|claude|idle|sign in: Demo|2m||Waiting for /login in the browser.", 9)


def events_log() -> dict[str, list[dict]]:
    ids = {p["title"]: p["pane_id"] for p in fleet()}
    return {ids[title]: [{"ts": NOW - m * 60, "text": t} for m, t in rows]
            for title, rows in EVENTS.items()}


def capture(pane: dict) -> str:
    """The live terminal frame: hand-made for the panes the shots show, else a plain one
    built from the card so every pane still has something to draw."""
    return CAPTURES.get(pane["title"]) or "\n".join(
        [b(pane["headline"]), "", f"{grn('dev@' + pane['session'])}$ "])


STEP = 900  # one fleet state per 15 minutes
GAPS = ((1122, 1148), (2482, 2496))  # steps before NOW: two nights the laptop slept


def _counts(i: int, n: int, rng: random.Random, walk: float) -> tuple[int, int, int, float]:
    """(panes alive, running, needs you, walk) at step i of n: more work on weekday
    afternoons, a random walk on top, and a fleet that grows from 37 panes to today's."""
    t = datetime.fromtimestamp(NOW - (n - 1 - i) * STEP, UTC)
    hour, trend = t.hour + t.minute / 60, i / n
    day = math.exp(-((hour - 14) ** 2) / 22)
    walk = walk * 0.96 + (rng.random() - 0.5) * 1.1
    run = max(0, round(3.4 + trend * 4 + (8 if t.weekday() < 5 else 3) * day + walk))
    need = max(0, round(day * 1.2 + (rng.random() - 0.55) * 2.2))  # 0 to 2
    return 37 + round(trend * 8), run, need, walk


def seed_history(history: History) -> History:
    """Fill a fresh History with HISTORY_DAYS of inventories plus the goal. The real
    recorder writes one a minute; one every COVERAGE seconds is the fewest that leave
    no false gaps."""
    panes, rng, walk = fleet(), random.Random(11), 0.0
    n = HISTORY_DAYS * 86400 // STEP
    gaps = {n - 1 - k for a, b in GAPS for k in range(a, b)}
    for i in range(n):
        alive, run, need, walk = _counts(i, n, rng, walk)
        if i in gaps:
            continue
        if i == n - 1:
            states = panes  # the newest sample is the fleet on screen now
        else:
            # Rotate who is busy every hour so per-session and per-tool slices move too.
            busy = sorted(panes[:alive],
                          key=lambda p: (int(p["pane_id"][1:]) * 17 + i // 4 * 7) % 53)
            acts = ["running"] * run + ["waiting"] * need
            states = [p | {"activity": acts[k] if k < len(acts) else "idle",
                           "waiting_on": None, "subagents": []} for k, p in enumerate(busy)]
        t0 = int(NOW) - (n - 1 - i) * STEP
        for t in range(t0, min(t0 + STEP, int(NOW) + 1), COVERAGE):
            history.record(states, "demo", now=t)
    with history.connect() as db:  # the shared goal line; inert on builds without one
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('running_goal', ?)", (str(GOAL),))
    return history


def past_windows(seconds: int, start: float) -> list[tuple[float, float, float]]:
    """(start, end, peak scale) of the windows ended before one that opened at `start`."""
    if seconds > 86400:
        return [(start - k * seconds, start - (k - 1) * seconds, 1)
                for k in range(1, USAGE_DAYS * 86400 // seconds + 2)]
    out, today = [], start - start % 86400
    for day in range(int(today), int(today) - USAGE_DAYS * 86400, -86400):
        weekend = datetime.fromtimestamp(day, UTC).weekday() >= 5
        out += [(day + h * 3600, day + h * 3600 + seconds, 0.4 if weekend else 1)
                for h in (FIVE_HOUR_STARTS[1:2] if weekend else FIVE_HOUR_STARTS)]
    return [w for w in out if w[1] <= start]


def seed_usage(usage, two: bool = False):
    """Samples of each PLANS window, ending at its share at NOW, after USAGE_DAYS of ended
    windows (past_windows): five-minute ones for the last eight days, hourly before, as
    plan_usage keeps them. A weekly window climbs a step a day, and one of Work's quotas
    resets early, falling to zero before its scheduled reset. And the accounts the panes
    draw on, as discovery would find them. Claude's last read failed, so its samples stop
    STALE seconds short of NOW."""
    rows, panes = [], fleet()
    for i, (tool, name, sessions, windows) in enumerate(plans(two)):
        last = NOW - STALE.get(tool, 0)
        for j, (window, seconds, share, pct) in enumerate(windows):
            start = NOW - share * seconds
            rows += [(tool, name, window, seconds, t,
                      min(100, round(pct * ((t - start) / (NOW - start)) ** 1.2)), start + seconds)
                     for t in range(int(last), int(start), -300)]
            days = seconds / 86400
            for k, (s, e, scale) in enumerate(past_windows(seconds, start)):
                peak = PAST_PEAKS[(k + 2 * j + 5 * i) % len(PAST_PEAKS)] * scale
                t = e - 1
                while t > s:
                    x = (t - s) / seconds
                    x -= math.sin(2 * math.pi * days * x) / (2 * math.pi * days) if days > 1 else 0
                    if (i, j, k) == (len(plans(two)) - 2, 1, 1) and x > 0.6:  # Work: early reset
                        x = (x - 0.6) / 2
                    rows.append((tool, name, window, seconds, t,
                                 min(100, round(peak * x ** 1.2)), e))
                    t -= 300 if t > NOW - 8 * 86400 else 3600
        # "short": what a base tree from before accounts.py labels a meter by.
        usage.accounts[(tool, name)] = {"homes": [name], "short": PROFILES[name][0], "panes": [
            p["pane_id"] for p in panes if p["tool"] == tool and p["session"] in sessions],
            "error": "unavailable" if tool in STALE else None}
    usage.history.record_usage(rows)
    return usage


# Open loops: example-org's pull requests and stray worktrees, as the GitHub query and the
# worktree scan would leave them. repo#number | title | hours since the last update |
# overrides of the defaults below (an open PR of yours, reviewer asked, green, to main).
LOOPS = """\
shop-web#412|Stabilize checkout.spec under slow iframes|1|
shop-api#409|Structured money type for order totals|2|decision=APPROVED
shop-api#410|Migrate API clients to the money v3 type|1|base=feat/409 stacked=1
shop-api#401|Order totals carry their currency|5|state=MERGED merged=5
shop-api#407|Idempotency keys for refunds|3|review=rin:CHANGES_REQUESTED:3
infra#77|Move the build cache to the new bucket|20|author=rin asked=1 label=build-cache
ops#33|Cache warmer reads the bucket from config|120|mergeable=CONFLICTING label=build-cache
docs#58|Release notes for 0.15|30|reviewers=0
shop-web#405|Coupon field keeps focus on error|26|checks=FAILURE draft=1
shop-web#398|Coupon API client|40|base=feat/coupon-api stacked=1 base_merged=1"""
LOOP_TREES = [("/work/research/.worktrees/tokenizer-v2", "research", "tokenizer-v2", 0, 3),
              ("/work/ci/.worktrees/flaky-quarantine", "ci", "fix/flaky-quarantine", 4, 0)]


def seed_loops(loops):
    """The cache an OpenLoops refresh would fill, so the dashboard has lanes to draw."""
    prs = []
    for line in LOOPS.splitlines():
        ref, title, hours, extra = line.split("|")
        repo, number = ref.split("#")
        o = dict(kv.split("=") for kv in extra.split())
        at, merged = NOW - int(hours) * 3600, o.get("merged")
        who, verdict, ago = (o.get("review") or "::").split(":")
        prs.append({
            "repo": f"example-org/{repo}", "number": int(number), "title": title,
            "url": f"https://{ORG}/{repo}/pull/{number}", "state": o.get("state", "OPEN"),
            "author": o.get("author", "dev"), "draft": "draft" in o, "updated_at": at,
            "merged_at": merged and NOW - int(merged) * 3600, "base": o.get("base", "main"),
            "head": f"feat/{number}", "fork": False, "stacked": "stacked" in o,
            "base_merged": "base_merged" in o,
            "mergeable": o.get("mergeable", "MERGEABLE"),
            "decision": o.get("decision", "REVIEW_REQUIRED"), "checks": o.get("checks", "SUCCESS"),
            "committed_at": at, "reviewers": int(o.get("reviewers", 1)), "asked": "asked" in o,
            "reviews": [{"by": who, "state": verdict, "at": NOW - int(ago) * 3600}] if who else [],
            "labels": [o["label"]] if "label" in o else []})
    loops.github, loops.fetched_at = {"viewer": "dev", "older": 6, "prs": prs}, NOW - 7 * 60
    loops.worktrees, loops.scanned_at = [
        {"path": path, "repo": f"example-org/{repo}", "branch": branch, "dirty": dirty,
         "unpushed": unpushed, "active_at": NOW - 2 * 86400}
        for path, repo, branch, dirty, unpushed in LOOP_TREES] + worktrees(), NOW - 7 * 60
    return loops


# Reboot restore: the workspace of a tmux server that died this morning, as the recorder
# would have saved it (openbus/restore.py), and agent-history's word on each conversation.
# session | window id, index, name | pane: tool | title | cwd | activity | cost | minutes idle
# at shutdown, or "-" for a plain shell | its conversation's state (running, or blank).
DIED = NOW - 3 * 3600
RESTORE = """\
shop-api|@1 0 api|claude|Rate-limit the public API|/work/shop-api|waiting|$3.10|4|
shop-api|@1 0 api|shell||/work/shop-api|||-|
shop-api|@2 1 checkout|codex|Fix the checkout retry loop|/work/shop-api|running|$1.45|0|
shop-api|@3 2 webhooks|claude|Answer webhooks before reserving stock|/work/shop-api|idle|$0.80|95|
research|@4 0 tokenizer|claude|Tokenizer v2 benchmarks|/work/research|idle|$6.20|5760|
research|@4 0 tokenizer|codex|Eval harness flakes|/work/research|idle|$0.35|40|running
research|@5 1 papers|claude|Survey of retrieval papers|/work/research|waiting|$2.05|12|"""


def restore_snapshot() -> tuple[dict, dict]:
    """(the snapshot's payload, agent-history's entries by session id)."""
    sessions, windows, entries = {}, {}, {}
    for n, line in enumerate(RESTORE.splitlines()):
        session, window, tool, title, cwd, activity, cost, idle, state = line.split("|")
        wid, index, name = window.split()
        s = sessions.setdefault(session, {"name": session, "path": cwd, "group": "",
                                          "windows": []})
        if not any(w["id"] == wid for w in s["windows"]):
            s["windows"].append({"id": wid, "index": int(index), "current": not s["windows"]})
        w = windows.setdefault(wid, {"name": name, "manual": True, "zoomed": False,
                                     "layout": "", "panes": []})
        shell = tool == "shell"
        at = 0 if shell else int(DIED - int(idle) * 60) // 60 * 60
        sid = None if shell else f"demo-{n}"
        w["panes"].append({"index": len(w["panes"]), "active": not w["panes"], "cwd": cwd,
                           "command": "zsh" if shell else tool,
                           "binding": sid and {"harness": tool, "session_id": sid},
                           "title": title or None, "tool": None if shell else tool,
                           "activity": activity or None, "waiting_on": None,
                           "cost": cost or None, "context_pct": None, "last_activity": at})
        if sid:
            entries[sid] = {"cwd": cwd, "resume_argv": [tool, "resume", sid],
                            "last_active": datetime.fromtimestamp(at, UTC).isoformat(),
                            "running": {"pid": 1} if state == "running" else None}
    for w in windows.values():  # 178x41, the panes side by side
        width = (178 - len(w["panes"]) + 1) // len(w["panes"])
        cells = ",".join(f"{width}x41,{i * (width + 1)},0,{i}" for i in range(len(w["panes"])))
        w["layout"] = f"0000,178x41,0,0{{{cells}}}" if len(w["panes"]) > 1 else "0000,178x41,0,0,0"
    return {"sessions": list(sessions.values()), "windows": windows}, entries


def restore_seams() -> dict:
    """What restore.offer reads besides the database, over the fleet: agent-history answers
    from it, and its invented directories pass for present. The demo server swaps these in;
    Restore and Start fresh are mutations there, answered without running."""
    entries = restore_snapshot()[1]
    return {"agent_history": SimpleNamespace(reconcile=lambda: None, get_many=lambda ids: {
                i: entries[i] for i in ids if i in entries}),
            "os": SimpleNamespace(path=SimpleNamespace(
                isdir=lambda d: d.startswith("/work/"), basename=os.path.basename))}


def seed_restore(history: History) -> History:
    """The dead server's row; its id's boot never matches this machine's, so it reads dead."""
    payload, _ = restore_snapshot()
    with history.connect() as db:
        db.execute("INSERT INTO layout_snapshots VALUES (?, ?, ?, NULL)",
                   ("demo-boot:4242:1", DIED, json.dumps(payload)))
    return history


# The scratch gallery (/scratch/): folder | minutes since created, last changed | pane, or
# "-" for none | title, or "-" to fall back on the page's own | description. A pane the
# fleet lacks has closed; its folder is found in the transcript in SCRATCH_WRITERS.
SCRATCH = """\
checkout-variants|40,25|%5|Checkout flow, two variants|Single page against stepped, at \
phone width.
ab-sweep-interim|130,70|%14|Prompt A/B sweep: interim|Variant C leads after 60 of 200 runs.
release-notes-draft|190,190|%19|-|Merged PRs since v0.14, grouped by area.
cost-breakdown|1500,1380|%77|Monthly cost breakdown|Compute, storage and egress by team.
latency-heatmap|4400,4300|-|-|-"""
SCRATCH_WRITERS = {"cost-breakdown": {"harness": "claude", "session_id": "demo-cost",
                                      "source": "~/.claude/projects/ops/demo-cost.jsonl",
                                      "title": "Billing export analysis"}}


def _thumb(path, hue: int) -> None:
    """A stand-in thumbnail: a page's header, a heading and blocks of content, in one hue."""
    im = Image.new("RGB", (480, 300), "#f5f7f6")
    d, ink = ImageDraw.Draw(im), f"hsl({hue}, 45%, 45%)"
    d.rectangle((0, 0, 480, 36), fill=f"hsl({hue}, 35%, 88%)")
    d.rectangle((24, 60, 260, 78), fill=ink)
    for n, width in enumerate((420, 380, 300)):
        d.rectangle((24, 98 + n * 18, 24 + width, 106 + n * 18), fill="#c8d1cc")
    for n in range(3):
        d.rounded_rectangle((24 + n * 148, 170, 156 + n * 148, 276), 8,
                            fill=f"hsl({hue + n * 25}, 40%, 70%)")
    im.save(path, "WEBP", quality=70)


def seed_scratch(root) -> None:
    """Write SCRATCH's folders: each an index.html, a thumbnail but for the oldest (its
    placeholder shows), and, when it names a pane, meta.json."""
    lines = SCRATCH.splitlines()
    for n, line in enumerate(lines):
        name, ages, pane, title, description = line.split("|")
        created, changed = (NOW - int(m) * 60 for m in ages.split(","))
        folder = root / name
        folder.mkdir()
        page = name.replace("-", " ").capitalize()
        (folder / "index.html").write_text(f"<title>{page}</title><h1>{page}</h1>")
        meta = folder / "meta.json"
        if pane != "-":
            meta.write_text(json.dumps({
                "title": "" if title == "-" else title, "description": description,
                "pane": pane, "created": datetime.fromtimestamp(created, UTC).isoformat()}))
        if n < len(lines) - 1:
            _thumb(folder / "thumb.webp", 150 + n * 47)
        for path in folder.iterdir():
            os.utime(path, (created, changed if path.suffix != ".json" else created))
