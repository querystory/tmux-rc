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

import re
from itertools import islice
from pathlib import Path

from .tmux import (
    PLACEHOLDER_CLOSE,
    PLACEHOLDER_OPEN,
    PROMPT_GLYPHS,
    VISIBLE_SCREEN,
    Pane,
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
_CHECKLIST_LINE_RE = re.compile(
    # OpenCode 1.18 draws todos as [✓] done, [•] in progress, [ ] pending; its cancelled
    # ~[ ] todo~ is deliberately unmatched, since it is neither open nor finished work.
    r"(?im)^[ \t]*(?:[-*][ \t]*)?(?:(?P<done>☑|✓|✔|\[[x✓]\])|☐|\[[ •]\])"
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
    # A stamp's trailing shell/monitor count is background work, not the agent's turn.
    r"^[ \t]*[·✢✳✶✻✽][ \t]+[A-Z][\w' -]*?"
    r"(?:(?P<live>…[ \t]*\((?:\d+[hms]|esc to interrupt))| for \d+[hms][\dhms ]*(?:·|$))",
    re.MULTILINE,
)
# A provider error that aborted the turn, per tool: Codex's "■ Selected model is at
# capacity…" (not its "■ Conversation interrupted"), Claude Code's "⎿ API Error: 529 …".
_TURN_ERROR_RE = {
    "codex": re.compile(r"^[ \t]*■[ \t]+(?!.*\binterrupted\b)(?P<text>.*\S)", re.MULTILINE),
    "claude": re.compile(r"^[ \t]*⎿[ \t]*(?P<text>API Error\b.*\S)", re.MULTILINE),
}
# A prompt row, bare or inside Claude's box ("│ ❯ …"); with text after it, the user typed.
_PROMPT_ROW = f"^[ \\t│]*[{PROMPT_GLYPHS}]"
_USER_ROW_RE = re.compile(_PROMPT_ROW + "[ \\xa0]*[^\\s│]", re.MULTILINE)

# tmux's foreground executable is stronger identity evidence than any model name inside
# an agent's UI. In particular OpenCode can run Claude, GPT, or Gemini models; calling it
# Claude Code because its selected model is Claude is the category error this guard
# prevents. These exact executable names come from tmux's pane_current_command.
_PROCESS_TOOLS = {
    "claude": "claude",
    "codex": "codex",
    "gemini": "gemini",
    "opencode": "opencode",
}


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
    for tool in ("codex", "gemini", "claude", "claude_detail"):
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


def _supported_question(question, visible: str, tool) -> bool:
    prompt = question.get("prompt") if isinstance(question, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return False
    words = r"\s+".join(map(re.escape, prompt.split()))
    *_, found = [None, *re.finditer(words, visible, re.IGNORECASE)]
    # The user's own turn or draft (a ❯/› row) is not the agent asking; a live spinner
    # below the text means the agent is working again; and a finished turn's question
    # followed by typed input has been answered.
    return found is not None and not (
        re.match(_PROMPT_ROW, visible[visible.rfind("\n", 0, found.start()) + 1:])
        or any(turn["live"] or _USER_ROW_RE.search(visible, turn.end())
               for turn in (_CLAUDE_TURN_RE.finditer(visible, found.end())
                            if tool == "claude" else ()))
    )


def _supported_rewind(rewind, visible: str) -> bool:
    text = " ".join(visible.split()).casefold()
    return bool(rewind) and "rewind to a previous point" in text and "enter to restore" in text


def _ground_visible_fields(result: dict, text: str, pane: Pane, llm_fn, prompt: str) -> None:
    """Validate actionable fields against their UI evidence, retrying once on that slice."""
    if result.get("tool") == "shell":
        result.pop("session", None)  # Old agent scrollback cannot name its replacement shell.
    visible = _visible(text)
    identity = text  # Keep the boundary: only explicit rename events may come from history.
    bad_question = (
        bool(result.get("question")) and VISIBLE_SCREEN in text
        and not _supported_question(result["question"], visible, result.get("tool"))
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
    evidence = visible if bad_action else identity_chrome
    if bad_action and bad_session and identity_chrome:
        evidence = f"{evidence}\n\n{identity_chrome}"
    retry = llm_fn(prompt, f"{_parser_context(pane, None)}\n\n{evidence}") if llm_fn else None
    retry = dict(retry) if isinstance(retry, dict) else None
    if bad_action:
        state_fields = ("activity", "waiting_on", "headline", "question", "rewind")
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
            and not _supported_question(result["question"], visible, result.get("tool"))
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


_LIST_ITEM_RE = re.compile(r"[-*•]\s+|\d+[.)]\s+")


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


def _final_ask(visible: str, tool: str) -> dict | None:
    """What a finished turn leaves blocked on the user: a provider error to retry, or in
    auto mode (where the agent stops only when it needs the user) a closing question or a
    `! command` handed over to run. Anything typed after it means the user already acted."""
    error_re = _TURN_ERROR_RE[tool]
    turns = _CLAUDE_TURN_RE.finditer(visible) if tool == "claude" else ()
    stop = max([*turns, *error_re.finditer(visible)], key=re.Match.start, default=None)
    if stop is None or stop.groupdict().get("live") or _USER_ROW_RE.search(visible, stop.end()):
        return None
    end = stop.end() if stop.re is error_re else stop.start()
    message = visible[:end].rsplit("\n● ", 1)[-1]  # the turn's final assistant message
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        return None
    if error := error_re.match(lines[-1]):
        return {"prompt": error["text"], "answer_style": "text", "options": ["try again"]}
    if not re.search(r"(?m)^[ \t]*(?:-- INSERT --[ \t]*)?⏵⏵ auto mode on\b", visible):
        return None
    paragraph = _final_paragraph(message)
    handoff = next((i for i in reversed(range(len(lines))) if lines[i].startswith("! ")), None)
    if paragraph.endswith("?"):
        return {"prompt": paragraph, "answer_style": "text"}
    if handoff is None:
        return None
    return {"prompt": " ".join(lines[max(handoff - 1, 0):handoff + 1]), "answer_style": "text"}


_YES_NO_RE = re.compile(
    r"(?:should|shall|do|does|did|can|could|will|would|want|is|are|was|were|have|has|may|ok|okay)\b(?!,)",
    re.IGNORECASE,
)
# An alternative, or a wh-word anywhere (so indirect "tell me which..." asks) rules out yes/no.
_NOT_YES_NO_RE = re.compile(
    r"\b(?:or|what|which|how|why|where|when|who|whom|whose)\b", re.IGNORECASE)
_GATED_RE = re.compile(r"\bthen\s+\w|,\s+and\b", re.IGNORECASE)  # several steps, each a gate


def _model_options(model_q: object, ask: dict) -> list[str]:
    """The model's own suggested replies, kept when a deterministic ask replaces its
    question: strings only, short, deduped, none that just repeat the prompt, at most 4.
    Only for the same question (one prompt contains the other)."""
    if not isinstance(model_q, dict) or model_q.get("answer_style", "text") != "text":
        return []
    old, new = str(model_q.get("prompt", "")).strip(), ask["prompt"]
    same = old.casefold() in new.casefold() or new.casefold() in old.casefold()
    raw = model_q.get("options")
    if not old or not same or not isinstance(raw, list):
        return []
    repeats = {old.casefold(), new.casefold()}
    picks: dict[str, str] = {}
    for o in raw:
        if isinstance(o, str) and 0 < len(o.strip()) <= 60 and o.strip().casefold() not in repeats:
            picks.setdefault(o.strip().lower(), o.strip())
    return list(picks.values())[:4]


def _yes_no_options(question: dict) -> None:
    """Give an option-less prose question Yes/No buttons when its last sentence is a plain
    yes/no ask: opens with an auxiliary or modal (so never a wh-question) and offers no
    "A or B" alternatives. The buttons type the word into the agent's input box."""
    if question.get("answer_style", "text") != "text" or question.get("options"):
        return
    sentences = re.split(r"(?<=[.!?])\s+", str(question.get("prompt", "")).strip())
    last = sentences[-1].lstrip("*_\"'`(").rstrip("*_\"'`)")
    if last.endswith("?") and _YES_NO_RE.match(last) and not _NOT_YES_NO_RE.search(last):
        gated = ["Yes, but check with me first"] if _GATED_RE.search(last) else []
        question["options"] = ["Yes", "No", *gated]  # push shows only the first two


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


def _parser_context(pane: Pane, repository: str | None) -> str:
    context = f"[tmux: this pane's foreground process is '{pane.current_command}'"
    if repository:
        context += f"; GitHub repository is '{repository}'"
    return context + "]"


def _working_prs(value) -> list[dict]:
    """Validate the model's semantic PR associations before they become sticky state."""
    if not isinstance(value, list):
        return []
    out = []
    seen = set()
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
        if key in seen:
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
    payload = f"{_parser_context(pane, repository)}\n\n{text}"
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
        "working_prs": _working_prs(result.get("working_prs")),
    }


def classify(
    pane: Pane,
    text: str,
    llm_fn=None,
    prior: list[str] | None = None,
    recent_events: list[str] | None = None,
    prev_activity: str | None = None,
    repository: str | None = None,
) -> dict:
    """Parse `pane` into a plain dict for the UI. `llm_fn(system, text) -> dict|None`
    is the Gemini parser. `prior` = recent prior captures (continuity); `recent_events`
    = events already reported (so the model doesn't repeat them). `prev_activity` is the
    pane's last classified activity, held onto when the parse fails (see below). Returns
    the model's JSON with pane_id/label merged in; on no/failed LLM a minimal heuristic
    dict."""
    visible = _visible(text)
    payload = _with_recent_events(_with_prior(text, prior or []), recent_events or [])
    # Ground truth the model can't hallucinate past: tmux's foreground process for the
    # pane. Anchors tool identity when screen CONTENT mentions agents/models (a server
    # log printing gemini-… lines is not the Gemini CLI).
    payload = f"{_parser_context(pane, repository)}\n\n{payload}"
    result = None
    prompt = ""
    if llm_fn:
        prompt = parser_prompt()
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
    if process_tool := _PROCESS_TOOLS.get(pane.current_command):
        result["tool"] = process_tool
    elif pane.current_command in ("bash", "zsh", "sh", "fish") and _obvious_idle(visible):
        # A returned shell prompt is stronger evidence than an agent in history.
        if result.get("tool") != "shell":
            result["headline"] = "Shell ready for a command"
        result.update(tool="shell", activity="idle")
        result.pop("question", None)
        result.pop("rewind", None)
    # OpenCode renders ordinary answer bullets immediately above its model/footer. The
    # parser sometimes promotes those review findings to the agent's live task plan.
    # Validate each model-returned task against an actual visible checkbox/progress line,
    # rather than treating any checklist on screen as permission for an unrelated bullet
    # list. Standalone markdown checkboxes (`[ ] task`) are valid too. OpenCode's TUI runs
    # on the alternate screen, so its capture has no scrollback: `text` is the live screen.
    if result.get("tool") == "opencode" and "tasks" in result:
        # The visible marker, not the model, is authoritative for completion state.
        visible_tasks = {
            _checklist_text(match.group("text")): bool(match.group("done"))
            for match in _CHECKLIST_LINE_RE.finditer(text)
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
    _ground_visible_fields(result, text, pane, llm_fn, prompt)
    # Apply authoritative live chrome AFTER a bounded retry can replace activity.
    if result.get("tool") == "opencode" and _opencode_running(text):
        result["activity"] = "running"
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
    # Each agent's own turn chrome only: a shell or Claude printing "■ Build failed" is not
    # a Codex error, and Claude chrome quoted inside Codex is not Codex's turn.
    # The finished turn's own chrome is authoritative over any model question: nothing was
    # typed after it, so a live menu (whose ❯/› rows would count as typed) is ruled out.
    if result.get("tool") in _TURN_ERROR_RE and (
        ask := _final_ask(visible, result["tool"])
    ):
        if not ask.get("options") and (kept := _model_options(result.get("question"), ask)):
            ask["options"] = kept
        result["question"] = ask
        result.pop("parse_ok", None)  # Grounded in the turn's own chrome, not the model.
    if isinstance(result.get("question"), dict):
        _yes_no_options(result["question"])
    # A cursor picker's advertised search binding is evidence, not a model guess.
    question = result.get("question")
    if isinstance(question, dict) and question.get("answer_style") == "cursor":
        keymap = question.get("keymap")
        footer = visible.splitlines()[-3:]
        if isinstance(keymap, dict) and any(re.search(
            r"(?:^|[·│])\s*Type to search(?:\s*[·│]|$)", line, re.IGNORECASE,
        ) for line in footer):
            keymap["search"] = True
    # A detected question/rewind means the pane is waiting, regardless of what the
    # model put in "activity" — this is the one bit of logic we keep out of the model.
    # A question/rewind is a user-facing affordance, so it's a USER wait (overrides any
    # stray "external" the model emitted).
    if result.get("question") or result.get("rewind"):
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
    subs = result.get("subagents")
    result["agents"] = (
        sum(1 for a in subs if isinstance(a, dict) and a.get("state") in ("running", "compacting"))
        if isinstance(subs, list)
        else 0
    )
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
    copy_source = re.sub(r"(?m)^[ \t]*│[ \t]?|[ \t]*│[ \t]*$", "", visible)
    copy_source = copy_source.replace("\\\n", "")

    def _valid(cps):
        """Validated entries, lazily — islice below stops us at 3 without validating the
        rest of a long model response on the hot /api/state path."""
        for c in cps:
            if not isinstance(c, dict) or not isinstance(c.get("text"), str):
                continue
            # strip() not len(): whitespace-only text is nothing to paste, and the client
            # discards it anyway — dropping here keeps it off every poll for every client.
            stripped = c["text"].strip()
            if (not stripped or len(c["text"]) > 4000 or stripped in hrefs
                    or " ".join(stripped.split()) in table_text):
                continue
            # Copy whole displayed blocks/inline code, not invented summaries or
            # fragments cut out of a longer prose paragraph. Permit terminal wraps.
            words = r"\s+".join(re.escape(word) for word in stripped.split())
            if f"`{stripped}`" not in copy_source and not re.search(
                r"(?m)^[ \t]*(?:[•●›❯$][ \t]+)?" + words + r"[ \t]*$", copy_source,
            ):
                continue
            yield {"label": str(c.get("label") or "")[:200], "text": c["text"]}

    good = list(islice(_valid(cps), 3)) if isinstance(cps, list) else []
    question = result.get("question")
    if good and isinstance(question, dict) and question.get("answer_style") in ("menu", "cursor"):
        # A held selection expects a key, not a pasted command. Keep the payload as
        # supporting question context instead of offering the wrong input affordance.
        result["tables"] = (tables if isinstance(tables, list) else []) + [
            {"title": c["label"], "headers": ["Context"], "rows": [[c["text"]]]} for c in good
        ]
        good = []
    if good:
        result["copyables"] = good
    else:
        result.pop("copyables", None)
    working_prs = _working_prs(result.get("working_prs"))
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
