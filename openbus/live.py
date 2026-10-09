"""Live Mode: talk to every pane at once over a voice-model session (Gemini Live or
OpenAI Realtime — the adapters live in live_providers.py).

One WebSocket (`/api/live-mode`) per session. The browser streams mic PCM up; the
daemon owns the model connection (live_providers.py), feeds it the watcher's
always-current pane state, streams the model's voice + transcripts back, and executes
the session's tools — type_in_pane and press_key — through the same send_keys primitive
every other input path uses.
Design + prompting rationale: docs/design/live-mode.md.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import subprocess
import time
import unicodedata
import uuid
from collections import deque
from types import SimpleNamespace

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import agent_history, live_providers, llm, push, telemetry, tmux
from .classify import _load_prompt
from .expunge import codex_status_segments
from .live_chat import TURNS_KEPT, TURNS_QUEUED
from .live_providers import KEYS, LiveModel

logger = logging.getLogger(__name__)

router = APIRouter()


def enabled() -> bool:
    """Whether Live Mode (voice) is turned on. OFF by default: the voice UX is still
    being tuned, so it ships dark — the classify/marking improvements it rides in with
    (dim/placeholder markers, window_index) help the phone cards regardless. Flip on with
    TMUXRC_LIVE_MODE=1, then restart the daemon: .env is loaded once at process start
    (python-dotenv), so a StatReload does NOT re-read it — a full restart does. Read from
    os.environ per-call (not cached) so an env change is picked up without a code edit."""
    return os.environ.get("TMUXRC_LIVE_MODE", "").strip().lower() in ("1", "true", "yes", "on")


# Ambient [tmux update] messages: at most one per this many seconds, and only when the
# watcher's state_version moved (the same change signal /api/state long-polls on).
UPDATE_MIN_SECONDS = 2.5
# Screen tail sent per pane in the connect snapshot and in post-type refreshes. Enough
# to answer "what's it doing / asking"; SCREEN_BUDGET_CHARS below is what keeps N of
# these cheap — the per-pane cap alone is not.
SCREEN_TAIL_LINES = 60
SCREEN_TAIL_CHARS = 4000
# Fleet-wide ceiling on screen text in the connect snapshot. The per-pane tail is
# bounded but the fleet is not: a real 24-pane deck put ~36k tokens of screen text in
# the system instruction, and Gemini Live closes the session outright over its setup
# limit (a 1007 naming the token count) — the voice session died the moment audio
# flowed, on every reconnect, forever. Spent on the active pane first, then the panes
# most recently at work; a pane past the budget keeps its digest block (headline,
# summary, pending question) and loses only its screen text. ~24k chars ≈ 7k tokens
# leaves the rest of the window for the conversation itself.
SCREEN_BUDGET_CHARS = 24_000
# After typing, wait this long before sending the acted-on pane's fresh screen — the
# pane's app needs a beat to react before a capture shows anything new.
POST_TYPE_REFRESH_SECONDS = 1.5


class _LiveUsage:
    """A session's token Split (text/audio × in/out, plus cached input) and its cost under
    the MODEL's rate card (live_providers.LiveModel.rates) — a single blended price would
    be badly wrong because audio out is ~24× text in and cached input is ~30× cheaper
    again, and one global card would be wrong the moment a second model is on the menu.
    Providers report CONNECTION-cumulative totals (live_providers.Event.usage), so the
    last event always carries the totals and cost() is always current."""

    def __init__(self, rates: live_providers.Split) -> None:
        self.rates = rates
        self.split = live_providers.Split(*[0] * len(rates))

    def set(self, split: live_providers.Split) -> None:
        """Take the provider's latest CUMULATIVE Split — overwrite, don't sum: the last
        event of a connection carries its totals."""
        self.split = live_providers.Split(*split)

    @property
    def cached(self) -> int:
        return self.split.text_cached + self.split.audio_cached

    @property
    def audio_in(self) -> int:
        return self.split.audio_in + self.split.audio_cached

    @property
    def in_tokens(self) -> int:
        return self.split.text_in + self.split.text_cached + self.audio_in

    @property
    def out_tokens(self) -> int:
        return self.split.text_out + self.split.audio_out

    def cost(self) -> float:
        return sum(n / 1e6 * rate for n, rate in zip(self.split, self.rates, strict=True))


class _Meter:
    """Per-session metering: accumulates usage (via `usage`) and a rolling transcript of
    what was said and typed, emits an OTel record at each voice turn, and a final
    cumulative record + status-bar fold-in at session end. One per _run_session; survives
    reconnects (usage_metadata is cumulative, so a fresh connection continues the count).

    `session` is a per-session UUID — the summable key shared with emit_live's watch-time
    rounds, so a query can join a voice session's cost to its screen-view time."""

    def __init__(
        self, session: str, actor: str | None, model: LiveModel, *, text: bool = False
    ) -> None:
        self.session = session
        self.actor = actor
        self.model = model
        self.text = text  # typed turns, written replies: no mic, no playback
        self.approvals: dict[str, asyncio.Future] = {}  # proposal id -> the user's answer
        self.superseded = False  # the user typed past a card: the rest of its turn is declined
        self.push = None  # the daemon's PushManager: a card left waiting unseen notifies (_nudge)
        # When the chat last went out of view (sheet closed or page hidden), None while in view
        self.unseen_since: float | None = None
        # Pasted images by conversation-wide number, kept for every turn the chat model's next
        # request can still show: the kept history, plus the queued turns and the one being
        # answered, which are numbered here before they enter that history.
        self.image_total = 0
        self._image_turns: deque[dict[int, tuple[str, bytes]]] = deque(
            maxlen=TURNS_KEPT + TURNS_QUEUED + 1)
        self.usage = _LiveUsage(model.rates)
        self.turns = 0
        self.started = time.monotonic()
        self._lines: list[str] = []
        # Extra OTel fields a provider adapter wants folded into each record (GPT-Live
        # adds voice_seconds / usage_final / backend_model). Empty for the seam's own
        # providers, which report everything through `usage`.
        self.details: dict = {}

    def keep_images(self, images: list[tuple[str, bytes]]) -> None:
        """Number a delivered turn's images (a turn without any still ages the older ones)."""
        self._image_turns.append(
            {self.image_total + i + 1: image for i, image in enumerate(images)})
        self.image_total += len(images)

    def image(self, number) -> tuple[int, str, bytes] | None:
        """(number, mime, bytes) of that image, the latest when none is named; None when it
        is unknown, or aged out with its turn."""
        kept = {n: image for turn in self._image_turns for n, image in turn.items()}
        number = max(kept, default=None) if number is None else number
        return (number, *kept[number]) if type(number) is int and number in kept else None

    def note(self, line: str) -> None:
        """Record a transcript fragment (voice in/out, or a typed action). Bounded so a
        long session can't grow this without limit — OTel only ships the tail anyway."""
        self._lines.append(line)
        if len(self._lines) > 200:
            self._lines = self._lines[-200:]

    def _transcript(self) -> str:
        return "\n".join(self._lines)

    def end_turn(self) -> None:
        """A voice turn completed — emit its cumulative usage snapshot."""
        self.turns += 1
        self._emit(final=False)

    def finish(self) -> None:
        """Session ending — emit the final cumulative record and fold cost into the
        status-bar totals. Idempotent-safe to call once in the session's finally."""
        self._emit(final=True)
        llm.record_live_usage(
            in_tokens=self.usage.in_tokens,
            out_tokens=self.usage.out_tokens,
            cost=self.usage.cost(),
        )

    def _emit(self, *, final: bool) -> None:
        telemetry.emit_live_turn(
            session=self.session,
            actor=self.actor,
            model=self.model.model,
            provider=self.model.backend,
            in_tokens=self.usage.in_tokens,
            out_tokens=self.usage.out_tokens,
            cached_tokens=self.usage.cached,
            audio_in_tokens=self.usage.audio_in,
            audio_out_tokens=self.usage.split.audio_out,
            cost=self.usage.cost(),
            turns=self.turns,
            duration_s=time.monotonic() - self.started,
            final=final,
            transcript=self._transcript(),
            **self.details,
        )


def _screen_tail(watcher, pane_id: str) -> str:
    """Last SCREEN_TAIL_LINES of a pane's current screen, dim-MARKED — the same
    ⟪dim⟫-wrapped text the classify parser sees, so the voice model can tell draft/ghost/
    placeholder runs (composer autocomplete, unsent input) from real output instead of
    treating them as typed content. Use the stored snapshot text directly, NOT
    watcher.snapshot_text() — that one strips the markers for the phone's raw render.
    Empty if none captured yet."""
    hist = watcher.snapshots.get(pane_id) or []
    if not hist:
        return ""
    text = hist[-1]["text"] or ""
    lines = text.rstrip().splitlines()[-SCREEN_TAIL_LINES:]
    # Char-bound on a MARKER boundary — a raw slice could cut a ⟪dim⟫ token or orphan
    # a close from its open, garbling the dim signal the model relies on.
    return tmux.tail_marked("\n".join(lines), SCREEN_TAIL_CHARS)


def _fmt_age(sec: float) -> str:
    """Human idle age at the coarsest useful unit: '40s', '12m', '3h', '2d'."""
    sec = int(sec)
    if sec < 60: return f"{sec}s"
    if sec < 3600: return f"{sec // 60}m"
    if sec < 86400: return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def _labels(watcher) -> dict[str, str]:
    """Each watched pane's window label by pane id: what the feed and the audit call it."""
    return {d["pane_id"]: d.get("label") or d["pane_id"] for d in watcher.digest()}


def _pane_name(d: dict) -> str:
    """A pane's user-facing identity, `window <number> "<title>"`: how the prompt lists it
    and the model names it, so an Open button reads the same as the reply above it."""
    win = d.get("window_index")
    head = f"window {win}" if win not in (None, "") else "window"
    # Best-first name, matching the phone card: the agent's self-published title, else
    # the window label (which itself falls back to session / session:index). Collapse any
    # newlines and drop embedded quotes so a stray title can't unbalance the quoting or
    # split the heading — the model must be able to parse one clean identity per pane.
    name = d.get("title") or d.get("label")
    if name:
        name = " ".join(str(name).split()).replace('"', "")
        head += f' "{name}"'
    return head


# Words that name the kind of thing, not which one: "the auth fix window".
_FILLER = frozenset({"the", "a", "my", "window", "pane", "session", "tab"})


def _words(text: str) -> list[str]:
    # Any script's letters and digits. NFKC first: \w skips combining marks, so a decomposed
    # "Cafe\u0301" would otherwise split from the "Café" it displays as.
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold())


def _match_panes(digest: list[dict], name: str) -> tuple[list[dict], bool]:
    """The live panes a spoken window name may mean, best first, and whether the first
    clearly wins. A pane scores the query words that its title, window label, tool or cwd's
    basename (its repo, usually) holds as whole words; two adjacent words also match run
    together, so "qs linux" finds "qslinux". A "window N" in the name keeps only panes with
    that number and counts as a word they all hold. It wins alone on top with most of the
    words: a stray extra word ("slack inbox merge") still finds "slack inbox", one shared
    word decides nothing."""
    number = re.search(r"\bwindow\s+(\d+)", name, re.IGNORECASE)
    if number:
        digest = [d for d in digest if str(d.get("window_index")) == number[1]]
        name = name[:number.start()] + name[number.end():]
    query = [w for w in _words(name) if w not in _FILLER]

    def score(d: dict) -> int:
        fields = [str(d.get(k) or "") for k in ("title", "label", "tool")]
        fields.append(os.path.basename(str(d.get("cwd") or "").rstrip("/")))
        have = set(_words(" ".join(fields)))
        hit = set()
        for i, w in enumerate(query):
            if w in have:
                hit.add(i)
            if i and query[i - 1] + w in have:
                hit |= {i - 1, i}
        return len(hit) + bool(number)

    ranked = [t for t in sorted(((score(d), d) for d in digest), key=lambda t: -t[0]) if t[0]]
    clear = bool(ranked) and 2 * ranked[0][0] > len(query) + bool(number) and (
        len(ranked) == 1 or ranked[1][0] < ranked[0][0])
    return [d for _, d in ranked[:5]], clear


def _pane_block(d: dict, screen: str | None) -> str:
    """One pane's state as prompt text. `d` is a watcher.digest() entry. The heading
    leads with the user-facing identity (window number + title) and gives the internal
    pane id only as `id=%N` — the handle for tool calls, never spoken (see live_prompt)."""
    head = f"{_pane_name(d)} (id={d['pane_id']}) — {d.get('tool') or 'unknown'}"
    head += f" — {d.get('activity') or 'unknown'}"
    # Idle AGE, not just the state: "idle for 2d" and "idle for 40s" are different routing
    # candidates — the prompt tells the model a long-idle pane is rarely where a new
    # instruction is destined (see live_prompt's targeting ladder).
    idle = d.get("idle_seconds")
    if d.get("activity") == "idle" and isinstance(idle, (int, float)) and idle >= 0:
        head += f" for {_fmt_age(idle)}"
    if d.get("tmux_active"):
        head += " — ACTIVE (the pane the user is looking at; 'here'/'this' means this one)"
    parts = [f"## {head}"]
    if d.get("cwd"):
        # Keep untrusted directory names from introducing fake prompt lines.
        parts.append(f"cwd: {json.dumps(str(d['cwd']), ensure_ascii=True)}")
    if d.get("headline"):
        parts.append(f"now: {d['headline']}")
    if d.get("summary"):
        parts.append(f"recently: {d['summary']}")
    if d.get("prs"):
        refs = ", ".join(f"{p['repo']}#{p['number']}" for p in d["prs"])
        parts.append(f"PRs this pane has worked on: {refs}")
    if d.get("question"):
        parts.append(f"PENDING QUESTION: {d['question']}")
    if screen:
        parts.append(f"screen:\n{screen}")
    return "\n".join(parts)


def _pane_context(watcher, screens: str) -> str:
    """All panes' state as prompt text — the digest the phone's cards already use, plus
    each pane's current screen tail per `screens`: "all" (the connect snapshot), "active"
    (only the focused pane — enough for the agent to read what it's acting on, without
    re-streaming every screen on each state change), or "none". No LLM calls; pure reads
    of state the watcher keeps current anyway."""
    if screens not in ("all", "active", "none"):
        raise ValueError(f"bad screens mode: {screens!r}")
    digest = watcher.digest()
    tails: dict[str, str] = {}
    if screens == "active":
        for d in digest:
            if d.get("tmux_active"):
                tails[d["pane_id"]] = _screen_tail(watcher, d["pane_id"])
    elif screens == "all":
        # Allocate SCREEN_BUDGET_CHARS priority-first — the active pane, then panes at
        # work, then the least-idle — but RENDER in digest order, which is tmux's own
        # session/window order: the model's pane map must not reshuffle by activity.
        # The last pane granted may overshoot the budget by at most one tail. A tail
        # itself can run a few chars past SCREEN_TAIL_CHARS — tail_marked() prefixes a
        # marker token (⟪dim⟫/⟪placeholder⟫) when the kept text starts inside a marked
        # run — so the slack is "one tail plus a marker", still bounded and still
        # simpler than truncating mid-screen.
        def prio(d):
            return (not d.get("tmux_active"),
                    d.get("activity") == "idle",
                    d.get("idle_seconds") or 0)
        budget = SCREEN_BUDGET_CHARS
        for d in sorted(digest, key=prio):
            if budget <= 0:
                break
            t = _screen_tail(watcher, d["pane_id"])
            if t:
                tails[d["pane_id"]] = t
                budget -= len(t)
    blocks = [_pane_block(d, tails.get(d["pane_id"])) for d in digest]
    return "\n\n".join(blocks) if blocks else "(no panes)"


# Appended in a text session, whose relayed messages are labelled by how the user really
# said them. The prompt's example label is the one place the relay prefix is taught.
_TEXT_NOTE = (
    "\nThis session is typed, not spoken: the user types to you and reads your replies. "
    'Keep them short and plain. Label relayed messages "(via text)". Actions that change '
    "a pane (type_in_pane, press_key, resume_session, send_image_to_pane) are shown to the "
    "user to approve first; a declined one did not happen, so don't retry it unless the user "
    "asks. Act first, then report the outcome briefly; don't narrate what you're about to "
    "do before calling a tool. Pasted images are numbered in the turn text; to give one to "
    'an agent ("send this to window 3") use send_image_to_pane.'
)


def _system_prompt(watcher, *, text: bool = False) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M %Z")
    prompt = _load_prompt("live_prompt.txt")
    if text:
        prompt = prompt.replace("(via voice)", "(via text)") + _TEXT_NOTE
    return (
        f"{prompt}\nNow: {stamp}"
        f"\n\n# Panes (live state)\n\n{_pane_context(watcher, screens='all')}"
    )


def _audit(meter: _Meter, action: str, pane_id: str = "-", **kw) -> None:
    """A Live audit record, tagged with the voice session it came from. Everything the
    voice typed or asked for is speech: journal content only under TMUXRC_QSDEBUG."""
    telemetry.audit(
        action, pane_id, meter.actor, speech=True, session=meter.session[:64],
        model=meter.model.model, provider=meter.model.backend, **kw,
    )


async def _handle_tool_call(websocket: WebSocket, session, fc, watcher, meter: _Meter) -> None:
    """Run one tool call, audit it, and answer the model tersely. The ONE place every
    Live tool call is recorded: the handler notes what it touched in `rec` (pane, detail,
    content, ids) and the outcome comes from its answer, so no path goes unaudited — a
    call that raises (say, the socket dropping after the keys went in) is audited too,
    with whatever `rec` already shows it did."""
    started, rec = time.monotonic(), {}
    # Pin an omitted image_number to the latest image NOW: the user approves the thumbnail
    # on the card, and a turn pasted while it waits must not change which image is sent.
    call = fc
    if fc.name == "send_image_to_pane" and isinstance(fc.args, dict):
        pinned = meter.image(fc.args.get("image_number"))
        if pinned:  # a bad or aged-out number stays as asked, and is refused at dispatch
            call = SimpleNamespace(
                id=fc.id, name=fc.name, args={**fc.args, "image_number": pinned[0]})
    # The name is provider data: anything but a known string (a list is unhashable) is
    # refused here, before it can reach a set lookup.
    known = isinstance(fc.name, str) and fc.name in _TOOLS
    result = {"status": "error", "reason": "aborted"} if known else {
        "status": "rejected", "reason": "unknown tool"}
    try:
        if known:
            ok, pid = (await _approved(websocket, call, watcher, meter, rec)
                       if meter.text and fc.name in _CONSENT else (True, None))
            result = await _act(websocket, session, call, watcher, meter, rec, ok, pid)
    finally:
        _audit_call(meter, fc.name if known else "unknown_tool", result, started, rec)
    await session.send_tool_result(fc, result)


async def _act(websocket, session, call, watcher, meter: _Meter, rec: dict, ok, pid) -> dict:
    """Run an answered call, or say why not: the answer the model gets."""
    return (await _dispatch(websocket, session, call, watcher, rec, expected_pid=pid,
                            meter=meter)
            if ok else {"status": "declined", "reason": _DECLINED[rec["consent"]]})


def _audit_call(meter: _Meter, name: str, result: dict, started: float, rec: dict) -> None:
    status, reason = result["status"], result.get("reason")
    _audit(
        meter, f"live_{name}",
        outcome="ok" if status in {"done", "ok", "opened", "button_shown"}
        else f"{status}: {reason}" if reason else status,
        latency_ms=round((time.monotonic() - started) * 1000), **rec,
    )


# The tools that change a pane. In a text session each waits for the user to tap Send on
# what it would do (the control plane's risk tiers: docs/design/agentic-control-plane.md):
# a typed request is read and answered, never acted on unasked. find_sessions only reads,
# so it runs at once. Voice keeps acting directly — a tap would end hands-free use.
_CONSENT = {"type_in_pane", "press_key", "resume_session", "send_image_to_pane"}
# Typing a new message while a card waits answers it: the user moved on, so the model is
# told not to retry and to take the new turn (queued behind this one) instead.
_DECLINED = {"declined": "the user declined",
             "superseded": "the user sent a new message instead of answering; do not retry "
                           "this, answer their new message (it follows)"}
# A card whose connection dropped before the user answered (iOS drops the socket on every
# lock, the tunnel relay every hour) stays up on the phone, so the daemon keeps what Send
# needs, for the same chat reconnecting: (actor, session, proposal) -> (parked at, call,
# pid, rec, meter, watcher). Bounded in time, and in memory only: a restart expires them.
# `_answered` keeps each card's answer as long, (decided at, ok), so a tap resent because
# its "decided" died with a socket is told the answer again rather than "expired".
PARKED_SECONDS = 30 * 60
_parked: dict[tuple, tuple] = {}
_answered: dict[tuple, tuple] = {}
# Each chat's live connection, (actor, session) -> its meter: whether a parked card is in
# view again is the reconnected chat's word, not the dropped connection's (_nudge).
_chats: dict[tuple, _Meter] = {}


def _sweep() -> None:
    stale = time.monotonic() - PARKED_SECONDS
    for table in (_parked, _answered):
        for key in [k for k, v in table.items() if v[0] < stale]:
            del table[key]


def _key(meter: _Meter, proposal: str) -> tuple:
    return meter.actor, meter.session, proposal


def _park(proposal: str, entry: tuple) -> None:
    """Park a card, unanswered: an answer whose "decided" died with it never ran."""
    key = _key(entry[4], proposal)
    _answered.pop(key, None)
    _parked[key] = entry
    _sweep()  # after, so a card re-parked past its time goes, and abandoned chats can't pile up


def _unpark(meter: _Meter, proposal: str | None = None) -> list[tuple[str, tuple]]:
    """Take this chat's parked cards (just `proposal`'s, if named), dropping stale ones."""
    _sweep()
    return [(k[2], _parked.pop(k)) for k in list(_parked)
            if k[:2] == (meter.actor, meter.session) and proposal in {None, k[2]}]


def _answer(meter: _Meter, proposal: str, ok: bool | None, rec: dict) -> None:
    rec["consent"] = {True: "approved", False: "declined", None: "superseded"}[ok]
    _sweep()  # here too: a steady connection answers cards without parking or claiming any
    _answered[_key(meter, proposal)] = (time.monotonic(), ok)


async def _tell(websocket: WebSocket, meter: _Meter, proposal: str) -> None:
    """Tell the phone what became of a card nobody is waiting on: its answer, else expired
    (parked too long, or from before a restart). The client shows an answer as final only
    on this, so a reconnect can't leave a card claiming an action nobody will take."""
    done = _answered.get(_key(meter, proposal))
    await websocket.send_json({"type": "decided", "id": proposal, "ok": done[1]} if done
                              else {"type": "expired", "id": proposal})


async def _decide(
    websocket: WebSocket, meter: _Meter, proposal: str, ok: bool | None, rec: dict
) -> None:
    _answer(meter, proposal, ok, rec)
    await _tell(websocket, meter, proposal)


async def _resume(websocket: WebSocket, proposal: str, parked: tuple, ok: bool) -> None:
    """The user's answer to a parked card, run on the connection it came in on. The model
    that asked went with the old one, so the outcome reaches only the phone and the audit:
    no session, so no post-type update tells the new model of an action it never took."""
    _, call, pid, rec, meter, watcher = parked
    started, result = time.monotonic(), {"status": "error", "reason": "aborted"}
    try:
        try:
            await _decide(websocket, meter, proposal, ok, rec)
        except BaseException:  # this socket dropped too, before anything ran: keep the card
            _park(proposal, parked)
            raise
        result = await _act(websocket, None, call, watcher, meter, rec, ok, pid)
    finally:
        _audit_call(meter, call.name, result, started, rec)


async def _approved(
    websocket: WebSocket, fc, watcher, meter: _Meter, rec: dict
) -> tuple[bool, str | None]:
    """Show the user what the call would do, as the model asked it, and wait for Send or
    Cancel, or a new typed turn, which supersedes the card. Returns the answer and the
    pane's pid when it was proposed: the card named THAT process, and tmux recycles %N, so
    an approval must not type into whatever holds the id by the time the user taps. A
    malformed call can be approved and is still refused by _dispatch: this gate only ever
    removes actions."""
    args = fc.args if isinstance(fc.args, dict) else {}
    pane_id = args.get("pane_id")
    d = next((d for d in watcher.digest() if d["pane_id"] == pane_id), None)
    known, pane = d is not None, pane_id
    if known:  # named as the prompt names it, so the card matches the reply above it, plus
        # the list's label (a bare tmux address for an unnamed window) unless that IS the title
        pane, label = _pane_name(d), d.get("label") or pane_id
        pane += "" if pane.endswith(f'"{label}"') else f" ({label})"
    # "" when the lookup finds no process: it matches no pane, so the send is refused
    # rather than going out unguarded (None would mean "don't check").
    pid = (await asyncio.to_thread(tmux.pane_pid, pane_id) or "") if known else None
    if known:
        rec["pane_id"] = pane_id  # a real pane: recorded even if declined
    card = {"type": "propose", "pane_id": pane_id if known else None}  # Open
    if fc.name == "resume_session":
        entry = (agent_history.offered() and isinstance(args.get("session_id"), str) and (
            await asyncio.to_thread(agent_history.get, args["session_id"]))) or {}
        summary = f"Resume {entry.get('title') or args.get('session_id')}"
        if entry:  # titles repeat: the card also says which tool, where, when, and which id
            card["session"] = {
                "tool": entry.get("harness"), "cwd": _home_relative(entry.get("cwd") or ""),
                "last_active": entry.get("last_active"), "id": entry["session_id"][:8]}
    elif fc.name == "press_key":
        summary = f"Press {args.get('key')} in {pane}"
    elif fc.name == "send_image_to_pane":
        image = meter.image(args.get("image_number"))
        caption = args.get("caption")
        summary = f"Send image {image[0] if image else args.get('image_number')} to {pane}" + (
            f": {caption}" if caption else "")
        if image:  # the card shows what would be sent; no image: refused below, as approved
            card["image"] = f"data:{image[1]};base64,{base64.b64encode(image[2]).decode()}"
    else:  # the card must say whether approving also presses Enter (runs it)
        verb = "Type (no Enter) into" if args.get("press_enter") is False else "Send to"
        summary = f"{verb} {pane}: {args.get('text')}"
    rec["keys"] = summary  # speech, like the dispatch's own record of what it typed
    card["text"] = summary
    proposal = uuid.uuid4().hex
    meter.approvals[proposal] = answer = asyncio.get_running_loop().create_future()
    if meter.superseded:  # a later call in a turn the user already moved on from
        answer.set_result(None)
    if meter.push:  # outlives this call: a parked card still notifies
        _background(asyncio.create_task(_nudge(meter, proposal, summary)))
    try:
        await websocket.send_json({**card, "id": proposal})
        ok = await answer  # True / False on a tap, None when a new message superseded it
        await _decide(websocket, meter, proposal, ok, rec)
    except BaseException:  # the connection failed under it (a cancel or a send): nothing ran
        if answer.done() and not answer.cancelled() and answer.result() is None:
            _answer(meter, proposal, None, rec)  # superseded: replayed, and never undone by a Send
        else:
            rec["consent"] = "parked"
            # A dead socket shows nothing: out of view until the chat reconnects (_nudge).
            meter.unseen_since = meter.unseen_since or time.monotonic()
            _park(proposal, (time.monotonic(), fc, pid, rec, meter, watcher))
        raise
    finally:
        meter.approvals.pop(proposal, None)
    return rec["consent"] == "approved", pid


_NUDGE_TICK = 1.0


async def _nudge(meter: _Meter, proposal: str, text: str) -> None:
    """Push "Chat needs you" once a card has waited push.SETTLE_SECONDS with nobody looking
    at the chat (sheet minimized, page hidden, phone locked: a dropped socket) the whole
    time. It lasts as long as the card does, live or parked, so it ends with the answer, a
    supersede or expiry, and it pushes at most once."""
    shown = time.monotonic()
    while proposal in meter.approvals or _key(meter, proposal) in _parked:
        since = _chats.get((meter.actor, meter.session), meter).unseen_since
        if since is not None and time.monotonic() - max(since, shown) >= push.SETTLE_SECONDS:
            await asyncio.to_thread(meter.push.chat, text)
            return
        await asyncio.sleep(_NUDGE_TICK)


async def _dispatch(
    websocket: WebSocket, session, fc, watcher, rec: dict, *,
    expected_pid: str | None = None, meter: _Meter | None = None,
) -> dict:
    """Route a tool call (type_in_pane / press_key, or a history tool) and return its
    answer. The result NEVER rides back through the tool response (echo loops — see
    design doc); the model sees the outcome via the post-action ambient refresh instead."""
    args = fc.args if isinstance(fc.args, dict) else {}
    if fc.name in _HISTORY_TOOLS:
        if not agent_history.offered():
            return {"status": "rejected", "reason": "session history not available"}
        return await _HISTORY_TOOLS[fc.name](websocket, args, watcher, rec)
    if fc.name == "send_image_to_pane":
        return await _send_image(websocket, args, watcher, rec, expected_pid, meter)
    if fc.name == "open_pane":
        return await _open_pane(websocket, args, watcher, rec)

    # Keep the RAW value as well as the coerced one: str() turns a dict or an int into a
    # perfectly plausible-looking string, and the guards below have to reject a wrong TYPE
    # rather than silently accept its repr. A model parroting our own tool response back
    # as a new call is exactly how a dict arrives here.
    raw_pane_id = args.get("pane_id")
    pane_id = str(raw_pane_id or "").strip()
    labels = _labels(watcher)

    # Parse per-tool into (send_args for tmux.send_keys, a human "what" for the audit/feed,
    # whether it counts as submitted). malformed stays None ⇒ reject below.
    send_args = what = None
    submitted = False
    if (
        fc.name == "type_in_pane"
        and isinstance(fc.args, dict)
        and isinstance(raw_pane_id, str)
        and isinstance(args.get("text"), str)
        and not (set(args) - {"pane_id", "text", "press_enter"})
    ):
        text = args["text"]
        raw_enter = args.get("press_enter", True)
        # Never coerce press_enter: bool("false") is True and would submit an unsent
        # command. A non-bool value is malformed.
        if text.strip() and isinstance(raw_enter, bool):
            send_args = (pane_id, text, raw_enter, True)  # literal text
            what, submitted = text, raw_enter
    elif (
        fc.name == "press_key"
        and isinstance(fc.args, dict)
        and isinstance(raw_pane_id, str)
        and isinstance(args.get("key"), str)
        and not (set(args) - {"pane_id", "key"})
    ):
        key = KEYS.get(args["key"])
        if key:
            send_args = (pane_id, key, False, False)  # named key, not literal, no auto-Enter
            what, submitted = f"[{key}]", key == "Enter"

    # The payload may hold secrets: it is speech, so it reaches the journal only under
    # QSDEBUG. The pane id is recorded only once it names a real pane.
    rec["keys"] = what or str(args)
    if pane_id not in labels:
        return {"status": "rejected", "reason": "unknown pane"}
    rec["pane_id"] = pane_id
    if send_args is None:
        return {"status": "rejected", "reason": "malformed call"}

    label = labels[pane_id]
    invalidate = getattr(watcher, "invalidate_input_actions", None)
    try:
        if invalidate is not None:
            await asyncio.to_thread(tmux.before_send, pane_id, lambda: invalidate(pane_id))
        await asyncio.to_thread(tmux.send_keys, *send_args, expected_pid=expected_pid)
    except Exception as e:  # report, don't kill the session
        # The error's text can quote the typed text (send-keys argv): speech, so only
        # the class leaves here unless QSDEBUG.
        rec["detail"] = type(e).__name__
        logger.warning("[live] %s failed for %s: %s", fc.name, pane_id, rec["detail"],
                       exc_info=telemetry.QSDEBUG)
        return _send_failed(e, rec, "pane did not accept input")

    rec["detail"] = f"into {label}" + (" +enter" if submitted else "")
    watcher.request_reparse(pane_id)
    # Every action the voice takes is visibly logged in the overlay.
    await websocket.send_json(
        {"type": "typed", "pane_id": pane_id, "label": label,
         "text": what, "submitted": submitted}
    )

    # Let the pane react, then show the model what its keystrokes did — as ambient
    # state, not as a tool result.
    async def refresh() -> None:
        await asyncio.sleep(POST_TYPE_REFRESH_SECONDS)
        tail = await asyncio.to_thread(_screen_tail, watcher, pane_id)
        if tail:
            await _send_ambient(
                session, f"[tmux update] {label} ({pane_id}) after your input:\n{tail}"
            )

    if session is not None:  # none for a resumed card (_resume)
        _background(asyncio.create_task(refresh()))
    return {"status": "done", "pane": label}


def _send_failed(e: Exception, rec: dict, reason: str) -> dict:
    """The answer to a send that raised. A password prompt refuses model text
    (tmux.send_keys): say so, so the model can tell the user to type it in the app. That
    text is probably the password, so it is never recorded, even under QSDEBUG."""
    if isinstance(e, tmux.PasswordPromptError):
        rec["keys"] = None
        return {"status": "error", "reason": str(e)}
    return {"status": "error", "reason": reason}


async def _send_image(websocket, args: dict, watcher, rec: dict, expected_pid, meter) -> dict:
    """Forward a pasted chat image to a pane through the pane composer's delivery
    (server.attach_image), with the caption, if any, and the submitting Enter in one draft.
    Bytes never reach the audit record: only type, size and target."""
    from .server import attach_image  # noqa: PLC0415 - server imports this module

    pane_id, caption = args.get("pane_id"), args.get("caption", "")
    labels = _labels(watcher)
    if (set(args) - {"pane_id", "image_number", "caption"} or not isinstance(caption, str)
            or not isinstance(pane_id, str) or pane_id not in labels):
        return {"status": "rejected", "reason": "malformed call or unknown pane"}
    rec["pane_id"] = pane_id
    image = meter.image(args.get("image_number"))
    if not image:
        return {"status": "rejected", "reason": "no such image in this conversation"}
    _, mime, data = image
    rec["detail"] = f"{mime} {len(data)}B into {labels[pane_id]}"
    rec["keys"] = caption  # speech, like typed text
    try:
        _, mode = await attach_image(pane_id, expected_pid, data, mime, caption)
    except Exception as e:  # report, don't kill the session
        rec["detail"] += f" ({type(e).__name__})"
        logger.warning("[live] send_image_to_pane failed for %s: %s", pane_id, type(e).__name__,
                       exc_info=telemetry.QSDEBUG)
        return _send_failed(e, rec, "pane did not accept the image")
    rec["detail"] += f" via {mode}"
    watcher.request_reparse(pane_id)
    await websocket.send_json({"type": "typed", "pane_id": pane_id, "label": labels[pane_id],
                               "text": f"[image] {caption}".strip(), "submitted": True})
    return {"status": "done", "pane": labels[pane_id]}


async def _offer(websocket, pane_id: str, pane: dict, *, auto: bool = False) -> None:
    await websocket.send_json(
        {"type": "open_pane", "pane_id": pane_id, "label": _pane_name(pane), "auto": auto})


async def _open_pane(websocket, args: dict, watcher, rec: dict, *, auto: bool = False) -> dict:
    """Put an Open button for a pane in the chat. It changes only what the phone shows,
    never the pane, so no consent card guards it, and a button rather than a jump: the user
    may be mid-sentence in the composer when the reply lands. `auto` jumps as well, for a
    resume the user just tapped Send on. A `name` in the user's words is matched against
    the live panes here, deterministically, rather than left to the model's reading."""
    digest = watcher.digest()
    if isinstance(name := args.get("name"), str) and set(args) == {"name"}:
        rec["keys"] = name  # the user's words: speech, like a transcript
        found, clear = _match_panes(digest, name)
        if not found:
            return {"status": "no_match",
                    "reason": "no open window has that name; for past work use find_sessions"}
        if not clear:  # a button for each: a window named in the reply is one tap away
            for d in found:
                await _offer(websocket, d["pane_id"], d)
            return {"status": "ambiguous",
                    "reason": "a button for each is shown, nothing is open; say how they differ",
                    "candidates": [{"pane_id": d["pane_id"], "window": _pane_name(d)}
                                   for d in found]}
        args = {"pane_id": found[0]["pane_id"]}
    pane_id = args.get("pane_id")
    pane = next((d for d in digest if d["pane_id"] == pane_id), None)
    # Not published yet: a window resume_session just opened, before the watcher's next tick.
    if pane is None and isinstance(pane_id, str):
        with contextlib.suppress(subprocess.CalledProcessError, OSError):  # tmux unreachable
            pane = next(({"window_index": p.window_index, "label": p.label,
                          "title": p.display_title}
                         for p in await asyncio.to_thread(tmux.list_panes) if p.id == pane_id),
                        None)
    if set(args) - {"pane_id"} or not isinstance(pane_id, str) or pane is None:
        return {"status": "rejected", "reason": "malformed call or unknown pane"}
    rec["pane_id"] = pane_id
    await _offer(websocket, pane_id, pane, auto=auto)
    # Not "done": the model read that as "opened" and told the user so before any tap.
    return {"status": "button_shown", "pane": _pane_name(pane),
            "reason": "the user taps it to open; nothing is open yet"}


async def _find_sessions(_websocket, args: dict, watcher, rec: dict) -> dict:
    """Past sessions for a topic, trimmed to what choosing needs. No message text: the
    model routes on titles, recency and liveness, and nothing from an old session is
    handed to it as if it were current. The query terms each one matched are passed on,
    since a title often never names the topic and the model otherwise dismisses a hit."""
    query = args.get("query")
    rec["keys"] = str(query)  # the user's words: speech, like a transcript
    if set(args) - {"query"} or not isinstance(query, str) or not query.strip():
        return {"status": "rejected", "reason": "malformed call"}
    projects = await asyncio.to_thread(agent_history.resolve, query.strip())
    if projects is None:
        return {"status": "error", "reason": "session history unavailable"}
    labels = _labels(watcher)
    results = []
    for p in projects:
        sessions = []
        for s in p["sessions"]:
            pane = await _running_pane(s, watcher)
            if pane and pane not in labels:
                watcher.request_reparse(pane)  # publish it before the model types there
            sessions.append({
                "session_id": s["session_id"],
                "tool": s.get("harness"),  # which agent CLI it resumes in
                "title": s.get("title") or "(untitled)",
                "last_active": (s.get("last_active") or "")[:10],
                "matched": s.get("matched") or [],
                # Named the way windows are everywhere else, once the watcher has it.
                **({"running_in": labels.get(pane, pane), "pane_id": pane} if pane else {}),
                **({"running_elsewhere": True} if s.get("running") and not pane else {}),
                **({"running_unknown": True} if s.get("running_unknown") else {}),
            })
        # The path, shortened under home, so two repos with one name stay distinct.
        results.append({"repo": _home_relative(p["repo"]), "sessions": sessions})
    ids = [s["session_id"] for r in results for s in r["sessions"]]
    rec.update(results=len(ids), top=",".join(ids[:3]))
    return {"status": "ok", "results": results}


# A resumed agent takes a moment to show as running (Claude registers itself, Codex
# opens its rollout), so a repeat call before then would see nothing running and
# start a second copy on the same transcript. Resumes are serialized, and each launch
# holds its session for as long as the pane it opened lives. The pane's pid, not its
# id, is the identity: tmux reuses ids.
_resume_lock = asyncio.Lock()
_resumed: dict[str, tuple[str, str, dict]] = {}  # session id -> (pane id, pid, audit rec)


def _pane_of(running: dict) -> str | None:
    """A running session's pane in THIS tmux server, or None. Its %N comes from whatever
    server the agent ran under, so it counts only if that pane's process here is an
    ancestor of the agent's pid."""
    pane, pid = running.get("tmux_pane"), running.get("pid")
    root = pane and isinstance(pid, int) and tmux.pane_pid(pane)
    return pane if root and int(root) in tmux.ancestors(pid) else None


async def _running_pane(entry: dict, watcher) -> str | None:
    """The pane a running session is in, in THIS tmux server, or None."""
    running = entry.get("running")
    pane = running and await asyncio.to_thread(_pane_of, running)
    if running and not pane and entry.get("harness") == "codex":
        pane = _codex_pane(watcher, entry["session_id"])
    return pane or None


def _codex_pane(watcher, thread_id: str) -> str | None:
    """The one watched Codex pane whose status bar shows this thread's id. Codex's
    app-server daemon, not the terminal client, holds a thread's rollout, so the process
    that proves it's running names no pane; a status bar configured with `session-id`
    shows the id as one of its segments. Only status chrome the parser validates
    counts, in panes classified as Codex (a shell can print a captured footer). Names
    aren't used: two live threads can share one."""
    codex = {d["pane_id"] for d in watcher.digest() if d.get("tool") == "codex"}
    # A copy: the watcher thread adds and drops panes while this runs.
    panes = {pane_id for pane_id, hist in list(watcher.snapshots.items())
             if hist and pane_id in codex
             and thread_id in codex_status_segments(hist[-1]["text"] or "")}
    return panes.pop() if len(panes) == 1 else None


def _home_relative(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + "/") else path


async def _resume_session(websocket, args: dict, watcher, rec: dict) -> dict:
    """Open a past session in a new window. Everything that reaches tmux comes from the
    index, not the model: the model only names a session id."""
    sid = args.get("session_id")
    rec["session_id"] = str(sid)[:80]
    if set(args) - {"session_id"} or not isinstance(sid, str) or not sid.strip():
        return {"status": "rejected", "reason": "malformed call"}
    async with _resume_lock:
        result = await _resume_locked(websocket, sid.strip(), watcher, rec)
    # Tapping Send on the card asked to go there, so take them; voice has no card, and a
    # model's own open_pane only offers a button.
    if rec.get("consent") == "approved" and result.get("pane_id"):
        shown = await _open_pane(websocket, {"pane_id": result["pane_id"]}, watcher, rec, auto=True)
        result["shown"] = shown["status"] == "button_shown"  # so the model offers no second button
    return result


async def _resume_locked(websocket, sid: str, watcher, rec: dict) -> dict:
    if sid in _resumed:
        pane, pid, launched = _resumed[sid]
        # Held while that same process lives in the pane (tmux reuses pane ids).
        if await asyncio.to_thread(tmux.pane_pid, pane) == pid:
            rec.update({**launched, **rec}, detail="resumed moments ago")  # this call's id
            return {"status": "already_running", "pane_id": pane}
        _resumed.pop(sid)  # that pane is gone; fall through to the registry
    entry = await asyncio.to_thread(agent_history.get, sid)
    if entry is None:
        return {"status": "rejected", "reason": "unknown session"}
    argv, cwd = entry.get("resume_argv") or [], entry.get("cwd") or ""
    rec.update(tool=argv[0] if argv else "-", cwd=_home_relative(cwd))

    # Never start a second process on a live session's transcript — including when
    # agent-history couldn't tell whether one is running.
    if entry.get("running_unknown"):
        return {"status": "error", "reason": "can't tell whether it's already running"}
    running = entry.get("running")
    if running:
        # The registry, not the watcher's last tick, is what says which pane it's in.
        pane = await _running_pane(entry, watcher)
        if not pane:
            return {"status": "rejected", "reason": "already running outside this tmux"}
        rec["pane_id"] = pane
        labels = _labels(watcher)
        if pane not in labels:
            watcher.request_reparse(pane)  # publish it before the model types there
        return {"status": "already_running", "pane_id": pane, "pane": labels.get(pane, pane)}

    if not argv or argv[0] not in agent_history.RESUMABLE:
        return {"status": "rejected", "reason": "session can't be resumed"}
    if not os.path.isdir(cwd):
        return {"status": "rejected", "reason": "session directory is gone"}

    title = entry.get("title") or entry["session_id"][:8]
    name = title[:24]
    try:  # tmux can fail here too; report, don't kill the session
        target = _session_for(await asyncio.to_thread(tmux.list_panes), cwd)
        if target is None:
            return {"status": "error", "reason": "no tmux session to open a window in"}
        pane_id = await asyncio.to_thread(tmux.new_window, target, name, argv, cwd)
    except Exception as e:
        logger.warning("[live] resume_session failed for %s", entry["session_id"], exc_info=True)
        rec["detail"] = str(e)[:120]
        return {"status": "error", "reason": "could not open a window"}
    rec.update(pane_id=pane_id, window=name, tmux_session=target)
    pid = await _launched_pid(pane_id)
    if pid is None:  # the window closed at once: the command failed to start
        return {"status": "error", "reason": "the resumed session exited immediately"}
    _resumed[sid] = (pane_id, pid, dict(rec))
    watcher.request_reparse(pane_id)  # wakes a full tick, so the new pane is addressable
    await websocket.send_json(
        {"type": "typed", "pane_id": pane_id, "label": name,
         "text": f"[resumed {title}]", "submitted": True}
    )
    return {"status": "opened", "pane_id": pane_id, "window": name}


_LAUNCH_PID_RETRY_S = 0.1


async def _launched_pid(pane_id: str) -> str | None:
    """The pid of a just-opened pane. tmux sets it as it forks, so this is normally
    immediate; a few short retries cover a slow tmux. None means the pane is gone."""
    for _ in range(5):
        pid = await asyncio.to_thread(tmux.pane_pid, pane_id)
        if pid is not None:
            return pid
        await asyncio.sleep(_LAUNCH_PID_RETRY_S)
    return None


def _session_for(panes, cwd: str) -> str | None:
    """The tmux session to open a resumed agent in: one already working in or under its
    directory, else the session of the active window, else any."""
    if not panes:
        return None
    for p in panes:
        if p.cwd and (p.cwd == cwd or p.cwd.startswith(cwd.rstrip("/") + "/")):
            return p.session
    active = next((p for p in panes if p.window_active == "1" and p.pane_active == "1"), panes[0])
    return active.session


_HISTORY_TOOLS = {"find_sessions": _find_sessions, "resume_session": _resume_session}
_TOOLS = {"type_in_pane", "press_key", "send_image_to_pane", "open_pane", *_HISTORY_TOOLS}


# Keep strong refs to fire-and-forget tasks so they aren't GC'd mid-flight.
_tasks: set[asyncio.Task] = set()


def _background(task: asyncio.Task) -> None:
    def _done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception():  # retrieve, or asyncio warns at GC
            logger.warning("[live] background task failed: %r", t.exception())

    _tasks.add(task)
    task.add_done_callback(_done)


async def _send_ambient(session, text: str) -> None:
    """Inject context WITHOUT prompting a response — the model simply has current state
    the next time the user speaks. This is the whole 'state is just always up to date'
    mechanism; the prompt additionally fences [tmux update] messages off from replies."""
    try:
        await session.send_context(text)
    except Exception as e:
        # A closed socket here is the normal stop/reconnect race (the updater lost to the
        # client's stop, or the session dropped — the reconnect loop's job); log it quietly.
        # A PERSISTENT non-transport failure is different: it would freeze the model's pane
        # view with no signal, so that keeps the warning + traceback.
        closed = type(e).__name__.startswith("ConnectionClosed")
        logger.log(logging.DEBUG if closed else logging.WARNING,
                   "[live] ambient context update skipped: %s", e, exc_info=not closed)


async def _context_updater(session, watcher) -> None:
    """Push digest-level [tmux update]s whenever the deck actually changes — driven by
    the same state_version the /api/state long-poll uses, throttled so a busy session
    drips small updates instead of streaming screens."""
    version = watcher.state_version()
    while True:
        # wait_for_state_change returns the current version even on timeout — only a
        # real advance earns an update (no 30s heartbeat; see docs/design/parse-cadence.md).
        new = await watcher.wait_for_state_change(version, timeout=30.0)
        if new == version:
            continue
        await asyncio.sleep(UPDATE_MIN_SECONDS)  # coalesce a burst into one update
        version = watcher.state_version()  # whatever landed during the throttle window
        await _send_ambient(
            session,
            f"[tmux update] current pane state:\n\n{_pane_context(watcher, screens='active')}",
        )


# A typed turn longer than this is refused, never cut: a cut can drop the very clause
# ("only look, don't type") that limits what a tool-capable assistant does. Both
# composers stop at the same length.
TYPED_TURN_CHARS = 4000


# Images a turn may carry, and their bytes together. Each is resent with every later request
# while the turn is kept (live_chat.TURNS_KEPT), so the budget bounds a whole session's
# history, not just one request. The composer stops at the same count and sends each as a
# JPEG no longer than 1568 px (a few hundred KB), so a current client never nears the bytes.
CHAT_IMAGES = 4
CHAT_IMAGE_BYTES = 8 * 2**20


def _images(raw, session) -> list[tuple[str, bytes]] | str:
    """A typed turn's pasted images as (mime, bytes), or why the whole turn is refused —
    named per cause, so a voice model that can't see images never reads as a size limit.
    The types are the pane paste's (server.send_image); `session.images` says whether the
    model can take them at all."""
    if raw is None or raw == []:
        return []
    from .server import _EXT  # noqa: PLC0415 - server imports this module

    if not session.images:
        return "This voice model can't take images; switch to Chat to send them"
    if not isinstance(raw, list):
        return "Could not read that image; not sent"
    if len(raw) > CHAT_IMAGES:
        return f"At most {CHAT_IMAGES} images a turn; not sent"
    out = []
    for image in raw:
        try:
            mime, data = image["mime"], base64.b64decode(image["data"], validate=True)
        except Exception:  # noqa: BLE001 - any malformed entry refuses the turn
            return "Could not read that image; not sent"
        if not isinstance(mime, str) or mime not in _EXT or not data:
            return "Images go as PNG, JPEG, WebP or GIF; not sent"
        out.append((mime, data))
    return out if sum(len(data) for _, data in out) <= CHAT_IMAGE_BYTES else (
        "Images are over 8 MB together; not sent")


async def _transcript(websocket: WebSocket, meter: _Meter, role: str, text: str, **extra) -> None:
    """One transcript fragment to the browser and the meter — spoken, or typed."""
    if telemetry.QSDEBUG:  # content reaches the journal under the same flag as OTel
        logger.info("[live] %s: %s", role, text)
    meter.note(f"{role}: {text}")
    await websocket.send_json({"type": "transcript", "role": role, "text": text, **extra})


async def _forward_client(websocket: WebSocket, session, meter: _Meter) -> None:
    """Client → model: base64 16kHz PCM frames and typed turns until the client says stop.
    A typed turn is echoed as the user's transcript, so both clients render it like speech."""
    while True:
        data = await websocket.receive_json()
        action = data.get("action")
        if action == "audio":
            raw = data.get("data")
            if not raw:
                continue
            try:
                audio = base64.b64decode(raw)
            except Exception:  # noqa: BLE001 - skip one bad frame, keep streaming
                continue
            await session.send_audio(audio)
        elif action == "text":
            text = data.get("text")
            text = text.strip() if isinstance(text, str) else ""
            images = _images(data.get("images"), session)
            # Each typed turn gets exactly one answer, the echo (with its image count) or a
            # refusal, which is how the client pairs its thumbnails with the right turn.
            refusal = ("Too long; not sent" if len(text) > TYPED_TURN_CHARS
                       else images if isinstance(images, str) else None)
            if refusal:
                await websocket.send_json({"type": "error", "message": refusal, "refused": True})
            elif text or images:
                # The model reads each image's number off its turn, and names it in a tool call.
                numbers = range(meter.image_total + 1, meter.image_total + len(images) + 1)
                labelled = "\n".join(
                    filter(None, [text, *(f"(image {n} attached)" for n in numbers)]))
                try:  # handed over first: a chat session with a full queue refuses the turn
                    await (session.send_text(labelled, images) if images
                           else session.send_text(text))
                except asyncio.QueueFull:
                    busy = "Still answering earlier turns; not sent"
                    await websocket.send_json({"type": "error", "refused": True, "message": busy})
                    continue
                meter.keep_images(images)
                # An unanswered card (not one tapped and still unwinding): this supersedes
                # it, and the rest of its turn.
                for answer in [a for a in meter.approvals.values() if not a.done()]:
                    meter.superseded = True
                    answer.set_result(None)
                # Parked ones too (their model is gone: nothing to tell it), all recorded
                # before any is sent, so a socket dying mid-way loses none.
                claimed = _unpark(meter)
                for proposal, parked in claimed:
                    _answer(meter, proposal, None, parked[3])
                for proposal, _ in claimed:
                    await _tell(websocket, meter, proposal)
                await _transcript(websocket, meter, "user", text, new_segment=True,
                                  images=len(images))
        elif action == "approve":  # the user's Send / Cancel on a proposed action
            proposal, ok = str(data.get("id")), data.get("ok") is True
            answer = meter.approvals.get(proposal)
            if answer:
                if not answer.done():
                    answer.set_result(ok)
            elif parked := _unpark(meter, proposal):
                await _resume(websocket, proposal, parked[0][1], ok)
            else:
                await _tell(websocket, meter, proposal)
        elif action == "sync":  # a reconnected phone's untapped cards: settled meanwhile?
            ids = data.get("ids")
            _sweep()
            for proposal in map(str, ids if isinstance(ids, list) else []):
                if proposal not in meter.approvals and _key(meter, proposal) not in _parked:
                    await _tell(websocket, meter, proposal)
        elif action == "viewing":  # whether a card waiting now would be seen (_nudge)
            meter.unseen_since = (None if data.get("on") is not False
                                  else meter.unseen_since or time.monotonic())
        elif action == "stop":
            return
        else:
            logger.debug("[live] unknown client action: %s", action)


async def _receiver(websocket: WebSocket, session, watcher, meter: _Meter) -> None:
    """Model → client: voice audio, both transcripts, tool calls, barge-in. Also meters
    the session — takes each usage event into `meter` and emits a per-turn OTel record
    at every turn boundary."""
    meter.superseded = False  # a reconnect drops the turn it belonged to
    async for ev in session.events():
        if ev.kind == "usage":
            meter.usage.set(ev.usage)
        elif ev.kind == "tool_call":
            meter.note(f"[typed] {ev.call.args}")
            await _handle_tool_call(websocket, session, ev.call, watcher, meter)
        elif ev.kind == "audio":
            await websocket.send_json({"type": "audio", "data": base64.b64encode(ev.data).decode()})
        elif ev.kind == "transcript":
            await _transcript(websocket, meter, ev.role, ev.text)
        elif ev.kind == "turn_complete":
            meter.superseded = False
            meter.end_turn()
            await websocket.send_json({"type": "turn_complete"})
        elif ev.kind == "interrupted":
            await websocket.send_json({"type": "interrupted"})


async def _hold(websocket: WebSocket, seconds: float) -> bool:
    """Wait out a reconnect backoff while still reading the browser socket. Without this
    the loop reconnected to Vertex for a phone that was already gone — the tunnel drops
    both legs at once, and the browser's disconnect is only observed by a receive. A
    WebSocketDisconnect propagates (client gone); a stop returns False; a timeout, True."""
    try:
        async with asyncio.timeout(seconds):
            while (await websocket.receive_json()).get("action") != "stop":
                pass  # a stray frame (audio already in flight) is no reason to reconnect early
    except TimeoutError:
        return True
    return False


async def _run_session(websocket: WebSocket, watcher, meter: _Meter) -> None:
    """Connect to meter.model and run the session; reconnect with backoff on drops."""
    model = meter.model
    max_reconnects = 5
    for attempt in range(max_reconnects + 1):
        await websocket.send_json({"type": "status", "status": "connecting"})
        try:
            # The system prompt is rebuilt per connect attempt so a RECONNECT gets a fresh
            # pane snapshot — the connect snapshot is the only place full screens are sent
            # (ambient [tmux update]s omit them), so reusing a stale one would leave a
            # reconnected session answering/acting on minutes-old screen state.
            async with live_providers.connect(
                model, _system_prompt(watcher, text=meter.text)
            ) as session:
                logger.info(
                    "[live] session up (model=%s via %s, actor=%s)",
                    model.model, model.backend, meter.actor,
                )
                await websocket.send_json({"type": "status", "status": "listening"})
                side = [
                    asyncio.create_task(
                        _receiver(websocket, session, watcher, meter), name="live-receiver"
                    ),
                    asyncio.create_task(
                        _context_updater(session, watcher), name="live-context-updater"
                    ),
                ]
                # The mic pump is RACED against the side tasks, not awaited alone. A
                # provider that drops surfaces here as the receiver ending — cleanly, when
                # its event stream just stops, or with an exception — and awaiting only the
                # pump meant nobody looked until the browser happened to send another
                # frame. A muted or backgrounded phone sends none, so the session sat in
                # "listening" against a dead socket with the reconnect loop one frame away
                # and never entered. (The GPT-Live adapter already waits on all of its
                # tasks together; this is the seam saying the same thing.)
                pump = asyncio.create_task(
                    _forward_client(websocket, session, meter), name="live-client"
                )
                side.append(pump)  # so the drain below tears this one down too
                try:
                    done, _ = await asyncio.wait(side, return_when=asyncio.FIRST_COMPLETED)
                    if pump in done:
                        pump.result()  # a WebSocketDisconnect here is the browser going away
                        return  # client sent stop — clean exit
                    for t in done:
                        t.result()  # a real failure, re-raised into the reconnect below
                    # Nothing raised, so a side task simply ENDED: the provider closed its
                    # stream. A quiet end is still an end — reconnect rather than sit.
                    raise ConnectionError("live provider stream ended")
                finally:
                    for t in side:
                        t.cancel()
                    # Bounded drain: a side task stuck in an un-cancellable unwind (e.g. genai's
                    # generator finally awaiting a close handshake on a half-open socket) must not
                    # delay the reconnect / websocket-close this finally gates by an OS TCP timeout.
                    # wait() never raises for task outcomes, so the CancelledError from the cancel
                    # above is absorbed here rather than escaping as it did under
                    # suppress(Exception).
                    done, pending = await asyncio.wait(side, timeout=2)
                    for t in pending:
                        logger.warning(
                            "[live] side task %s did not unwind within 2s; abandoning it",
                            t.get_name(),
                        )
                    for t in done:
                        if not t.cancelled() and (exc := t.exception()) is not None:
                            logger.warning(
                                "[live] side task %s ended in error: %r", t.get_name(), exc
                            )
        except (WebSocketDisconnect, live_providers.Unreachable):
            raise  # client gone, or a misconfiguration no retry can fix
        except Exception:
            if attempt >= max_reconnects:
                raise
            backoff = min(2**attempt, 15)
            logger.warning("[live] session dropped; reconnecting in %ds", backoff, exc_info=True)
            await websocket.send_json({"type": "status", "status": "reconnecting"})
            if not await _hold(websocket, backoff):
                return  # client sent stop during the backoff


def offered() -> list[live_providers.LiveModel]:
    """Everything a client may pick, in picker order: the configured table, then GPT-Live
    when its key is set. ONE list, behind both /api/version's menu and this module's socket
    gate, because the two saying it separately is exactly how they come to disagree — and a
    gate that refuses what the menu just offered is indistinguishable from a broken pick.

    GPT-Live is appended rather than configured: the seam opens a CONNECTION and hands it
    to the shared coroutines, while the adapter owns a whole SESSION, so it cannot be a
    table entry. The TABLE wins a label collision — an operator who names an entry
    "GPT-Live 1" gets the entry they configured, not a second row shadowing it — and it
    wins even while that entry is KEYLESS and therefore off the menu. A label is owned by
    whoever configured it, not by whoever currently has credentials: otherwise a remembered
    pick would silently change which model answers as keys come and go, which is the one
    thing label-only selection exists to prevent."""
    from . import gpt_live  # noqa: PLC0415 - the adapter imports this module's handlers

    # Read the table ONCE and filter here rather than calling available(): the reservation
    # above has to see the unoffered half too, and two reads of the same env for one answer
    # is two chances for the halves to disagree.
    table = live_providers.models()
    menu = [m for m in table if m.available()]
    if os.environ.get("OPENAI_API_KEY") and all(m.label != gpt_live.LABEL for m in table):
        menu.append(gpt_live.ENTRY)
    return menu


def _menu(text: bool) -> list[live_providers.LiveModel]:
    """The offered models for one mode: chat models for text, the rest for voice."""
    return [m for m in offered() if m.text == text]


def pick(label: str | None, *, text: bool = False) -> live_providers.LiveModel | None:
    """Resolve a client-supplied label against the mode's `offered()` models. Label-only,
    like launchers: the client names an entry and never a model id, backend or credential.
    No label at all is the mode's first entry — what a user who never opens the picker
    gets. Unknown, configured but keyless, or the other mode's is None: refused, never
    defaulted, because silently answering with a different model would make a
    side-by-side comparison lie."""
    menu = _menu(text)
    if not label:
        return menu[0] if menu else None
    return next((m for m in menu if m.label == label), None)


@router.websocket("/api/live-mode")
async def live_mode(websocket: WebSocket) -> None:
    if not enabled():
        # Feature-flagged off — refuse before any model connection or mic streaming.
        # 1008 = policy violation; the client hides the button too, so this only fires
        # for a stale tab or a direct probe. Reason points at the fix (reload the page —
        # a current client reads live_enabled from /api/version and hides the button).
        await websocket.close(code=1008, reason="Live Mode is disabled — reload the page")
        return
    from . import gpt_live  # noqa: PLC0415 - adapter imports this module's shared handlers

    text = websocket.query_params.get("mode") == "text"
    model = pick(websocket.query_params.get("model"), text=text)
    if model is None:
        # Nothing offered at all (every entry key-gated, no key set) is the operator's
        # config problem, not a stale tab's — a reload can't fix it, so don't say so.
        why = "reload the page" if _menu(text) else "no configured model has its key set"
        await websocket.close(code=1008, reason=f"Live model not available — {why}")
        return
    # Which runner, decided by identity rather than by re-reading the label: `offered()`
    # already settled who owns this pick, including a table entry that claimed the
    # adapter's label.
    use_gpt = model is gpt_live.ENTRY
    await websocket.accept()
    watcher = websocket.app.state.watcher
    # Per-session UUID — the summable key that ties this voice session's cost (emit_live_turn)
    # to its screen watch-time (emit_live). Accept the client's if it passes one, else mint one.
    session_id = websocket.query_params.get("session") or uuid.uuid4().hex
    meter = _Meter(session_id, telemetry.actor(websocket), model, text=text)
    meter.push = getattr(websocket.app.state, "push", None)
    _chats[meter.actor, meter.session] = meter
    _audit(meter, "live_session", detail="start", mode="text" if text else "voice")
    if websocket.query_params.get("fresh"):  # a new chat: an earlier one ended offline
        _unpark(meter)
    outcome, reason = "ok", "stop"
    try:
        if use_gpt:
            await gpt_live.run_session(websocket, watcher, meter)
        else:
            await _run_session(websocket, watcher, meter)
    except WebSocketDisconnect:
        reason = "client gone"  # phone lock / tab close / tunnel drop — the normal ends
    except Exception as e:  # noqa: BLE001 - the session's last stop: report it, never crash the WS
        reason, outcome = "error", f"error: {type(e).__name__}"  # the class, never provider text
        # A model that can't be reached (bad deployment name, rejected key) says exactly
        # what to fix — the user fixes config, not the retry count. Anything else stays a
        # generic line so internal detail never reaches the browser. The adapter's
        # ProviderError is the same promise from the other side of the seam — it has
        # already sanitized the provider's diagnostics — so it rides this one path rather
        # than an except clause of its own, which would have skipped the log line and sent
        # on a socket that may already be gone.
        fatal = isinstance(e, (live_providers.Unreachable, gpt_live.ProviderError))
        (logger.error if fatal else logger.exception)(
            "[live] session failed%s", f": {e}" if fatal else ""
        )
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {"type": "error", "message": str(e) if fatal else "live session failed"}
            )
    finally:
        if reason == "stop":  # the user ended the chat, and its cards with it
            _unpark(meter)
        if _chats.get((meter.actor, meter.session)) is meter:  # not a reconnect's newer one
            del _chats[meter.actor, meter.session]
        meter.finish()  # final cumulative OTel record + fold cost into the status bar
        _audit(
            meter, "live_session", detail=f"end: {reason}", outcome=outcome, turns=meter.turns,
            cost_usd=round(meter.usage.cost(), 4),
            duration_s=round(time.monotonic() - meter.started, 1),
        )
        with contextlib.suppress(Exception):
            await websocket.close()
