"""LLM-first classification, raw-JSON pipe.

The LLM is the parser AND the schema. We send the visible pane text to Gemini Flash
Lite with one layered prompt and pass its JSON straight through to the UI — no typed
schema in the middle. New prompt fields light up in the frontend with no backend
change. Research (see research/README.md) showed text-only beats image on accuracy,
cost, and latency across working / rewind-picker / error screens, so text is the
hot-path input; the image switch stays wired but off.

`classify` returns a plain dict: the model's JSON, plus watcher-managed fields
(pane_id, label, idle_seconds, snapshot_id, updated_at). On no/failed LLM it returns a
minimal dict (idle vs running) so the pipe never breaks.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import textwrap
from functools import lru_cache
from itertools import islice
from pathlib import Path

from .tmux import (
    INTERPRETERS,
    OMP_TITLE_RE,
    PLACEHOLDER_CLOSE,
    PLACEHOLDER_OPEN,
    PROMPT_GLYPHS,
    VISIBLE_SCREEN,
    Pane,
    processes,
    program,
    strip_dim,
)

# Cheap fast-path only (NOT semantic parsing): a bare shell prompt at the tail lets the
# watcher/fallback call an obviously-idle shell "idle" without an LLM call.
_SHELL_PROMPT_RE = re.compile(r"[\w.-]+@[\w.-]+.*[$#]\s*$")
_OPAQUE_SESSION_RE = re.compile(
    r"(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{16,})", re.IGNORECASE,
)
_RELATIVE_PATH_RE = re.compile(r"^(?:[^/\s]+/)+[^/\s]+$")
_CODEX_MODEL_TOKEN_RE = re.compile(r"(?:gpt-[\w.-]+|o\d[\w.-]*)", re.IGNORECASE)
_OUTPUT_LABELS = frozenset({
    "debug", "error", "footer", "info", "log", "output", "result", "session",
    "status", "title", "warn", "warning",
})
_GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# A PR named with its repository on screen: a GitHub pull/issue URL, or a standalone
# "owner/name#N" that is not the tail of a path or of another host's URL.
_QUALIFIED_PR_RE = re.compile(r"(?<![\w./:-])((?i:(?:https?://)?(?:www\.)?github\.com/))?"
                              r"([\w.-]+/[\w.-]+)(?(1)/(?:pull|issues)/|#)(\d{1,16})")
_CHECKLIST_LINE_RE = re.compile(
    # OpenCode 1.18 draws todos as [✓] done, [•] in progress, [ ] pending; its cancelled
    # ~[ ] todo~ is deliberately unmatched, since it is neither open nor finished work.
    # omp adds tree gutters and Unicode/Nerd Font/ASCII checkbox presets, not Ask radios.
    r"(?im)^[ \t│├└─|+*-]*(?:(?P<done>☑|✓|✔|\uf14a|\[[x✓]\])|☐|\uf096|\[[ •]\])"
    r"[ \t]*(?P<text>\S.*)$",
)
_OPENCODE_RUNNING_RE = re.compile(
    # OpenCode 1.18's spinner is ■/⬝ blocks, or "[⋯]" with animations off; a first Esc
    # press turns the label into "esc again to interrupt".
    r"^[ \t]*(?:[▰▱▮▯■⬝□▪▫█▓▒░]+|\[⋯\])[ \t]+esc[ \t]+(?:again[ \t]+to[ \t]+)?interrupt[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_CLAUDE_TURN_RE = re.compile(
    # Claude Code's turn status row: a live spinner ("✶ Befuddling… (1m 7s · ↓ 2k tokens)")
    # or the finished stamp ("✻ Worked for 40s · done 4:47 AM · 1 shell still running").
    # A stamp's trailing shell/monitor count is background work, not the agent's turn, and
    # a bar after it is the frame Claude Code draws down the right of its transcript.
    r"^[ \t]*[·✢✳✶✻✽][ \t]+[A-Z][\w' -]*?"
    r"(?:(?P<live>…[ \t]*\((?:\d+[hms]|esc to interrupt))| for \d+[hms][\dhms ]*(?:[·│]|$))",
    re.MULTILINE,
)
# Codex's live status row: "Working (2m 32s • esc to interrupt)", and the same row while its
# own reviewer weighs an approval: "Reviewing approval request (17m 00s • esc to interrupt)".
# Unindented, its elapsed time, and nothing under it but its own detail rows (└ the command,
# ↳ a queued message) above the input row. Codex hides it while a menu waits on the user,
# so under it nothing is asking.
_CODEX_LIVE_RE = re.compile(
    r"^(?:• )?\w[^\n(]*\(\d+[hms](?: \d+[hms])* • esc to interrupt\).*"
    r"(?=(?:\n(?:[ \t]+[└↳⌥].*|[ \t]*))*\n[ \t]*›)", re.MULTILINE)
# A provider error that aborted the turn, per tool: Codex's "■ Selected model is at
# capacity…" (not its "■ Conversation interrupted"), Claude Code's "⎿ API Error: 529 …".
_TURN_ERROR_RE = {
    "codex": re.compile(r"^[ \t]*■[ \t]+(?!.*\binterrupted\b)(?P<text>.*\S)", re.MULTILINE),
    "claude": re.compile(r"^[ \t]*⎿[ \t]*(?P<text>API Error\b.*\S)", re.MULTILINE),
}
# A prompt row, bare or inside Claude's box ("│ ❯ …"); with text after it, the user typed.
_PROMPT_ROW = f"^[ \\t│]*[{PROMPT_GLYPHS}]"
_USER_ROW_RE = re.compile(_PROMPT_ROW + "[ \\xa0]*[^\\s│]", re.MULTILINE)
# Rows whose text is never the agent asking: the user's own (a prompt row), and file
# content behind a tool's line-number gutter ("12│", "+245│" in a diff, "*65│" on a grep hit).
_NOT_ASKING_ROW_RE = re.compile(f"{_PROMPT_ROW}|^[ \\t│├└─]*[+*-]?\\d+│")
# A tabbed panel the user opened and is browsing (/config, /plugin): a strip of 3+ short
# tab labels under a dialog's top rule ("Settings  Status  Config  Usage  Stats"), a search
# box or a ❯ cursor on a "name  value" row below it, and the panel's own key-hint footer
# as the screen's last row. The agent is not asking: a row toggles a setting or opens a
# page and the panel stays. The footer closes, clears, goes back or exits on Esc, where a
# question's cancels; any other wording fails safe as a question, as does a later dialog
# below a lookalike strip. Read by structure, not one panel's exact tabs or hints, which
# Claude Code keeps rewording.
_TAB_LABEL = r"\w\S*(?: \S+)?"
_TAB_STRIP_RE = re.compile(
    rf"▔{{3,}}.*\n[ \t]*{_TAB_LABEL}(?:[ \t]{{2,}}{_TAB_LABEL}){{2,}}[ \t]*$", re.MULTILINE)
_PANEL_BODY_RE = re.compile(r"⌕|^[ \t]*❯[ \t]*\S.*?\S[ \t]{2,}\S", re.MULTILINE)
_PANEL_FOOTER_RE = re.compile(r"\bEsc to (?:close|clear|(?:go )?back|exit)\b", re.IGNORECASE)


def _tabbed_panel_open(visible: str) -> bool:
    """Whether the screen ends in an open tabbed panel (see _TAB_STRIP_RE)."""
    *_, strip = (None, *_TAB_STRIP_RE.finditer(visible))
    body = visible[strip.end():].rstrip() if strip else ""
    return bool(_PANEL_BODY_RE.search(body)
                and _PANEL_FOOTER_RE.search(body.rsplit("\n", 1)[-1]))


# A picker's highlighted row: the last prompt-glyph row below its own prompt, read past
# the frame, a radio and a digit. The model reads the highlight too but can misplace it
# (it took a ❯ user turn above Claude's digitless No/Yes box for the cursor, so a tap on
# Yes pressed Enter on No), so the screen decides wherever it shows one. Only between the
# prompt and the widget's bottom edge (a ─── rule or a ╰ corner), so neither a turn in the
# scrollback nor the input box under a picker that has closed is ever the cursor. A box
# with no highlight in it is the search field above the list (/resume, /config), not its
# edge, so it is passed over. Returns the one option the row names: itself, or itself
# ahead of a value column ("Auto-compact      true"). Only a row that opens with a frame
# loses one closing bar: a label's own bar is content.
_HIGHLIGHT_RE = re.compile(
    rf"^[ \t]*(?P<frame>│)?[ \t│]*[{PROMPT_GLYPHS}>][ \t]*(?:[○●◉◯][ \t]*)?(?:\d+[.)][ \t]+)?"
    r"(?P<label>.*?)(?(frame)[ \t]*[│┃╎])[ \t]*$", re.MULTILINE)
# A picker heading's live count ("Resume session (1 of 47)"): it redraws with every move
# and filter while the picker stays the same, so it is never part of the prompt.
_LIVE_COUNT = r"\(\d+ of \d+\)"
_LIVE_COUNT_RE = re.compile(rf"\s*{_LIVE_COUNT}\s*$")
_WIDGET_END_RE = re.compile(r"^\s*(?:(?P<rule>[─━]{3,})|╰)", re.MULTILINE)
# Codex's compact key-hint footer, led by its commit key: "enter continue · esc back".
_ENTER_FOOTER_RE = re.compile(r"\s*(?:enter \w+ · |↑/↓ Navigate · enter (?:Select|Confirm))")


def arrow_driven(question: dict, text: str) -> bool:
    """Whether numbered `question` is a picker to walk, not answer with a digit: the
    screen ends in Codex's compact key-hint footer, where a digit need not commit (its
    trust dialog takes "1" as a move to row 1), and shows the highlighted row a walk
    needs. Its approval box ("Press enter to confirm …") takes digits."""
    visible, rows = _visible(text), question.get("options")
    return (any(map(_ENTER_FOOTER_RE.match, visible.rstrip().splitlines()[-3:]))
            and isinstance(rows, list)
            and highlighted_row(visible, _question_prompt(question) or "", rows) is not None)


def _widget_end(visible: str, start: int) -> int:
    return next((edge.start() for edge in _WIDGET_END_RE.finditer(visible, start)
                 if edge["rule"] or _HIGHLIGHT_RE.search(visible, start, edge.start())),
                len(visible))


def _live_prompt(visible: str, prompt: str) -> tuple[re.Match, int] | None:
    """The live picker's prompt row, and where its widget ends, or None."""
    # A whole row only: a longer prompt ("… for deployment") is a different picker.
    found = prompt.strip() and _occurrences(prompt, visible, row=True)  # "" matches anywhere
    if not found:
        return None
    # The last match's widget is the live one, but the prompt is its first match there: a
    # later one inside the same widget is an option or footer repeating the prompt's words.
    asked = next(m for m in found if _widget_end(visible, m.end()) > found[-1].start())
    end = _widget_end(visible, asked.end())
    # Past an unframed picker's end nothing is drawn: a supported agent erases or redraws
    # a picker once it is answered, so no later one ever sits below it in its range.
    if _HIGHLIGHT_RE.search(visible, end):  # a later picker, or the input box, is live:
        return None                         # this one is history in the scrollback
    return asked, end


# A dialog's own top edge, drawn from column 0 (▔▔▔, ───, ╭): a command's rule-like row
# inside a dialog is indented, so it never stands in for the edge.
_DIALOG_TOP_RE = re.compile(r"^(?:[─━▔]{3,}|╭)", re.MULTILINE)


def _drawn_row(visible: str, live: tuple[re.Match, int]) -> str | None:
    *_, row = [None, *_HIGHLIGHT_RE.finditer(visible, live[0].end(), live[1])]
    return row and row["label"]


def picker_read(text: str, prompt: str) -> tuple[str, str | None] | None:
    """A capture's live picker, as a walk is held to it: its digest and its highlighted
    row as drawn, or None. The digest hashes from the dialog's top edge (so the command a
    generic "Do you want to proceed?" approves is part of it) down to the screen's end, as
    classify reads the viewport (_visible) but otherwise verbatim. Any change since the
    frame a step was computed from, be it a replacement picker, a moved highlight or a
    row's "30s" now "60s", refuses the step (/send), and /send hands both back after each
    key, so the next step is planned off the screen itself with no parse in between."""
    visible = _visible(text)
    live = _live_prompt(visible, prompt)
    if not live:
        return None
    *_, top = [live[0].start(), *(m.start() for m in
                                  _DIALOG_TOP_RE.finditer(visible, 0, live[0].start()))]
    return hashlib.sha256(visible[top:].encode()).hexdigest(), _drawn_row(visible, live)


def prompt_gone(text: str, prompt: str) -> bool:
    """Whether no words on the screen ask `prompt` any longer: its dialog was answered and
    erased, which needs no model to tell. Words left anywhere (an echo in the transcript)
    leave it to the parse."""
    return bool(prompt.strip()) and not _occurrences(prompt, _visible(text))


def picker_digest(text: str, prompt: str) -> str | None:
    read = picker_read(text, prompt)
    return read and read[0]


def highlighted_row(visible: str, prompt: str, options: list) -> str | None:
    """The one option the live picker's highlighted row under prompt names, or None."""
    live = _live_prompt(visible, prompt)
    label = live and _drawn_row(visible, live)
    named = label is not None and [option for option in options if isinstance(option, str)
                                   and (label == option or label.startswith(f"{option}  "))]
    return named[0] if named and len(named) == 1 else None


# The last prompt row, when empty (its greyed suggestion already stripped): an agent idle
# at its input box, so every row under it is status/footer chrome (Claude's welcome
# session list included), never a question. A real picker replaces the box with a ❯ cursor.
_EMPTY_INPUT_RE = re.compile(_PROMPT_ROW + r"[ \xa0\t│]*$(?![\s\S]*" + _PROMPT_ROW + ")",
                             re.MULTILINE)
# omp blocks that are never a live question: a completed Ask receipt, plain or boxed, from
# its "? Ask" header through its chosen radios (a pending Ask is a "╭─ Ask" dialog with no
# "?"); the queued outgoing-input bands ("Steering · 1", "After yield · 2"); and the "⎋"
# activity row, whose right-aligned session label can read like an Ask.
# Each may straddle the history boundary, which _omp_asking_view marks with a "\0" row.
_OMP_RECEIPT_RE = re.compile(r"(?m)^ ?(?:╭─+ )?\? Ask\b.*\n(?:.*\S.*\n)*")
_OMP_NOT_ASKING_RE = re.compile(
    rf"{_OMP_RECEIPT_RE.pattern}|^ ?(?:Steering|After yield) · \d+\n(?: {{3,}}.*\n|\0\n)*"
    r"|^ *⎋ .*\n",
)

# tmux's foreground executable is stronger identity evidence than any model name inside
# an agent's UI. In particular OpenCode can run Claude, GPT, or Gemini models; calling it
# Claude Code because its selected model is Claude is the category error this guard
# prevents. These exact executable names come from tmux's pane_current_command.
_PROCESS_TOOLS = {
    "claude": "claude",
    "codex": "codex",
    "gemini": "gemini",
    "agy": "antigravity",
    "antigravity": "antigravity",
    "opencode": "opencode",
    "omp": "omp",
}
# An agent installed through npm or bun runs as `node`/`bun`, so the foreground executable
# proves nothing; the agent that interpreter runs, found under the pane, does. omp's title
# (OMP_TITLE_RE) proves it outright under bun. Launched behind a wrapper (`omp …; exec
# bash`, a script, `uv run`) the foreground is a shell, and the title alone can't be
# trusted there because it outlives omp, but the title plus a live omp process under the
# pane can. Other agents get no such shell walk: without a title to corroborate it, a
# suspended or backgrounded agent under an idle shell would claim the pane.
def _programs(pid: str) -> set[str]:
    """Programs among `pid` and its descendants."""
    return {program(p) for p in processes(pid)}


def _host_tool(pane: Pane) -> str | None:
    """The agent the pane's process or title proves it is running, else None."""
    if tool := _PROCESS_TOOLS.get(pane.current_command):
        return tool
    omp_title = OMP_TITLE_RE.match(pane.title)
    hosted = pane.current_command in INTERPRETERS
    if hosted and omp_title:
        return "omp"
    if not pane.pid or not (hosted or omp_title):
        return None
    found = _programs(pane.pid) & (_PROCESS_TOOLS.keys() if hosted else {"omp"})
    return _PROCESS_TOOLS[found.pop()] if len(found) == 1 else None


# omp's status row is fixed-format chrome (status-line/metrics.ts), read here rather than
# left to the model, which drops the units: "> S0.09 (+0.18) ▶──4%──" is subscription
# spend, subagent spend, and context used; a "$" in place of "S" is metered spend.
_OMP_COST_RE = re.compile(r" > ([S$])([\d.]+)(?: \(\+([\d.]+)\))?")
_OMP_CTX_RE = re.compile(r"▶─*(\d+)%[─╎┃]")
_OMP_ELAPSED_RE = re.compile(r"^ ?\S ([\dhms ]+?) > ")  # "⠦ 14s > …" while working
_OMP_AGENTS_RE = re.compile(r"◀ 👥 (\d+)")  # its count of subagents still running


def _read_omp_row(result: dict, visible: str) -> None:
    """cost, context, working time and running subagents off omp's status row (the one
    with its context bar)."""
    *_, row = ["", *(line for line in visible.splitlines() if _OMP_CTX_RE.search(line))]
    if m := _OMP_COST_RE.search(row):
        unit, spend, sub = m.groups()
        result["cost"] = f"${spend}" + (f" (+${sub})" if sub else "") + (" (sub)" * (unit == "S"))
    if m := _OMP_CTX_RE.search(row):
        result["context_pct"] = int(m[1])
    if m := _OMP_AGENTS_RE.search(row):
        result["agents"] = int(m[1])
    if m := _OMP_ELAPSED_RE.match(row):
        working = result.get("working")
        result["working"] = {**(working if isinstance(working, dict) else {}), "elapsed": m[1]}


# Claude Code's agent list (under "● main") shows a workflow as fixed-format chrome, read
# here rather than left to the model: " ○  docs-sweep  ▰▰▱▱  1/2 · 3m37s · ↓ 109.1k tokens".
# Only rows in that live list count: the "● main" line and the ○/◯/● rows under it, which
# Claude draws as the last thing on screen, below its input box and footer. A row, or a
# whole list, quoted in the conversation sits above the input box, so it never matches.
# A long list ends with an "↓ 2 more" line.
_AGENT_LIST_RE = re.compile(
    r"(?:^|\n) *● +main *\n((?: *(?:[○◯●] |[↑↓] \d+ more)[^\n]*(?:\n|\Z))*)\s*\Z")
_WORKFLOW_ROW_RE = re.compile(
    r"^ *[○◯●] +(\S+) +[▰▱]+ +((\d+)/(\d+)) · (\S+) · ↓ ?([\d.]+\w?) tokens", re.MULTILINE)


def _read_workflow_rows(result: dict, visible: str) -> None:
    """Each workflow row is one background worker: it replaces whatever the model made of
    that row (its name as a label, or the bar mistaken for one) with the row's own read."""
    rows = [{"label": m[1], "state": "done" if m[3] == m[4] else "running",
             "progress": m[2], "elapsed": m[5], "tokens": m[6]}
            for block in _AGENT_LIST_RE.findall(visible)
            for m in _WORKFLOW_ROW_RE.finditer(block)]
    if rows:
        subs, names = result.get("subagents"), {r["label"] for r in rows}

        def theirs(a):  # the model's read of a workflow row: its exact name, or the bar
            label = str(a.get("label")) if isinstance(a, dict) else ""
            return label in names or bool({"▰", "▱"} & set(label))
        result["subagents"] = [a for a in (subs if isinstance(subs, list) else [])
                               if not theirs(a)] + rows


def _checklist_text(text: str) -> str:
    """Normalize visible and model-returned task labels for conservative matching."""
    return " ".join(text.split()).casefold()


def _opencode_running(text: str) -> bool:
    """Recognize OpenCode's live interrupt row, not an older row in scrollback."""
    last = next((line for line in reversed(text.splitlines()) if line.strip()), "")
    return _OPENCODE_RUNNING_RE.fullmatch(last) is not None

# A concrete CLI section cue needs a local clarification, not more rules applied
# to every unrelated pane. The model still identifies and classifies the workers.
_BACKGROUND_TERMINALS_HINT = """
AGENT COUNTS: a background process is not a background coding agent. In particular,
Codex's "Background terminals" / "exec session" rows describe shell commands it ran,
not delegated agents. A screen containing only "exec session 1: tail -f ... (running)"
must omit subagents (or emit []). They also do not make the HOST agent's activity
"running": classify the host from its own current chrome and words (idle at an empty
input, or waiting/external when it says it is waiting for real coding agents). Only
include actual spawned-agent contexts.
"""

# The production parser prompt lives in parser_prompt.txt (a load-bearing ~120-line
# artifact — kept as its own file so it can be edited/diffed as prose, not wrangled
# inside a Python string). Stable between edits ⇒ hits Gemini's context cache.
# research/probe.py loads the SAME file so the two never drift. Re-read on mtime
# change: an import-time constant silently served STALE prompts after edits, because
# uvicorn's stat reloader only watches *.py (reload_includes needs watchfiles).
_prompts: dict[str, tuple[int, str]] = {}  # name -> (mtime_ns, text)


def _load_prompt(name: str) -> str:
    path = Path(__file__).with_name(name)
    mtime = path.stat().st_mtime_ns  # ns: coarse mtime can miss rapid edits
    cached = _prompts.get(name)
    if cached is None or cached[0] != mtime:
        # Preserve the exact candidate bytes used by the eval harness. Boundary
        # whitespace changes tokenization too; production must not silently strip it.
        _prompts[name] = (mtime, path.read_text(encoding="utf-8"))
    return _prompts[name][1]


def compose_prompt(read_prompt) -> str:
    """Compose a template using fragments from the same source/revision."""
    prompt = read_prompt("parser_prompt.txt")
    for tool in ("codex", "gemini", "omp", "claude", "claude_detail"):
        marker = "{{" + tool + "}}\n"
        if marker in prompt:
            prompt = prompt.replace(marker, read_prompt(f"parser_{tool}.txt"))
    return prompt


def parser_prompt() -> str:
    return compose_prompt(_load_prompt)


def bootstrap_prompt() -> str:
    return _load_prompt("bootstrap_prompt.txt")


def _codex_model_segments(line: str) -> list[int]:
    stripped = line.strip()
    wrapped = (stripped[:1], stripped[-1:])
    if wrapped in {("{", "}"), ("'", "'"), ('"', '"')}:
        return []
    if wrapped == ("[", "]") and any(char in stripped for char in "{'\""):
        return []
    segments = [segment.strip() for segment in line.split("·")]
    label = re.match(r"^([A-Za-z][\w-]*):\s+", segments[0])
    if label and (label.group(1).islower() or label.group(1).casefold() in _OUTPUT_LABELS):
        return []
    if not any(
        re.match(r"^(?:~/|/)", segment)
        or re.search(r"(?:\bcontext\b|\bweekly\b|%)", segment, re.IGNORECASE)
        for segment in segments
    ):
        return []
    return [i for i, segment in enumerate(segments)
            if segment and _CODEX_MODEL_TOKEN_RE.fullmatch(segment.split(maxsplit=1)[0])]


def _session_chrome(text: str) -> list[str]:
    lines = strip_dim(text.rsplit(VISIBLE_SCREEN, 1)[-1]).splitlines()
    # The input row separates conversation output from the bottom status chrome.
    # A short capture can put quoted tool output in the last four rows too.
    input_row = max((i for i, line in enumerate(lines)
                     if re.match(r"^\s*[›❯](?:\s|$)", line)), default=-1)
    candidates = lines[max(input_row + 1, len(lines) - 4):]
    chrome = []
    status_rows = [(i, line, re.match(r"^\s*(?:~/|/)", line))
                   for i, line in enumerate(candidates)
                   if re.match(r"^\s*(?:~/|/)", line) or _codex_model_segments(line)]
    if status_rows:
        i, line, path_status = status_rows[-1]  # The bottommost recognized row is live chrome.
        if (path_status and i
                and re.match(r"^\s*[─━]+\s+\S", candidates[i - 1])):
            chrome.append(candidates[i - 1])  # Claude title immediately above status.
        chrome.append(line)
    renames = [line for line in strip_dim(text).splitlines()
               if re.match(r"^\s*[•●]\s+Thread renamed to \S", line)]
    if renames:
        chrome.append(renames[-1])
    return chrome


def _session_evidence(text: str) -> str:
    titles = []
    for line in _session_chrome(text):
        match = re.match(r"^\s*[─━]+\s+(\S.*?)\s*$", line)
        if match:
            titles.append(match.group(1))
            continue
        if models := _codex_model_segments(line):
            segments = [segment.strip() for segment in line.split("·")]
            model = models[-1]
            if model:
                titles.append(" · ".join(segments[:model]))
        match = re.match(r"^\s*[•●]\s+Thread renamed to (\S.*?)\s*$", line)
        if match:
            titles.append(match.group(1))
    return "\n".join(titles)


def _valid_session_shape(name) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    name = name.strip()
    return not (name.startswith(("~/", "/")) or _RELATIVE_PATH_RE.fullmatch(name)
                or _OPAQUE_SESSION_RE.fullmatch(name))


def _canonical_session(name, visible: str) -> str | None:
    if not _valid_session_shape(name):
        return None
    name = name.strip()
    titles = _session_evidence(visible).splitlines()
    exact = [title for title in titles if title.casefold() == name.casefold()]
    if exact:
        return exact[-1]
    pattern = r"(?<![\w/.-])" + re.escape(name) + r"(?![\w/.-])"
    matches = [title for title in titles if re.search(pattern, title)]
    return matches[0] if len(matches) == 1 else None


def _visible(text: str) -> str:
    """The live viewport as the phone renders it, minus greyed input placeholders."""
    screen = text.rsplit(VISIBLE_SCREEN, 1)[-1]
    return strip_dim(re.sub(f"{PLACEHOLDER_OPEN}.*?{PLACEHOLDER_CLOSE}", "", screen))


def _question_prompt(question) -> str | None:
    """The model question's prompt text, or None for any malformed payload."""
    prompt = question.get("prompt") if isinstance(question, dict) else None
    return prompt if isinstance(prompt, str) and prompt.strip() else None


def _occurrences(prompt: str, visible: str, *, row: bool = False) -> list[re.Match]:
    """The prompt's matches; with row, only those that are a whole row bar the frame (and
    a search field's ⌕), so the same words inside a command (`printf "Proceed?"; rm …`)
    never stand in for it. The row may close on a live count, which the model drops; any
    other words after the prompt ("… for deployment", "(production)") are another one,
    unless the prompt is a whole sentence: the model quotes the question out of a longer
    paragraph (Claude's trust dialog: "… one you trust? (Like your own code, …"), which
    the terminal may also wrap, so the prompt's row runs on past it."""
    words = r"\s+".join(map(re.escape, prompt.split()))
    if row:
        words = (rf"^[ \t│┃╎]*(?:⌕[ \t]*)?{words}(?:[ \t]*{_LIVE_COUNT})?"
                 r"(?=[ \t│┃╎]*$|(?<=[?.!])[ \t]+\S)")
    return list(re.finditer(words, visible, re.IGNORECASE | re.MULTILINE))


def _last_occurrence(prompt: str, visible: str) -> re.Match | None:
    return next(reversed(_occurrences(prompt, visible)), None)


_ROW_RE = re.compile(".*")


def _answered_prompt(question, visible: str) -> re.Match | None:
    """The row of a shell program's typed-answer prompt that it has printed output below,
    with the cursor on a blank row after that output (capture_pane's trailing empty row):
    the prompt was answered and the program moved on."""
    prompt = question.get("answer_style") == "text" and _question_prompt(question)
    found = prompt and _last_occurrence(prompt, visible)
    row = found and _ROW_RE.match(visible, visible.rfind("\n", 0, found.start()) + 1)
    below = visible[row.end():].split("\n")[1:] if row else []
    return row if any(map(str.strip, below)) and not below[-1] else None


def _omp_asking_view(text: str) -> str:
    """The viewport minus omp text that never asks. Blocks are matched across the history
    boundary (a heading may have scrolled off) but only the visible part is kept."""
    head = strip_dim(text.rpartition(VISIBLE_SCREEN)[0])
    joined = f"{head}\0\n" + _visible(text).removeprefix("\n")
    joined = _OMP_NOT_ASKING_RE.sub(lambda m: "\0" if "\0" in m[0] else "", joined)
    return joined.rsplit("\0", 1)[-1]


def _supported_question(question, text: str, tool, pane: Pane) -> bool:
    visible = _visible(text)
    prompt = _question_prompt(question)
    if prompt is None:
        return False
    if tool == "omp":
        state = (omp := OMP_TITLE_RE.match(pane.title)) and omp["state"]
        # A held Ask/approval uses "!"; working titles cannot hold an old Ask open.
        if state and state not in (">", "!"):
            return False
        if state == ">" and question.get("answer_style") in ("menu", "cursor"):
            return False
        if state == ">" and re.search(
            rf"(?m)^ {{2,}}{re.escape(prompt.strip())}\s*\n[ \t]*π >",
            visible,
        ):
            return False  # Right-aligned label above the idle footer, not assistant prose.
        visible = _omp_asking_view(text)
    found = _last_occurrence(prompt, visible)
    if tool == "shell" and _answered_prompt(question, visible):
        return False
    # The user's own turn or draft (a ❯/› row) or a gutter-numbered file line is not the
    # agent asking; a live spinner below the text means the agent is working again; and a
    # finished turn's question followed by typed input has been answered.
    idle_box = _EMPTY_INPUT_RE.search(visible)
    row = found and visible.rfind("\n", 0, found.start()) + 1
    return found is not None and not (
        (idle_box and found.start() > idle_box.start())
        or _NOT_ASKING_ROW_RE.match(visible[row:])
        or any(turn["live"] or _USER_ROW_RE.search(visible, turn.end())
               for turn in (_CLAUDE_TURN_RE.finditer(visible, found.end())
                            if tool == "claude" else ()))
        or (tool == "codex" and _CODEX_LIVE_RE.search(visible, row))  # the prompt's own row too
    )


def _supported_rewind(rewind, visible: str) -> bool:
    text = " ".join(visible.split()).casefold()
    return bool(rewind) and "rewind to a previous point" in text and "enter to restore" in text


def _ground_visible_fields(
    result: dict, text: str, pane: Pane, llm_fn, prompt: str, host_tool: str | None,
) -> None:
    """Validate actionable fields against their UI evidence, retrying once on that slice."""
    if result.get("tool") == "shell":
        result.pop("session", None)  # Old agent scrollback cannot name its replacement shell.
    visible = _visible(text)
    identity = text  # Keep the boundary: only explicit rename events may come from history.
    bad_question = (
        bool(result.get("question")) and (VISIBLE_SCREEN in text or host_tool == "omp")
        and not _supported_question(result["question"], text, result.get("tool"), pane)
    )
    bad_rewind = (
        bool(result.get("rewind")) and VISIBLE_SCREEN in text
        and not _supported_rewind(result["rewind"], visible)
    )
    session = result.get("session")
    canonical_session = _canonical_session(session, identity)
    bad_session = session is not None and canonical_session is None
    if canonical_session:
        result["session"] = canonical_session
    if not (bad_question or bad_rewind or bad_session):
        return
    # A rejected old menu can also contaminate activity/headline. Re-read only the
    # viewport; for identity alone, restrict the same model to the status evidence.
    bad_action = bad_question or bad_rewind
    identity_chrome = "\n".join(_session_chrome(identity))
    # Below an idle input box is only chrome: the re-read never sees it, so a list there
    # cannot be read as the question again.
    box = _EMPTY_INPUT_RE.search(visible)
    evidence = (visible[:box.end()] if box else visible) if bad_action else identity_chrome
    receipt = None
    if bad_question and host_tool == "omp" and (
        (omp := OMP_TITLE_RE.match(pane.title)) and omp["state"] not in (None, "!")
    ):
        # Read beyond an answered Ask receipt; any other rejected text keeps the viewport.
        asked, kind = _question_prompt(result["question"]), "Ask receipt"
        *_, receipt = [None, *(m for m in _OMP_RECEIPT_RE.finditer(visible)
                               if asked and _last_occurrence(asked, m[0]))]
    elif bad_question and result.get("tool") == "shell":
        receipt, kind = _answered_prompt(result["question"], visible), "prompt"
    if receipt:
        evidence = (
            f"[Completed {kind} — question and chosen answer explain the resumed task;\n"
            "use them for the headline's goal, NEVER as a current input request]\n"
            f"{receipt[0]}\n[Current visible work]\n{visible[receipt.end():]}"
        )
    if bad_action and bad_session and identity_chrome:
        evidence = f"{evidence}\n\n{identity_chrome}"
    retry = llm_fn(
        prompt, f"{_parser_context(pane, None, host_tool, visible)}\n\n{evidence}",
    ) if llm_fn else None
    retry = dict(retry) if isinstance(retry, dict) else None
    if retry:
        _fold_sends(retry)
    if retry and receipt and kind == "prompt":
        # The cursor rests below everything printed since the answer: nothing there asks.
        retry.pop("question", None)
    if bad_action:
        # Only the viewport re-read vouches for a link: the first read also saw history.
        # Links survive a failed re-read, which the watcher publishes on the kept card.
        result.pop("links", None)
        if retry and isinstance(retry.get("links"), list) and retry["links"]:
            result["links"] = retry["links"]
        state_fields = ("activity", "waiting_on", "headline", "question", "rewind", "offers")
        if bad_question and result.get("tool") == "omp":
            state_fields += ("tables", "tasks")
        for key in state_fields:
            result.pop(key, None)
        if retry:
            for key in state_fields:
                if key in retry:
                    result[key] = retry[key]
        else:
            result["activity"] = "unknown"
            result["parse_ok"] = False  # Do not retire this screen after a failed re-read.
        unsupported_action = (
            result.get("question")
            and not _supported_question(result["question"], text, result.get("tool"), pane)
        ) or (result.get("rewind") and not _supported_rewind(result["rewind"], visible))
        if unsupported_action:
            for key in state_fields:
                result.pop(key, None)
            result["activity"] = "unknown"
            result["parse_ok"] = False
    if bad_session:
        result.pop("session", None)
        # A missing title is safe; rejecting identity must not freeze otherwise
        # valid activity/question state behind the watcher's previous card.
        if retry and (retry_session := _canonical_session(retry.get("session"), identity)):
            result["session"] = retry_session


# A widget's own top edge (a ─── rule or a ╭ box corner), its first option row, and the
# box/gutter glyphs framing each of its rows.
_WIDGET_TOP_RE = re.compile(r"^\s*(?:[─━]{3,}|╭)")
# A menu row is its digit then "." or ")" (the parser prompt's "1. 2. 3."), the only
# shape any supported agent draws; none draws "1:" or "1 -" (no capture in the eval
# samples or tests has one). Both the widget reader and the answer frame
# (question_rows) use this one definition, so they never disagree on a menu's rows.
_OPTION_ROW = r"^([ \t│❯›>]*)(?:{n})[.)]\s"
_FIRST_OPTION_RE = re.compile(_OPTION_ROW.format(n="1"), re.MULTILINE)
_OPTION_ROW_RE = re.compile(_OPTION_ROW.format(n=r"\d+") + ".*$", re.MULTILINE)
_FRAME_RE = re.compile(r"^\s*[│┃╎]|\s+$")  # left glyph and padding: indentation is content
_RULE_ROW_RE = re.compile(r"^\s*[─━╌┄]{3,}\s*$")  # a separator inside the widget


def _indent(row: str) -> int:
    return len(row) - len(row.lstrip(" \t│┃╎"))


def _widget_text(prompt: str, visible: str) -> str:
    """What a menu asks about, read off its own widget: the rows from the widget's top
    edge down to the prompt (Claude's tool, description, command and any blocking notice)
    plus any between the prompt and option 1 (where Codex puts its command). Only the
    frame (its glyphs, its rules and its common margin) is stripped: every other row,
    blank ones and indentation included, is kept exactly (the viewport bounds it), since
    this is both the evidence the restatement reads and the question's identity.
    "Do you want to proceed?" alone is meaningless on a card. With no top edge close
    above, the rows there are the conversation, so none are taken."""
    found = _occurrences(prompt, visible, row=True)
    option = next(reversed(list(_FIRST_OPTION_RE.finditer(visible))), None)
    if found and option and option.start() > found[-1].start():
        # The options are the LAST option-1 row (a command's own "1. payload" sits above
        # them), and the prompt is the last row level with them: a command's rows, a
        # quoted copy of the prompt among them, are indented deeper than the widget's.
        found = [m for m in found if _indent(m.group()) == _indent(option.group())]
        end = option.start()
    else:
        end = 0  # no options below: the rows after the prompt are not its own
    if not found:
        return ""
    found = found[-1]
    above = visible[:visible.rfind("\n", 0, found.start()) + 1].splitlines()  # whole rows
    top = next((i for i in reversed(range(max(len(above) - 16, 0), len(above)))
                if _WIDGET_TOP_RE.match(above[i])), None)
    # A prompt sentence may open a longer row: the rest of it is the widget's too, kept
    # at the prompt's own margin so the frame and common margin go as on every row.
    rest, *below = visible[found.end():end].split("\n")
    rest = rest.strip(" \t│┃╎") and found.group()[:_indent(found.group())] + rest.lstrip()
    # With no edge the rows are unframed (Codex): only the terminal's padding goes, so
    # a command's own "│" or "━━━" row stays part of the identity. Framed, a rule is the
    # widget's only when the whole raw row is one ("│ ━━━" is content), and only a ╭ box
    # closes each row with a right border, exactly one.
    boxed = top is not None and "╭" in above[top]
    text = "\n".join(raw.rstrip() if top is None else "" if _RULE_ROW_RE.match(raw) else
                     _FRAME_RE.sub("", re.sub(r"[│┃╎]\s*$", "", raw) if boxed else raw)
                     for raw in [*(above[top + 1:] if top is not None else ()),
                                 rest, *below])
    return textwrap.dedent(text).strip("\n")


def question_rows(question: dict, text: str) -> str:
    """The rows a menu `question` reads off capture `text`, verbatim, so a command or an
    option that differs only in a duration or a cost still differs: from its widget's top
    edge (Claude draws ▔▔▔ or ─── from column 0; an indented rule is a command's own row)
    down to the screen's last numbered row and the rows indented under it (its wrapped
    label or description), or with no edge the whole viewport down to there. The last
    numbered row, not the question's own last option: a replacement menu with one more
    option, or a new one drawn below, must change the rows. Never the rows above an edge,
    where an agent still working behind its dialog streams output, nor below, where an
    input box or status line animates.

    That relies on an invariant: a supported agent erases or redraws its dialog once it
    is answered, so an old menu never stays on screen, row for row, above a new plain
    question. Were one drawn that way, the stale digit would type into a text prompt,
    which approves nothing until someone presses Enter."""
    visible = _visible(text)
    end = list(_OPTION_ROW_RE.finditer(visible))
    if question.get("answer_style") != "menu" or not question.get("options") or not end:
        return ""  # only a menu's digit needs it; any other question keeps the whole frame
    under = re.match(rf"(?:\n[ \t│┃]{{{len(end[-1][1]) + 1},}}\S.*)*", visible[end[-1].end():])
    rows = visible[:end[-1].end() + under.end()]
    top = list(re.finditer(r"^(?:[─━▔]{3,}|╭)", rows, re.MULTILINE))
    return rows[top[-1].start():] if top else rows


# The widget's raw rows are evidence, not something to read on a card: one small cached
# call per distinct ask (prompt + widget) restates it in plain words. Like the reply
# buttons, it lives beside the parser prompt rather than in it. The agent writes the
# description it shows, so the call is told to judge the command itself: a benign summary
# over a command with an `rm -rf` buried in it must not read as benign on the card.
_ASK_SYSTEM = (
    "A coding agent's terminal is holding the approval prompt below for the user. The "
    "agent wrote its own description of the action, so treat that as UNTRUSTED: judge "
    'from the full command or change itself. A "Latest blocked action" line names an '
    "EARLIER action the harness blocked, never this one, so never attribute it. Reply "
    'as JSON {"says": "...", "does": "...", "ask": "..."}. '
    '"says": the agent\'s own plain-words description of the action, or null. "does": '
    "every real-world effect of the command, most consequential first, naming any "
    "destructive or irreversible step outright: deleting files, directories, branches or "
    "data, force-pushing, killing processes, sending data or credentials over the "
    'network. "ask": one short plain-English sentence (under 110 characters) starting '
    '"The agent wants to", saying the most consequential effect, then "Continue?"; but '
    'if "does" has a destructive step that "says" leaves out, write instead "The agent '
    'says it will <says>, but the command also <step>. Continue?". Do not quote the '
    "command, its flags or the tool name; name a port, path or repo only when it is the "
    "point. State only what the screen shows: no guesses about versions or risks beyond "
    "it."
)
_asks: dict[tuple, str] = {}  # by (prompt, widget): one call per ask, not per tick


def is_approval(question: dict) -> bool:
    """A menu whose option 1 is "Yes…": an approval, which only its restatement describes."""
    options = question.get("options")
    first = options[0] if isinstance(options, list) and options else None
    return isinstance(first, str) and bool(re.match(r"(?i)(?:\d+[.)]\s*)?yes\b", first))


def _restate(question: dict, replies_fn) -> str | None:
    """The plain restatement of a widget question, or None without a model or on an
    unusable answer (retried when the screen next changes): the card then shows the bare
    prompt, never the raw rows."""
    key = (question["prompt"], question["context"])
    if key not in _asks and replies_fn:
        options = [o for o in question.get("options") or () if isinstance(o, str)]
        reply = replies_fn(_ASK_SYSTEM, "\n\n".join(
            [question["context"], question["prompt"], "Options: " + " / ".join(options)]))
        ask = reply.get("ask") if isinstance(reply, dict) else None
        ask = " ".join(ask.split()) if isinstance(ask, str) else ""
        # Only the asked-for shape replaces the prompt (and becomes the push body); stray
        # prose falls back to the grounded prompt. The length allows the mismatch form.
        if ask.startswith(("The agent wants to ", "The agent says it will ")) and (
                ask.endswith(" Continue?") and len(ask) <= 160 and ask.isprintable()):
            if len(_asks) > 256:
                _asks.clear()
            _asks[key] = ask
    return _asks.get(key)


_CONTEXT_ENTRY_RE = re.compile(r"(?i)\(?\d+%\s*(?:ctx|context)\b.*")
_LIST_ITEM_RE = re.compile(r"[-*•]\s+|\d+[.)]\s+")
# A box's side bars, a block quote's left bar (Claude Code draws "▎"), and the bar Claude
# Code draws down the right of its transcript.
_FRAME_BARS_RE = re.compile(r"(?m)^[ \t]*[│┃▎▍▌][ \t]?|[ \t]*[│┃][ \t]*$")


def _final_paragraph(message: str) -> str:
    """The message's last paragraph as one line: terminal wrapping splits a long question
    across rows. Reading back from the end it stops at a blank row, and at a list item (kept
    when it is the last row or the rows after it are its indented continuation)."""
    rows: list[str] = []
    for row in reversed(message.splitlines()):
        text = row.strip()
        if not text:
            if rows:
                break
            continue
        if _LIST_ITEM_RE.match(text):
            indent = len(row) - len(row.lstrip())
            if not rows or len(rows[-1]) - len(rows[-1].lstrip()) > indent:
                rows.append(row)  # the item itself, or the one the wrapped rows continue
            break
        rows.append(row)
    return " ".join(r.strip() for r in reversed(rows))


def _final_message(visible: str, tool: str) -> str | None:
    """A finished turn's final assistant message, up to its stamp or through the provider
    error that stopped it, or None while it runs. Anything typed after: the user acted."""
    error_re = _TURN_ERROR_RE[tool]
    turns = _CLAUDE_TURN_RE.finditer(visible) if tool == "claude" else ()
    stop = max([*turns, *error_re.finditer(visible)], key=re.Match.start, default=None)
    if stop is None or stop.groupdict().get("live") or _USER_ROW_RE.search(visible, stop.end()):
        return None
    end = stop.end() if stop.re is error_re else stop.start()
    # The turn's final assistant message, without the bars framing its rows: a bar left on
    # a blank row joins paragraphs, and one after a "?" hides that the row asks.
    return _FRAME_BARS_RE.sub("", visible[:end].rsplit("\n● ", 1)[-1])


@lru_cache(maxsize=256)
def _parses(command: str) -> bool:
    """Whether bash reads `command` whole (`bash -n` runs nothing): the first part of a script
    split by a blank row ("for ... in", "&&", an open quote, "f()") does not parse. Quoting
    checks alone pass grammar the shell rejects, so the shell is asked. Any diagnostic
    counts: an unterminated heredoc only warns, and exits 0."""
    try:
        checked = subprocess.run(["bash", "-n"], input=command, capture_output=True,
                                 text=True, timeout=2, check=False)
        return checked.returncode == 0 and not checked.stderr
    except (OSError, subprocess.SubprocessError):
        return False


def _quoted(text, visible: str) -> bool:
    """Whether the model's quote of the screen is on it: its exact text, the terminal's
    wraps allowed for (a wrap may fall at a space or inside a word) and the bars framing a
    transcript row ignored."""
    if not isinstance(text, str) or not text.strip():
        return False
    # One row break at most between two characters, never a blank row: a word may wrap
    # between any two of its letters, a single space is a space or a wrap, and a run of
    # spaces shows as is (a wrap there would hide its width).
    wrap = r"[ \t]*\n[ \t]*"
    exact = "".join(
        (f"(?: |{wrap})" if part == " " else re.escape(part)) if part.isspace()
        else f"(?:{wrap})?".join(map(re.escape, part))
        for part in re.split(r"( +)", text.strip()))
    return bool(re.search(exact, _FRAME_BARS_RE.sub("", visible)))


def screen_offers(offers: object, text: str) -> list[str]:
    """The model's offers that hold up on `text`, three at most: each is on the screen
    (_quoted), and a `! command` is one bash reads whole and that does not end in a line
    continuation (which bash -n accepts). Classify's gate, and the watcher's on a screen it
    does not reparse (its fingerprint ignores a duration). An offer is sent as one line, so
    a row break the model copied from a wrap is a space."""
    listed = offers if isinstance(offers, list) else ()
    return list(islice((o for o in dict.fromkeys(re.sub(r"[ \t]*\n[ \t]*", " ", o.strip())
                                                  for o in listed if isinstance(o, str))
                        if _quoted(o, _visible(text)) and not (o.startswith("! ") and (
                            o.endswith("\\") or not _parses(o[2:])))), 3))


def _final_ask(visible: str, tool: str, message: str, offers: list[str]) -> dict | None:
    """What a finished turn leaves blocked on the user: a provider error to retry, or in
    auto mode (where the agent stops only when it needs the user) a closing question or a
    `! command` handed over to run: on a row of its own, or an offer ending the message."""
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        return None
    if error := _TURN_ERROR_RE[tool].match(lines[-1]):
        return {"prompt": error["text"], "answer_style": "text", "options": ["try again"]}
    if "auto mode" not in (m[1] for m in _CLAUDE_MODE_RE.finditer(visible)):
        return None
    # An offered command ending the message hands it over, its own "?" or wrapped rows and
    # all: read on the whole last paragraph (a wrapped "* glob" row is no list item there).
    paragraph = " ".join(re.split(r"\n\s*\n", message.strip())[-1].split())
    if not any(o.startswith("! ") and paragraph.endswith(" ".join(o.split())) for o in offers):
        paragraph = _final_paragraph(message)
        if paragraph.rstrip("*_\"'`)").endswith("?"):
            return {"prompt": paragraph, "answer_style": "text"}
        handoff = next((i for i in reversed(range(len(lines))) if lines[i].startswith("! ")), None)
        if handoff is None:
            return None
        paragraph = " ".join(lines[max(handoff - 1, 0):handoff + 1])
    # Not a question: no reply buttons, since "Yes, proceed" typed back runs nothing.
    return {"prompt": paragraph, "answer_style": "text", "options": []}


def _clean_options(raw: object, *prompts: str) -> list[str]:
    """Suggested replies as buttons: strings only, short, deduped, printable (an option is
    typed into the agent's input box), none that just repeat a prompt. Callers cap the count."""
    repeats = {p.casefold() for p in prompts}
    picks: dict[str, str] = {}
    for o in raw if isinstance(raw, list) else []:
        if (isinstance(o, str) and 0 < len(o.strip()) <= 60 and o.isprintable()
                and o.strip().casefold() not in repeats):
            picks.setdefault(o.strip().lower(), o.strip())
    return list(picks.values())


def _same_question(model_q: object, ask: dict) -> dict:
    """The parser's own question when the deterministic ask that replaces it is the same
    question (one prompt contains the other), else {}: its suggested replies and the rows
    it cites are kept."""
    if not isinstance(model_q, dict) or model_q.get("answer_style", "text") != "text":
        return {}
    old, new = str(model_q.get("prompt", "")).strip(), ask["prompt"]
    return model_q if old and (
        old.casefold() in new.casefold() or new.casefold() in old.casefold()) else {}


# What kind of question it is, and so what the buttons are, is the model's call: one small
# request per distinct question, seeing only the question text. It lives here, not in the
# parser prompt. Empty means open-ended: no buttons, the free-text box is always there.
_REPLIES_SYSTEM = (
    "The coding agent just asked the user the question below. Reply as JSON "
    '{"options": [...]} with reply buttons for it: short (under 8 words), distinct, each a '
    "complete reply the user could type. Use an EMPTY list when the question is open-ended "
    "(what, which, how, a choice between alternatives, a request for content). For a "
    "yes/no question give the natural accept and decline. If it bundles several steps, "
    'add between them one "Yes, but check with me before <step>" per step, naming only '
    "steps the question names. The first option accepts; the last declines."
)
_replies: dict[str, list[str]] = {}  # by question text: one call per question, not per tick


def _reply_options(prompt: str, replies_fn) -> list[str]:
    """The model's reply buttons for `prompt`, or [] without a model or on an unusable
    answer (not cached: retried when the screen next changes or the daemon restarts, since
    an unchanged screen is never re-parsed). Push shows only the first two
    options, so the final decline moves up to second place."""
    if prompt not in _replies and replies_fn:
        reply = replies_fn(_REPLIES_SYSTEM, prompt)
        if isinstance(reply, dict) and isinstance(reply.get("options"), list):
            got = _clean_options(reply["options"], prompt)
            if len(got) > 2:
                got.insert(1, got.pop())  # the decline, before the cap can drop it
            if len(got) != 1 and (got or not reply["options"]):  # lone or all-junk: unusable
                if len(_replies) > 256:
                    _replies.clear()
                _replies[prompt] = got[:4]
    return _replies.get(prompt, [])


def _obvious_idle(text: str) -> bool:
    for ln in reversed(text.splitlines()):
        if ln.strip():
            return bool(_SHELL_PROMPT_RE.search(ln))
    return False


def _with_prior(text: str, prior: list[str]) -> str:
    """Prepend recent prior captures (oldest→newest) as labeled context, so the model
    sees the trajectory. This keeps classification STABLE when content trickles in one
    line at a time (no re-deciding from scratch each frame → no flicker) and gives it
    material to describe what JUST happened. Prompt tokens are cheap, so this is nearly
    free. The LAST block is the current screen — the one to report state for."""
    if not prior:
        return text
    blocks = [f"[earlier frame -{len(prior) - i}]\n{p}" for i, p in enumerate(prior)]
    blocks.append(f"[current frame — report state for THIS one]\n{text}")
    return "\n\n".join(blocks)


def _with_recent_events(text: str, recent: list[str]) -> str:
    """Append the events we ALREADY reported for this pane, so the model emits only
    genuinely NEW events instead of restating ongoing work in different words each
    parse. This deduplicates the activity log at the source (the model knows what it
    already said) rather than the client guessing whether two phrasings mean the same."""
    if not recent:
        return text
    already = "\n".join(f"- {e}" for e in recent[-20:])
    return (
        f"{text}\n\n[events already reported for this pane — do NOT repeat these or "
        f"restate the same action in different words; only add events for genuinely "
        f"new activity since them]\n{already}"
    )


# Claude Code's permission mode, as its status line spells it out: fixed chrome, read here
# so the model gets it as a fact.
_CLAUDE_MODE_RE = re.compile(r"^[ \t]*(?:-- INSERT --[ \t]*)?(?:⏵⏵|⏸)[ \t]*"
                             r"(auto mode|accept edits|plan mode|bypass permissions) on\b",
                             re.MULTILINE)


def claude_mode(visible: str) -> str | None:
    """The mode Claude Code's status line shows in the screen's last rows, or None."""
    *_, m = [None, *_CLAUDE_MODE_RE.finditer("\n".join(visible.rstrip().splitlines()[-4:]))]
    return m and m[1]


def _parser_context(pane: Pane, repository: str | None, host_tool: str | None,
                    visible: str = "") -> str:
    context = f"[tmux: this pane's foreground process is '{pane.current_command}'"
    if repository:
        context += f"; GitHub repository is '{repository}'"
    if mode := claude_mode(visible):
        context += f"; Claude Code's status line shows its mode: '{mode}'" + (
            " (it ends a turn only when it is done or needs the user, so a finished turn"
            " that ends asking the user something waits on them)" * (mode == "auto mode"))
    # The title's live state must reach the model before an old Ask card can override it.
    if host_tool == "omp" and (omp := OMP_TITLE_RE.match(pane.title)) and omp["state"]:
        state = omp["state"]
        state = "idle" if state == ">" else "waiting" if state == "!" else "running"
        context += f"; omp live run state is '{state}'"
    return context + "]"


def _working_prs(value, repository: str | None, text: str) -> list[dict]:
    """Validate the model's semantic PR associations before they become sticky state.

    A PR's repository must be grounded: the pane's own origin, or that exact repo and
    number written together on screen. Left to itself the model names a repo after the
    cwd folder ("qs") or pairs a bare #N with every repo the screen mentions."""
    if not isinstance(value, list):
        return []
    out = []
    seen = set()
    cited = {(m[2].lower(), int(m[3])) for m in _QUALIFIED_PR_RE.finditer(strip_dim(text))}
    own = (repository or "").lower()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("repo"), str):
            continue
        repo = item["repo"].strip()
        number = item.get("number")
        # Bound before conversion: Python rejects enormous digit strings, and the
        # browser must be able to represent the resulting identifier exactly.
        if isinstance(number, str) and len(number) <= 16 and number.isascii() and number.isdigit():
            number = int(number)
        if (
            not _GITHUB_REPOSITORY_RE.fullmatch(repo)
            or any(part in {".", ".."} for part in repo.split("/"))
            or len(repo) > 256
            or not isinstance(number, int)
            or isinstance(number, bool)
            or not 0 < number <= 2**53 - 1
        ):
            continue
        key = (repo.lower(), number)
        if key in seen or (key[0] != own and key not in cited):
            continue
        seen.add(key)
        out.append({"repo": repo, "number": number})
        if len(out) == 8:
            break
    return out


def bootstrap(
    pane: Pane, text: str, llm_fn, repository: str | None = None
) -> dict | None:
    """One-time deep read of a pane's scrollback → {name, summary, events}, seeding the
    card before live watching has accumulated anything. Events come back flagged
    historical=True (reconstructed, not observed — the UI dims them). Returns None on
    any failure so the caller can retry later."""
    payload = f"{_parser_context(pane, repository, _host_tool(pane))}\n\n{text}"
    result = llm_fn(bootstrap_prompt(), payload)
    if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
        return None
    events = [
        {"text": str(e["text"]), "historical": True}
        for e in (result.get("events") or [])
        if isinstance(e, dict) and e.get("text")
    ][:12]
    name = result.get("name")
    name = name.strip()[:60] if _valid_session_shape(name) else None
    return {
        "summary": result["summary"].strip(),
        "name": name,
        "events": events,
        "working_prs": _working_prs(result.get("working_prs"), repository, text),
    }


_FENCE_RE = re.compile(r"(?ms)^([ \t]*)(`{3,}|~{3,})[ \t]*([\w+-]*).*?\n(.*?)^[ \t]*\2[`~]*[ \t]*$")


def _code_blocks(reply: str | None) -> list[dict]:
    """Fenced blocks of a Markdown reply, newest first, each labelled by the first
    sentence of the paragraph that introduces it ("Run this on the server."), else by
    its fence language."""
    blocks, end = [], 0
    for m in _FENCE_RE.finditer(reply or ""):
        lead = " ".join(reply[end:m.start()].strip().rsplit("\n\n", 1)[-1].split())
        lead = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", lead)  # a list item's marker
        label = re.split(r"(?<=[.:!?])\s", lead, maxsplit=1)[0].rstrip(":")
        # Only the fence's own indent (a block inside a list item) is markup; any
        # indentation within the code is payload.
        text = re.sub(f"(?m)^{re.escape(m.group(1))}", "", m.group(4)).rstrip("\n")
        blocks.append({"label": label or m.group(3) or "Code", "text": text})
        end = m.end()
    return blocks[::-1]


def _from_reply(offer: str, reply: str | None) -> str:
    """The model picks what to send; the agent's reply has its exact text. An offer that a
    one-line code block of the reply begins with, word for word, is that block, whatever
    the screen cut."""
    key = " ".join(offer.split())
    lines = [(" ".join(t.split()), t) for block in _code_blocks(reply)
             if "\n" not in (t := block["text"].strip())]
    for hits in ({t for k, t in lines if k == key},  # exact, else a prefix; only one block
                 {t for k, t in lines if f"{k} ".startswith(f"{key} ")}):
        if hits:
            return hits.pop() if len(hits) == 1 else offer
    return offer


def _fold_sends(result: dict) -> None:
    """What to send is the model's call: the text it gives copyables to send here leads
    its offers, so a corrective re-read replaces both together."""
    cps, offers = result.get("copyables"), result.get("offers")
    result["offers"] = [*(c["send"] for c in (cps if isinstance(cps, list) else ())
                          if isinstance(c, dict) and isinstance(c.get("send"), str)),
                        *(offers if isinstance(offers, list) else ())]


def classify(
    pane: Pane,
    text: str,
    llm_fn=None,
    prior: list[str] | None = None,
    recent_events: list[str] | None = None,
    prev_activity: str | None = None,
    repository: str | None = None,
    replies_fn=None,
    reply: str | None = None,
) -> dict:
    """Parse `pane` into a plain dict for the UI. `llm_fn(system, text) -> dict|None`
    is the Gemini parser. `prior` = recent prior captures (continuity); `recent_events`
    = events already reported (so the model doesn't repeat them). `prev_activity` is the
    pane's last classified activity, held onto when the parse fails (see below). `reply`
    is the agent's last message from its transcript, whose code blocks become copyables.
    Returns the model's JSON with pane_id/label merged in; on no/failed LLM a minimal
    heuristic dict."""
    visible = _visible(text)
    process_tool = _host_tool(pane)
    payload = _with_recent_events(_with_prior(text, prior or []), recent_events or [])
    # Ground truth the model can't hallucinate past: tmux's foreground process for the
    # pane. Anchors tool identity when screen CONTENT mentions agents/models (a server
    # log printing gemini-… lines is not the Gemini CLI).
    payload = f"{_parser_context(pane, repository, process_tool, visible)}\n\n{payload}"
    result = None
    prompt = ""
    if llm_fn:
        prompt = parser_prompt()
        # Keep other agents' prompts stable: adding a new tool must not change their reads.
        if _host_tool(pane) == "antigravity":
            prompt += ("\nThis pane runs Antigravity CLI (agy). Include antigravity among the "
                       "allowed tool values and report tool=antigravity. The selected model "
                       "(Gemini, Argon, Claude, etc.) is its backend, not the host agent.\n")
        if re.search(
            r"(?im)^\s*(?:Background terminals\s*:|\d+ background terminals? running\b)", text,
        ):
            prompt += _BACKGROUND_TERMINALS_HINT
        result = llm_fn(prompt, payload)
    if not isinstance(result, dict):
        # A failed parse knows nothing about the screen, so it must not INVENT a state.
        # `_obvious_idle` only recognizes a bare shell prompt, so on an agent TUI it is
        # always False and the old `else "running"` fabricated "running" for every
        # failure — including the silent one that matters, a 429 returning None with
        # nothing in the log. That guess then stuck: the watcher advances the pane's
        # fingerprint before this returns, so a screen that never changes again is never
        # re-parsed, and a finished agent wore a green "Running" badge indefinitely
        # (and sorted as recent). Carrying the pane's last real classification forward
        # is the honest answer — the screen did change, but we failed to read it, so the
        # most recent thing we actually knew stays until a parse succeeds. Only when
        # there is no prior state (first sight of the pane) do we fall back to the
        # shell-prompt heuristic, and "unknown" rather than "running" when even that is
        # silent: an unknown pane reads as stale in the UI, which is what a pane we
        # cannot classify IS.
        # A bare shell prompt at the tail is a genuine READ of the screen, not a guess —
        # it is the one state this file can recognize without the model, and the reason
        # _obvious_idle exists. So it retires the screen like any successful parse.
        # Marking it a failure would strand TMUXRC_NO_LLM=1 (where every parse takes this
        # branch): the fingerprint would never be set, so every tick would count as a
        # content change — re-recording a snapshot, resetting last_activity_at, and
        # pinning idle_seconds at 0 so a pane never ages out of "Recent".
        read_it = _obvious_idle(text)
        result = {
            "tool": "shell"
            if pane.current_command in ("bash", "zsh", "sh", "fish")
            else "unknown",
            "activity": "idle" if read_it else (prev_activity or "unknown"),
        }
        if not read_it:
            # Tells the watcher this screen was never actually read, so it can leave the
            # pane's fingerprint unset and try again rather than retiring the screen.
            result["parse_ok"] = False
    # A direct agent executable is ground truth. The LLM still parses activity and the
    # selected model/provider, but may not relabel the host application from those model
    # names (OpenCode showing "Claude Opus" is still OpenCode).
    if process_tool:
        result["tool"] = process_tool
    elif pane.current_command in ("bash", "zsh", "sh", "fish") and _obvious_idle(visible):
        # A returned shell prompt is stronger evidence than an agent in history.
        if result.get("tool") != "shell":
            result["headline"] = "Shell ready for a command"
        result.update(tool="shell", activity="idle")
        result.pop("question", None)
        result.pop("rewind", None)
    _fold_sends(result)
    _ground_visible_fields(result, text, pane, llm_fn, prompt, process_tool)
    # OpenCode answer bullets and omp Ask radios are not tasks. Match genuine visible
    # checkbox rows after a bounded re-read, whose replacement fields need grounding too.
    if result.get("tool") in ("opencode", "omp") and "tasks" in result:
        # The visible marker, not the model, is authoritative for completion state.
        visible_tasks = {
            _checklist_text(match.group("text")): bool(match.group("done"))
            for match in _CHECKLIST_LINE_RE.finditer(visible)
        }
        tasks = result.get("tasks")
        validated = [
            {**task, "done": visible_tasks[_checklist_text(task["text"])]}
            for task in tasks
            if isinstance(task, dict)
            and isinstance(task.get("text"), str)
            and _checklist_text(task["text"]) in visible_tasks
        ] if isinstance(tasks, list) else []
        if validated:
            result["tasks"] = validated
        else:
            result.pop("tasks", None)
    # Apply authoritative live chrome AFTER a bounded retry can replace activity.
    if result.get("tool") == "opencode" and _opencode_running(text):
        result["activity"] = "running"
    # omp's title is its run state (title-generator.ts), and the title is itself a read,
    # so it stands even after a failed parse. "π >": the turn is over, so job rows still on
    # screen are finished history, and so are its workers (any omp still tracks stay
    # counted by its row's "👥 N"). A parsed question or rewind still decides the activity:
    # a turn that ends asking "Should I merge?" is idle to omp but a user-wait to us.
    # "π !": its ask or approval prompt, a user wait even if the parse missed the question.
    # A failed parse surfaces that wait, unless the last card was already one: that card
    # has the answer controls, so it is kept and the screen retried. Any
    # other separator is its working spinner: running, including while it waits on its own
    # jobs (a parsed question still makes a user wait below); only compacting is finer.
    omp = result.get("tool") == "omp" and OMP_TITLE_RE.match(pane.title)
    state = omp and omp["state"]
    if state == ">":
        result.pop("subagents", None)
        if not (result.get("question") or result.get("rewind")):
            result["activity"] = "idle"
            result.pop("waiting_on", None)
            result.pop("parse_ok", None)
    elif state == "!":
        result.update(activity="waiting", waiting_on="user")
        if prev_activity != "waiting":
            result.pop("parse_ok", None)
    elif state and result.get("activity") != "compacting":
        result["activity"] = "running"
        result.pop("parse_ok", None)
    *_, turn = [None, *_CLAUDE_TURN_RE.finditer(visible)]
    # Either row is itself a read of the screen, so it stands even after a failed parse.
    if result.get("tool") == "claude" and turn:
        if turn["live"] and result.get("activity") in (None, "idle", "unknown"):
            result["activity"] = "running"
            result.pop("parse_ok", None)
        elif (not turn["live"] and result.get("activity") == "running"
              and not _USER_ROW_RE.search(visible, turn.end())):  # no newer turn began
            result["activity"] = "idle"  # The turn is over; background shells don't count.
            result.pop("parse_ok", None)
    # Codex's live row is a read too: a user wait with no question under it, or an ask the
    # re-read could not place, is its turn running. A finished turn's "idle" stands.
    if (result.get("tool") == "codex" and result.get("activity") in ("waiting", "unknown", None)
            and not result.get("question") and result.get("waiting_on") != "external"
            and _CODEX_LIVE_RE.search(visible)):
        result["activity"] = "running"
        result.pop("parse_ok", None)
    # Each agent's own turn chrome only: a shell or Claude printing "■ Build failed" is not
    # a Codex error, and Claude chrome quoted inside Codex is not Codex's turn.
    # The finished turn's own chrome is authoritative over any model question: nothing was
    # typed after it, so a live menu (whose ❯/› rows would count as typed) is ruled out.
    tool = result.get("tool")
    final = tool in _TURN_ERROR_RE and _final_message(visible, tool)
    if offers := screen_offers(result.pop("offers", None), text):
        result["offers"] = offers = [_from_reply(offer, reply) for offer in offers]
    if final and (ask := _final_ask(visible, tool, final, offers)):
        if ask.get("options") == ["try again"]:
            result["headline"] = ask["prompt"]  # A stopped provider error outranks old progress.
        own = _same_question(result.get("question"), ask)
        if "options" not in ask and (
            options := _clean_options(own.get("options"), str(own.get("prompt", "")).strip(),
                                      ask["prompt"])[:4]
            or _reply_options(ask["prompt"], replies_fn)
        ):
            ask["options"] = options
        ask.update({k: own[k] for k in ("items", "subject", "link") if k in own})
        result["question"] = ask
        result.pop("parse_ok", None)  # Grounded in the turn's own chrome, not the model.
    if result.get("tool") == "claude" and _tabbed_panel_open(visible):
        # Authoritative over the model, a failed parse included: the screen is a settings
        # panel, so nothing is asked. Only a turn the model saw still running stays running
        # (not one a failed parse carried over).
        result.pop("question", None)
        if result.pop("parse_ok", True) is False or result.get("activity") != "running":
            result["activity"] = "idle"
    question = result.get("question")
    # A numbered picker driven by arrows is walked, not answered with a digit, where the
    # screen shows its highlighted row, which a walk needs.
    if (isinstance(question, dict) and question.get("answer_style") == "menu"
            and arrow_driven(question, text)):
        question.update(answer_style="cursor", keymap={
            "next": "Down", "prev": "Up", "select": "Enter", "search": False})
    # A cursor picker's advertised search binding is evidence, not a model guess.
    if isinstance(question, dict) and question.get("answer_style") == "cursor":
        keymap = question.get("keymap")
        footer = visible.splitlines()[-3:]
        if isinstance(keymap, dict) and any(re.search(
            r"(?:^|[·│])\s*Type to search(?:\s*[·│]|$)", line, re.IGNORECASE,
        ) for line in footer):
            keymap["search"] = True
        rows, asked = question.get("options"), _question_prompt(question)
        if asked:  # the walk and /send hold the prompt across moves, which redraw the count
            question["prompt"] = asked = _LIVE_COUNT_RE.sub("", asked)
        # The screen /send holds a walk's keys to, read off it and never passed through, and
        # only where it shows which row is highlighted: a colour-only highlight is invisible
        # to the digest, so no walk is held to a screen that hashes alike whichever row it is.
        question.pop("picker", None)
        if asked and isinstance(rows, list) and (row := highlighted_row(visible, asked, rows)):
            question["selected"] = rows.index(row)
            question["picker"] = picker_digest(text, asked)
    if isinstance(question, dict):
        # The widget's rows and their restatement are read here, never passed through.
        for key in ("context", "ask"):
            question.pop(key, None)
        # The rows the model cites for the question, and what they decide, must be on screen.
        items = question.pop("items", None)
        if isinstance(items, list) and (quoted := [i for i in items if _quoted(i, visible)]):
            question["items"] = quoted
        if not _quoted(question.get("subject"), visible):
            question.pop("subject", None)
        # A question with no style is answered as text (models.py).
        style, asked = question.get("answer_style", "text"), _question_prompt(question)
        # A menu answer is its row's digit, so a menu shows its prompt above its last option
        # row. A prompt only on that row ("1. Continue? [y/N]"), below it (a bare [y/N]
        # under numbered output reads a typed line), or nowhere has no digit to press, so it
        # is answered as text. The last match counts, so an older copy of the same prompt
        # vouches for nothing, and a match inside an option's words, wrapped or not ("1.
        # Repeat “Which action?”"), is the option's, not the prompt. This check, not the
        # parser prompt's "or is a y/n confirmation", is the authority: the eval measured
        # every rewording of the prompt regressing others.
        rows = list(_OPTION_ROW_RE.finditer(visible))
        # An option's words run past its row onto lines wrapped deeper than its number
        # (a boxed widget's frame glyph counted as indentation).
        words = [(r.end(1), re.compile(rf"(?:\n[ \t│┃╎]{{{len(r[1]) + 1},}}[^\s│┃╎].*)*").match(
            visible, r.end()).end()) for r in rows]
        said = [m for m in (_occurrences(asked, visible) if asked else [])
                if not any(lo < m.start() < hi for lo, hi in words)]
        if style == "menu" and (not _FIRST_OPTION_RE.search(visible) or not said
                                or said[-1].start() >= rows[-1].start()):
            # The menu's labels ("Yes") are no answer for a line prompt that reads "y":
            # without buttons the user types exactly what the screen asks for.
            style = question["answer_style"] = "text"
            question.pop("options", None)
        # Numbered menus only: a cursor picker is a plain choice, never an approval.
        if context := style == "menu" and asked and _widget_text(asked, visible):
            question["context"] = context
            # Only an approval is restated: a numbered choice such as "Which environment?"
            # keeps its own question.
            if is_approval(question) and (ask := _restate(question, replies_fn)):
                question["ask"] = ask
    # The model's own answers must agree: a question, rewind or offer it returned (and the
    # screen showed) is something for the user to answer or send, so the pane waits on the
    # user, whatever it put in "activity" (a stray "external" included).
    if result.get("question") or result.get("rewind") or result.get("offers"):
        result["activity"] = "waiting"
        result["waiting_on"] = "user"
    # waiting_on says WHOM a WAITING pane is blocked on and is meaningful only then:
    # default absent → "user" (the safe actionable default — never demote a real
    # user-wait to the busy/running treatment), and drop any stray value the model
    # emitted on a non-waiting pane so it can't leak inconsistent state to the UI.
    if result.get("activity") == "waiting":
        if result.get("waiting_on") != "external":
            result["waiting_on"] = "user"
    else:
        result.pop("waiting_on", None)
    # Count only workers observed running; waiting/idle/unknown are not active work.
    # Compacting has its own history state but still counts as busy in the dock.
    if result.get("tool") == "claude":  # its agent list's workflow rows are chrome
        _read_workflow_rows(result, visible)
    subs = result.get("subagents")
    result["agents"] = (
        sum(1 for a in subs if isinstance(a, dict) and a.get("state") in ("running", "compacting"))
        if isinstance(subs, list)
        else 0
    )
    if result.get("tool") == "omp":  # its own "👥 N" outranks the model's roster read
        _read_omp_row(result, visible)
    # Copyables carry a whole payload each (a commit message, a code block), and they
    # ride EVERY /api/state poll for as long as the screen shows them. Cap count and
    # size here — a wall-of-text screen (or a hostile pane) must not inflate the deck
    # for every client. 4000 chars is far past any realistic paste; drop rather than
    # truncate, since a silently clipped paste is worse than none (the prompt says the
    # same). Malformed entries are dropped, not repaired.
    #
    # Three details that all have to hold at once: re-emit a minimal {label, text} rather
    # than pass the model's dict through (an invented extra key would ride the wire for
    # free, and a giant label is payload too — the client's 60 is a display cap, not this
    # one); validate BEFORE the 3-item cap, since the prompt asks for the most-pasteable
    # entries first and slicing raw would let a malformed early entry burn a slot and drop
    # a good one; and when nothing survives, drop the field entirely instead of shipping
    # `copyables: []` — the prompt says omit when there's nothing, and the UI keys off
    # presence.
    #
    # And a copyable that is JUST a URL already in `links` is a DUPLICATE affordance: the
    # card renders that link as a tap-to-open chip, so a second row offering the same
    # string is noise stacked under it. The prompt says a URL belongs in "links", but
    # prose can't enforce it — here the two fields are side by side and comparable, so
    # drop the overlap in code. Only whole-URL copyables go: text that CONTAINS a link
    # (a command with a --scopes=https://… flag, a config block) is still worth copying.
    hrefs = {
        str(link["href"]).strip()
        for link in (result.get("links") or [])
        if isinstance(link, dict) and link.get("href")
    }
    # Structured tables already render this content; don't duplicate their rows as
    # a copy button just because the model also emitted the terminal's plain text.
    tables = result.get("tables")
    table_text = set()
    for table in tables if isinstance(tables, list) else []:
        rows = table.get("rows") if isinstance(table, dict) else None
        if isinstance(rows, list) and rows and all(
            isinstance(row, list) and all(isinstance(v, str) for v in row) for row in rows
        ):
            headers = table.get("headers")
            table_text.add(" ".join(" ".join(v for row in rows for v in row).split()))
            if isinstance(headers, list) and all(isinstance(value, str) for value in headers):
                table_text.add(" ".join(" ".join(
                    value for row in [headers, *rows] for value in row
                ).split()))
            table_text.update(" ".join(" ".join(row).split()) for row in rows)
    cps = result.get("copyables")
    copy_source = _FRAME_BARS_RE.sub("", visible).replace("\\\n", "")

    def _shown(text):
        # Copy whole displayed blocks/inline code, not invented summaries or
        # fragments cut out of a longer prose paragraph. Permit terminal wraps, and
        # line continuations kept as shown or joined (both sides drop them alike).
        text = text.replace("\\\n", "")
        words = r"\s+".join(re.escape(word) for word in text.split())
        return f"`{text}`" in copy_source or re.search(
            r"(?m)^[ \t]*(?:[•●›❯$][ \t]+)?" + words + r"[ \t]*$", copy_source,
        )

    def _valid(cps, *, transcript=False):
        """Validated entries, lazily — islice below stops us at 3 without validating the
        rest of a long model response on the hot /api/state path. A transcript block is
        exact by construction, so only its last three lines must be on screen: enough to
        show the reply is the one displayed (one `}` or `done` could be anything), while
        its top may have scrolled away."""
        for c in cps:
            if not isinstance(c, dict) or not isinstance(c.get("text"), str):
                continue
            # strip() not len(): whitespace-only text is nothing to paste, and the client
            # discards it anyway — dropping here keeps it off every poll for every client.
            stripped = c["text"].strip()
            if (not stripped or len(c["text"]) > 4000 or stripped in hrefs
                    or " ".join(stripped.split()) in table_text):
                continue
            lines = stripped.replace("\\\n", "").splitlines()  # as copy_source joins them
            if not _shown("\n".join(lines[-3:]) if transcript else stripped):
                continue
            yield {"label": str(c.get("label") or "")[:200], "text": c["text"]}

    # The agent's own code blocks beat the model's re-typing of them (see transcript.py);
    # the model's picks remain for shells and for replies with no code.
    good = list(islice(_valid(_code_blocks(reply), transcript=True), 3)) or (
        list(islice(_valid(cps), 3)) if isinstance(cps, list) else [])
    question = result.get("question")
    if good and isinstance(question, dict) and question.get("answer_style") in ("menu", "cursor"):
        # A held selection expects a key, not a pasted command. Keep the payload as
        # supporting question context instead of offering the wrong input affordance,
        # unless the question already carries its widget's own rows, which show it once.
        if not question.get("context"):
            result["tables"] = (tables if isinstance(tables, list) else []) + [
                {"title": c["label"], "headers": ["Context"], "rows": [[c["text"]]]}
                for c in good
            ]
        good = []
    if good:
        result["copyables"] = good
    else:
        result.pop("copyables", None)
    # The status line's model, context and cost have chips of their own; the parser copies
    # them into status_entries too ("16% ctx" beside 16% context, "$4.96" twice).
    entries = result.get("status_entries")
    if isinstance(entries, list):
        result["status_entries"] = [e for e in entries if not (
            e in (result.get("model"), result.get("cost")) or (
                result.get("context_pct") is not None and isinstance(e, str)
                and _CONTEXT_ENTRY_RE.fullmatch(e.strip())))]
    working_prs = _working_prs(
        result.get("working_prs"), repository, "\n".join([*(prior or []), text]))
    if working_prs:
        result["working_prs"] = working_prs
    else:
        result.pop("working_prs", None)
    result["pane_id"] = pane.id
    # Prefer the agent's own session name (read from the pane by the LLM, e.g.
    # "tmux-rc-dev") over the tmux-derived label — it's what the user recognizes.
    # tmux_label records what tmux said at refinement time, so the watcher's identity
    # stamp (which must reflect tmux RENAMES on idle ticks) can tell "unchanged —
    # keep the refinement" from "renamed — tmux wins" (watcher._stamp_identity).
    sess = result.get("session")
    result["label"] = (
        str(sess)[:40] if isinstance(sess, str) and sess.strip() else pane.label
    )
    result["tmux_label"] = pane.label
    return result
