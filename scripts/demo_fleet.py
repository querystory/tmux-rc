"""A fictional tmux fleet for screenshots: README images, docs, and before/after diffs.

Everything here is invented — example-org, its sessions, every summary and URL — so a
screenshot taken against it can be published as-is. tests/test_demo_fleet.py keeps it that
way by failing on anything that looks like a real path, account, or credential.

Time is frozen at NOW: every timestamp is an offset from it, and the screenshot browser's
clock is pinned to the same instant, so "3m ago" and the chart's x-axis never drift.
History is generated from a seed, not stored, so it costs a few lines instead of megabytes.
"""
from __future__ import annotations

import math
import random
from datetime import UTC, datetime

from openbus.history import COVERAGE, History

NOW = datetime(2026, 6, 16, 15, 30, tzinfo=UTC).timestamp()  # a Tuesday afternoon
HISTORY_DAYS = 37
GOAL = 12  # the shared "running agents" goal line
ORG = "github.com/example-org"
SESSIONS = ["shop-api", "ci", "research", "docs", "ops", "infra", "marketing", "misc"]

# session index | tool | run / need / idle | title | age | u = finished recently, or the
# question when need | the activity line, or the reply options when need.
ROWS = """\
0|claude|idle|fact checker agent|2m||Waiting for the preview build after updating review docs.
0|codex|run|migration planner|now||Mapping table dependencies before proposing an order.
0|opencode|run|schema reviewer|now||Checking the new column constraints against live data.
0|claude|need|api contract diff|3m|Two endpoints changed. Bump the API version?|Bump to v3,Keep v2
0|claude|idle|preview deploy|2h||Deployed. Link posted in the PR thread.
0|shell|idle|zsh ~/src/shop-api|41m||git status: clean, on main.
0|codex|idle|seed data refresh|5h||Refreshed 12 fixtures. Tests green.
1|claude|need|flaky test hunter|4m|Two reruns disagree. Which suite to quarantine?|e2e,integration
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
4|claude|need|alert tuning|11m|Silence the disk alert on build-02 for 24 hours?|Silence 24h,Keep
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
7|codex|need|dependency audit|9m|Approve upgrading 3 packages with breaking changes?|Approve,Hold
7|claude|run|sidebar toggle|now||Wiring the group-by toggle into the sidebar.
7|shell|idle|zsh ~/src|41m||Idle at the prompt.
7|claude|idle|rss digest|5h||Summarised 12 new posts.
7|opencode|idle|invoice parser|2d||Parsed 31 invoices, 2 flagged.
7|shell|idle|dotfiles sync|3d||Synced with origin."""

MODELS = {"claude": ("Opus 5", "Sonnet 5"), "codex": ("GPT-6.1",), "gemini": ("Gemini 3.5 Pro",),
          "opencode": ("Claude Sonnet 5", "GPT-6.1"), "omp": ("GPT-6.1", "Claude Opus 5")}


# Plan limits, one Claude and one weekly-only Codex account, as a typical day reads: a
# light 5h window behind its even pace, a 7d window on pace to end in the 90s (amber), and
# Codex ahead of pace with no 5h window at all. provider | name | the sessions whose
# panes draw on it | per window: name, seconds, share of it gone at NOW, % used at NOW.
PLANS = [
    ("claude", "claude", SESSIONS, [("5h", 5 * 3600, 1 - 160 / 300, 16),
                                    ("7d", 7 * 86400, 1 - 7.5 / 168, 89)]),
    ("codex", "codex", SESSIONS, [("7d", 7 * 86400, 1 - 127 / 168, 14)]),
]


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
    "api contract diff": "\n".join([
        f"{b('●')} Compared the generated OpenAPI spec against the published v2 contract.",
        "",
        f"  {red('-')} GET /orders/{{id}}       {dim('total: number')}",
        f"  {grn('+')} GET /orders/{{id}}       {dim('total: {amount: string, currency: string}')}",
        f"  {red('-')} POST /carts/{{id}}/items {dim('returns 200 with the cart')}",
        f"  {grn('+')} POST /carts/{{id}}/items {dim('returns 201 with the line item')}",
        "",
        f"{b('●')} Both are breaking for existing clients. Two endpoints changed shape.",
        "  Bump the public API version?",
        "", RULE, "❯ ", RULE,
        f"{cyn('~/src/shop-api')} on {mag('feat/money-type')} | {blu('Sonnet 5')}",
    ]),
}

# Extra card fields for the panes the screenshots open, so every section has something.
DETAIL = {
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
                        "endpoints. Both break existing clients, so versioning needs a call.",
        "prs": [{"repo": "example-org/shop-api", "number": 409,
              "title": "Structured money type for order totals"}]},
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
# Questions answered with one keystroke rather than typed text, with the widget fields
# classify() adds to one: its raw rows and their plain restatement.
MENU = {"dependency audit": {
    "context": "Bash command\nUpgrade three packages with breaking changes\n\n"
               "npm install react@20 vite@8 eslint@10",
    "ask": "The agent wants to upgrade React, Vite and ESLint to new major versions. "
           "Continue?",
}}

# Activity feeds for the panes the screenshots open: (minutes before NOW, text).
EVENTS = {
    "e2e triage": [(48, "Started on the checkout.spec flake"),
                   (35, "Reproduced it 3 times in 20 local runs"),
                   (22, "Traced a failing run: the iframe loads after the coupon call"),
                   (12, "Swapped the networkidle wait for an iframe locator"),
                   (4, "Started a traced 20x rerun")],
    "api contract diff": [(30, "Generated the OpenAPI spec for the money type"),
                          (9, "Diffed it against the published v2 contract"),
                          (3, "Asked whether to bump the public API version")],
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
        "snapshot_id": None, "cwd": f"/work/{SESSIONS[int(s)]}",
        "tmux_active": title == "e2e triage", "session_active": window == 0,
        "events": [], "events_seq": len(EVENTS.get(title, [])), "agents": 0,
    }
    if tool in MODELS:
        pane |= {"model": MODELS[tool][i % len(MODELS[tool])], "context_pct": 12 + i * 37 % 70}
    if st == "need":
        pane |= {"headline": extra, "status_line": extra, "session_summary": "",
                 "waiting_on": "user",
                 "question": {"prompt": extra, "options": text.split(","),
                              "answer_style": "menu" if title in MENU else "text",
                              **MENU.get(title, {})}}
    return pane | DETAIL.get(title, {})


def fleet() -> list[dict]:
    """The /api/state panes, one per ROWS line, pane ids %1.. in row order."""
    panes, windows = [], {}
    for i, line in enumerate(ROWS.splitlines(), 1):
        s = line.split("|", 1)[0]
        windows[s] = windows.get(s, -1) + 1
        panes.append(_pane(i, line, windows[s]))
    return panes


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
    need = max(0, round(day * 2.4 + (rng.random() - 0.55) * 2.2))
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


def seed_usage(usage):
    """Five-minute samples of each PLANS window, rising a little faster late in it, ending
    at its share at NOW; and the accounts the panes draw on, as discovery would find them."""
    rows, panes = [], fleet()
    for tool, name, sessions, windows in PLANS:
        for window, seconds, share, pct in windows:
            start = NOW - share * seconds
            rows += [(tool, name, window, seconds, t,
                      round(pct * ((t - start) / (NOW - start)) ** 1.2), start + seconds)
                     for t in range(int(NOW), int(start), -300)]
        usage.accounts[(tool, name)] = {"short": name, "error": None, "panes": [
            p["pane_id"] for p in panes if p["tool"] == tool and p["session"] in sessions]}
    usage.history.record_usage(rows)
    return usage
