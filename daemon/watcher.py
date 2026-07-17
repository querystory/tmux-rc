"""Background poll loop: capture the target pane, classify it, keep the latest
PaneState and a rolling snapshot history for the timeline.

Milestone 1 watches a single target pane. The state is held in a list so the
server and the eventual multi-pane fan-out (Milestone 2) need no change.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from functools import partial

from . import tmux
from .classify import bootstrap, classify
from .llm import backing_off, classify_text, summarize_events

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.5
# Full-text snapshots per pane (the timeline). Snapshots append on real content change
# only; each is <=~24KB (200 lines), so 200 of them is ~5MB/pane worst case — memory is
# cheap and none of this reaches the model, so keep enough to scroll back meaningfully.
SNAPSHOT_HISTORY = 200
# LLM parse cadence. We capture every tick (cheap, for the snapshot buffer) but only
# PARSE when the content fingerprint changed, or when this long since the last parse
# (a heartbeat so a slowly-evolving screen still refreshes). An agent that's merely
# working — spinner + timer + token counter ticking, no content change — costs ZERO
# LLM calls, because that churn is stripped from the fingerprint.
HEARTBEAT_SECONDS = 10
# One-time deep read of a pane's scrollback that seeds the card (summary + history)
# before live watching has accumulated anything. Fat input ⇒ at most ONE per tick,
# behind the live parses; a few retries with spacing so an LLM hiccup isn't permanent.
BOOTSTRAP_LINES = 800
BOOTSTRAP_ATTEMPTS = 3
BOOTSTRAP_RETRY_SECONDS = 60
EVENTS_LOG_MAX = 300  # per-pane activity-log cache served to the UI (~45KB/pane worst case)
PRIOR_FRAMES = 2  # recent captures sent alongside the current one, for continuity
# When a pane has been idle this long with accumulated events, summarize the recent
# activity burst once (a {from,to,text} span). The UI collapses events in that time
# range under the summary. Generated once per idle period; reset when the pane works.
IDLE_SUMMARY_AFTER = 60
# Per-pane activity-history ring (memory only — never sent to the model, so growing it
# costs RAM, not tokens; ~150B/event => ~75KB/pane at 500). The model feedback window
# stays small and separate (_recent_events); the summarizer slices its own input.
BURST_HISTORY = 500
DIGEST_HISTORY = 100  # events returned per pane by /api/digest (slice of the ring)
# How long a reported event's text suppresses an identical re-report. Long enough to
# starve the repetition loop (a model re-emitting every heartbeat), short enough that a
# genuinely repeated action later in the session shows up again.
RECENT_EVENT_TTL = 15 * 60

# Volatile bits that repaint constantly on an agent status line even when nothing
# meaningful is happening. Confirmed by diffing live captures: elapsed timers, token
# counts, spinner glyphs, AND the status-line metrics that drift every tick — cost
# ($38.15→$38.31), context %, and the history counters (prompts/tools/MB). Strip them
# so "is this the same screen?" is judged on real content; otherwise the LLM re-fires
# every tick and the card flickers. (These values still reach the UI via the LLM's
# structured fields — we only ignore them for the change check.)
_VOLATILE_RE = re.compile(
    r"\d+h\d+m|\d+m\s*\d+s|\d+s\b"  # durations
    r"|↓\s*[\d.]+k?|[\d.]+k tokens"  # token counts
    r"|\$[\d.]+"  # cost
    r"|\d+%\s*ctx"  # context percent
    r"|\d+\s*(prompts|tools|imgs)|[\d.]+\s*[KMG]B"  # history counters
    r"|[⏳✳✻✶✷✽❋⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒]"  # spinner glyphs
    r"|[ \t]+$",  # trailing whitespace
    re.MULTILINE,
)


def _fingerprint(text: str) -> str:
    """Content signature of a pane, ignoring volatile timer/spinner churn."""
    return _VOLATILE_RE.sub("", text)


def _append_events(log: list[dict], events: list[dict], ts: float) -> None:
    """Append events (stamped with `ts`) to a pane's activity-log cache, in place,
    holding it to EVENTS_LOG_MAX by dropping the oldest. See docs/design/activity-log.md."""
    log.extend({**e, "ts": ts} for e in events)
    del log[:-EVENTS_LOG_MAX]


class Watcher:
    """Holds current pane state + snapshot history, refreshed by an async loop."""

    def __init__(self, target: str | None, use_llm: bool = True):
        self.target = target
        self.use_llm = use_llm
        self.states: list[dict] = []  # raw LLM JSON dicts, piped straight to the UI
        self.events_log: dict[
            str, list[dict]
        ] = {}  # pane_id -> activity-log cache [{text, file?, meta?, ts, historical?}]
        self._events_seq: dict[
            str, int
        ] = {}  # pane_id -> monotonic count of events ever appended (refetch signal)
        self.snapshots: dict[str, list[dict]] = {}  # pane_id -> [{id, text, ts}]
        self._prev_fp: dict[str, str] = {}  # pane_id -> fingerprint at last parse
        self._unchanged_since: dict[str, float] = {}
        self._last_parse: dict[
            str, float
        ] = {}  # pane_id -> when we last called the LLM
        self._tool: dict[
            str, tuple[str, float]
        ] = {}  # pane_id -> (agent tool, last-seen ts)
        self._state: dict[
            str, dict
        ] = {}  # pane_id -> last parsed dict (reused between parses)
        self._recent_events: dict[
            str, list[tuple[str, float]]
        ] = {}  # pane_id -> recently-emitted (event text, ts); TTL-expired on read
        self._last_dropped: dict[
            str, list[str]
        ] = {}  # pane_id -> texts dropped by the dedup guard last parse (log throttle)
        self._burst: dict[
            str, dict
        ] = {}  # pane_id -> {start, events:[{text,ts}]} for the current activity burst
        self._summary: dict[
            str, dict
        ] = {}  # pane_id -> cached {from,to,text} idle summary
        self._birth: dict[str, str] = {}  # pane_id -> pane pid; detects recycled ids
        self._boot: dict[
            str, dict
        ] = {}  # pane_id -> bootstrap {summary, name, events} + seeded flag once served
        self._boot_tries: dict[
            str, tuple[int, float]
        ] = {}  # pane_id -> (attempts, last attempt ts)
        self._last_tick: float = (
            0.0  # wall time of the last loop iteration (staleness check)
        )
        self._task: asyncio.Task | None = None

    def is_stale(self) -> bool:
        """True if the loop hasn't ticked recently — the watcher is dead/stalled and
        the served state is frozen. Surfaced to the UI so a dead loop is VISIBLE
        instead of silently serving stale cards (as happened after a resize/reload)."""
        return (
            self._last_tick > 0 and (time.time() - self._last_tick) > 5 * POLL_SECONDS
        )

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def digest(self) -> list[dict]:
        """Per-pane current state PLUS recent history, for agent/programmatic consumers.

        /api/state is shaped for the phone: it carries only the events NEW in the last
        parse (the phone accumulates its own log client-side), so a one-shot reader sees
        an empty feed and no past. This view exposes what the watcher already tracks —
        the burst ring (recent timestamped events) and the cached idle summary — so one
        GET answers "what has been going on in each pane," no client-side accumulation
        and no extra LLM calls."""
        out = []
        for s in self.states:
            pid = s.get("pane_id")
            burst = self._burst.get(pid) or {}
            out.append(
                {
                    "pane_id": pid,
                    "label": s.get("label"),
                    "tool": s.get("tool"),
                    "activity": s.get("activity"),
                    "idle_seconds": s.get("idle_seconds"),
                    "headline": s.get("headline"),
                    "question": (s.get("question") or {}).get("prompt"),
                    # LLM one-liner for the last activity burst (present once the pane
                    # has idled past the summary threshold; None while actively working).
                    "summary": (self._summary.get(pid) or {}).get("text"),
                    # Recent activity, newest last, straight from the burst ring.
                    "history": [
                        {"ts": e["ts"], "text": e["text"]}
                        for e in (burst.get("events") or [])[-DIGEST_HISTORY:]
                    ],
                }
            )
        return out

    def snapshot_text(self, pane_id: str, snap_id: str) -> str | None:
        for s in self.snapshots.get(pane_id, []):
            if s["id"] == snap_id:
                return s["text"]
        return None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._tick)
            except Exception:  # noqa: BLE001 - never let one bad tick kill the loop
                logger.warning("watcher tick failed", exc_info=True)
            self._last_tick = time.time()
            await asyncio.sleep(POLL_SECONDS)

    def _tick(self) -> None:
        if not tmux.server_running():
            self.states = []
            return
        # Multi-pane: watch every pane (or just the configured target if set). Each
        # pane's per-tick work is keyed by pane.id, so panes are fully independent.
        if self.target:
            p = tmux.find_pane(self.target)
            panes = [p] if p else []
        else:
            panes = tmux.list_panes()
        if not panes:
            self.states = []
            return
        alive = {p.id for p in panes}
        # tmux recycles pane ids on close ("%3" freed, reassigned to a new pane). If an
        # id's pid changed, it's a different pane wearing the old id — evict the previous
        # pane's buffers so its snapshots/events don't bleed into the new one. Emit
        # lifecycle telemetry at the two birth transitions: a brand-new id, and a recycled
        # id (which is a removal of the old occupant + a fresh creation).
        for p in panes:
            if p.id in self._birth and self._birth[p.id] != p.pid:
                self._forget(p.id)  # emits pane_removed for the old occupant
                self._pane_event("pane_created", pane_id=p.id, label=p.label, tool=None)
            elif p.id not in self._birth:
                self._pane_event("pane_created", pane_id=p.id, label=p.label, tool=None)
            self._birth[p.id] = p.pid
        # One bad pane must NEVER wedge the whole watcher (that loses all visibility).
        # Tick each pane defensively: on error, degrade to a stub card, keep going.
        states = []
        for p in panes:
            try:
                s = self._tick_pane(p)
            except subprocess.CalledProcessError as e:
                # rc=124 is tmux.py's timeout marker: a wedged/slow tmux SERVER, not a
                # closed pane — labeling it "vanished" would misdirect exactly the
                # incident class the timeouts exist to expose. Anything else here is
                # the expected pane-closed race (gone between list-panes and
                # capture-pane); one line, next tick's gc evicts it.
                if e.returncode == 124:
                    logger.warning("tmux timed out mid-tick on pane %s", p.id)
                else:
                    logger.warning("pane %s vanished mid-tick", p.id)
                s = None
            except Exception:  # noqa: BLE001 - isolate per-pane failures
                logger.warning("pane tick failed: %s", p.id, exc_info=True)
                s = None
            if not isinstance(s, dict):
                s = {
                    "pane_id": p.id,
                    "label": p.label,
                    "title": p.display_title,
                    "tool": "unknown",
                    "activity": "unknown",
                    "updated_at": time.time(),
                }
            states.append(s)
        # Mark the pane tmux currently has focused, so the phone can default its
        # selection to the pane the user is actually on (not just the top-sorted one).
        focused = tmux.active_pane_id()
        for s in states:
            s["tmux_active"] = s.get("pane_id") == focused
        # One-time scrollback bootstrap (staggered), then merge its products into the
        # outgoing states. Every state also advertises events_seq (monotonic append
        # counter, not length — see _events_seq) so the client knows when to
        # (re)fetch /api/panes/{id}/events.
        if self.use_llm:
            self._maybe_bootstrap(panes)
        for s in states:
            s["events_seq"] = self._events_seq.get(s.get("pane_id"), 0)
            b = self._boot.get(s.get("pane_id"))
            if not b:
                continue
            s["session_summary"] = b["summary"]
            if b["name"] and not s.get("title"):
                s["title"] = b["name"]
        # Keep tmux's natural order (session/window/pane, as list-panes emits it) —
        # the UI's dock, list, and swipe direction all key off this array order, and
        # it must match the window numbers the user sees in tmux's own status bar.
        # (Activity grouping is a client concern now; we used to sort waiting-first.)
        self.states = states
        self._gc(alive)

    def _maybe_bootstrap(self, panes) -> None:
        """Deep-read ONE not-yet-bootstrapped pane's scrollback per tick (staggers the
        fat LLM calls behind live parses) and stash {summary, name, events}. Failures
        retry a few times with spacing — a 429 at boot shouldn't be permanent."""
        if backing_off():
            return  # rate-limited: a call now would be refused — don't burn attempts
        now = time.time()
        for p in panes:
            if p.id in self._boot:
                continue
            tries, last = self._boot_tries.get(p.id, (0, 0.0))
            if tries >= BOOTSTRAP_ATTEMPTS or (tries and now - last < BOOTSTRAP_RETRY_SECONDS):
                continue
            self._boot_tries[p.id] = (tries + 1, now)
            try:
                llm_fn = partial(
                    classify_text,
                    kind="bootstrap",
                    pane_uid=tmux.pane_uid(p),
                    pane_label=p.label,
                )
                result = bootstrap(
                    p, tmux.capture_pane(p.id, lines=BOOTSTRAP_LINES), llm_fn
                )
            except Exception:  # noqa: BLE001 - one pane must never wedge the watcher
                logger.warning("bootstrap failed for %s", p.id, exc_info=True)
                result = None
            if result:
                self._boot[p.id] = result
                # Reconstructed history seeds the FRONT of the log cache (it predates
                # anything observed live), and the parser's already-reported list so
                # the next live parse doesn't restate it as new events.
                log = self.events_log.setdefault(p.id, [])
                log[:0] = [{**e, "ts": now} for e in result["events"]]
                del log[:-EVENTS_LOG_MAX]
                self._events_seq[p.id] = (
                    self._events_seq.get(p.id, 0) + len(result["events"])
                )
                recent = self._recent_events.setdefault(p.id, [])
                recent.extend((e["text"], now) for e in result["events"])
                logger.info("%s: bootstrapped (%d events)", p.id, len(result["events"]))
            return  # at most one bootstrap attempt per tick

    # Every per-pane store, keyed by pane id. One tuple so gc (vanished panes) and
    # reuse-eviction (recycled ids) can't drift apart.
    def _stores(self):
        return (
            self._prev_fp,
            self._unchanged_since,
            self._last_parse,
            self._tool,
            self._state,
            self._recent_events,
            self._last_dropped,
            self.snapshots,
            self._burst,
            self._summary,
            self._birth,
            self._boot,
            self._boot_tries,
            self.events_log,
            self._events_seq,
        )

    def _pane_event(
        self, event: str, *, pane_id: str, label: str, tool: str | None
    ) -> None:
        """Best-effort pane-lifecycle telemetry. server_uid is constant for this daemon
        run, so a departing pane's uid is reconstructable from its id alone."""
        try:
            from .telemetry import emit_pane_event

            emit_pane_event(
                event=event,
                pane_uid=f"{tmux.server_uid()}:{pane_id}",
                label=label,
                tool=tool,
            )
        except Exception:  # noqa: BLE001 - telemetry must never break the watcher
            logger.debug("pane-event emit failed", exc_info=True)

    def _forget(self, pane_id: str) -> None:
        # Emit pane_removed from the ONE choke point where a pane's state is dropped
        # (covers both a vanished pane via _gc and the old occupant of a recycled id),
        # using its last-known label/tool before we discard them.
        last = self._state.get(pane_id) or {}
        self._pane_event(
            "pane_removed",
            pane_id=pane_id,
            label=last.get("label", pane_id),
            tool=last.get("tool"),
        )
        for store in self._stores():
            store.pop(pane_id, None)

    def _gc(self, alive: set[str]) -> None:
        """Drop per-pane state for panes that no longer exist, so closing windows
        doesn't leak memory over a long session."""
        for pid in {k for store in self._stores() for k in store} - alive:
            self._forget(pid)

    def _maybe_summarize(self, pane_id: str, idle: int) -> dict | None:
        """Once a pane has been idle past the threshold, summarize its recent activity
        burst into a {from, to, text} span (one LLM call, cached until new activity).
        Returns the cached/new summary, or None if there's nothing to summarize yet."""
        if pane_id in self._summary:
            return self._summary[pane_id]  # already summarized this idle period
        burst = self._burst.get(pane_id)
        if (
            not self.use_llm
            or idle < IDLE_SUMMARY_AFTER
            or not burst
            or not burst["events"]
        ):
            return None
        texts = [e["text"] for e in burst["events"]]
        summary_text = summarize_events(texts)  # may be None on LLM failure
        if not summary_text:
            return None
        span = {
            "from": burst["events"][0]["ts"],
            "to": burst["events"][-1]["ts"],
            "text": summary_text,
            "count": len(texts),
        }
        self._summary[pane_id] = span
        return span

    def _tick_pane(self, pane) -> dict:
        text = tmux.capture_pane(pane.id)
        now = time.time()
        fp = _fingerprint(text)
        changed = fp != self._prev_fp.get(
            pane.id
        )  # real content change (timers stripped)
        if changed:
            self._unchanged_since[pane.id] = now
        idle = int(now - self._unchanged_since.get(pane.id, now))

        # Record a snapshot whenever content changed (bounded ring buffer, for timeline).
        if changed:
            hist = self.snapshots.setdefault(pane.id, [])
            hist.append({"id": f"{int(now * 1000)}", "text": text, "ts": now})
            del hist[:-SNAPSHOT_HISTORY]

        # Decide whether to PARSE (call the LLM). Parse on real content change, or on a
        # heartbeat so a slowly-drifting screen refreshes. Merely-working churn (spinner/
        # timer/tokens) is stripped from the fingerprint ⇒ no change ⇒ no call.
        cached = self._state.get(pane.id)
        due = now - self._last_parse.get(pane.id, 0) >= HEARTBEAT_SECONDS
        if cached is not None and not changed and not due:
            cached["idle_seconds"] = idle  # just tick the timer, reuse everything else
            cached["updated_at"] = now
            # The pane TITLE lives outside the captured text — agents rename it while
            # the screen sits still, so refresh it even when nothing else re-parses.
            cached["title"] = pane.display_title
            # Idle a while with unsummarized activity → summarize the burst once, so the
            # UI can collapse those events under a {from,to,text} span.
            cached["summary"] = self._maybe_summarize(pane.id, idle)
            return cached

        # Recent prior captures (before the current one) give the model continuity, so
        # slow line-by-line output doesn't make it re-decide each frame (anti-flicker)
        # and it can describe what just happened. Cheap — prompt tokens are cheap.
        hist = self.snapshots.get(pane.id, [])
        prior = [s["text"] for s in hist[-(PRIOR_FRAMES + 1) : -1]]
        # Feed back events we already reported so the model emits only NEW ones instead
        # of restating ongoing work in slightly different words each parse (the source
        # fix for near-duplicate log entries — dedup at the model, not the client).
        # Entries expire after RECENT_EVENT_TTL so a LEGITIMATE later repeat of the same
        # action (rerunning the same command an hour on) isn't suppressed forever — the
        # cost is a bounded worst case of one duplicate per TTL for a persistently
        # re-emitting model, which the guard below re-drops on the next parse.
        recent = [
            (t, ts)
            for t, ts in self._recent_events.get(pane.id, [])
            if now - ts < RECENT_EVENT_TTL
        ]
        recent_texts = [t for t, _ in recent]
        # Bind `changed` (content-change vs heartbeat re-parse) and the pane's stable
        # identity into the parser so they ride through to the benchmark telemetry without
        # widening the generic llm_fn seam. Label falls back to the tmux label; the
        # classifier may refine it (agent session name) before emitting.
        llm_fn = (
            partial(
                classify_text,
                changed=changed,
                pane_uid=tmux.pane_uid(pane),
                pane_label=pane.label,
            )
            if self.use_llm
            else None
        )
        state = classify(
            pane,
            text,
            llm_fn=llm_fn,
            prior=prior,
            recent_events=recent_texts,
        )
        # Remember the events this parse produced (bounded) for the next call's context,
        # and add them (timestamped) to the current activity burst. New activity clears
        # any cached idle summary — it'll be regenerated when the pane goes idle again.
        #
        # Drop events whose text we already reported recently (whether the model re-emits
        # one — the feedback list only SHOWS it the last 20, so re-emitting an older
        # entry isn't disobedience — or duplicates one within a single response, which
        # `seen` also catches). Without this guard a repeated event is appended to
        # _recent_events again each parse, the feedback section becomes a wall of the
        # same line, and that repetition-primed prompt tail degenerates the model's
        # output — observed in production as it emitting a SECOND copy of the whole JSON
        # object ("Extra data" parse failures), a self-reinforcing loop.
        seen = set(recent_texts)
        new_events, dropped = [], []
        for e in state.get("events") or []:
            if not (isinstance(e, dict) and e.get("text")):
                continue
            (dropped if e["text"] in seen else new_events).append(e)
            seen.add(e["text"])
        # Visibility without spam: log only when the dropped set changes — a heartbeat
        # re-dropping the same text every 10s stays quiet.
        dropped_texts = [e["text"] for e in dropped]
        if dropped_texts and dropped_texts != self._last_dropped.get(pane.id):
            logger.info(
                "%s: dropped %d re-emitted event(s), e.g. %r",
                pane.id,
                len(dropped_texts),
                dropped_texts[0][:80],
            )
        self._last_dropped[pane.id] = dropped_texts
        state["events"] = new_events  # dups also don't reach the UI activity feed
        # Per-pane activity-log cache (what /api/panes/{id}/events serves): the phone's
        # feed no longer starts from zero on page reload. IN-MEMORY, not persisted —
        # tmux is the state; a daemon restart drops this and bootstrap reconstructs an
        # approximation from scrollback. Same category as the snapshot ring buffer.
        # See docs/design/activity-log.md.
        if new_events:
            _append_events(self.events_log.setdefault(pane.id, []), new_events, now)
            # Monotonic append counter, NOT len(log): the log is capped, so once it's
            # full its length stops changing while content still rotates — a length
            # signal would freeze and the client would stop refetching. The counter
            # only ever grows, so "seq changed ⇒ refetch" survives the cap.
            self._events_seq[pane.id] = self._events_seq.get(pane.id, 0) + len(new_events)
        recent.extend((e["text"], now) for e in new_events)
        self._recent_events[pane.id] = recent[-30:]
        if new_events:
            burst = self._burst.setdefault(pane.id, {"start": now, "events": []})
            for e in new_events:
                burst["events"].append({"text": e["text"], "ts": now})
            burst["events"] = burst["events"][-BURST_HISTORY:]
            self._summary.pop(pane.id, None)  # activity invalidates the idle summary
        state["summary"] = self._summary.get(
            pane.id
        )  # may be None (only set once idle)
        self._prev_fp[pane.id] = fp
        self._last_parse[pane.id] = now

        # Tool identity. Trust the LLM's read of the screen: a real agent pane has an
        # unmistakable status-line/box, so if it says "shell" it IS a shell — never let
        # a stale sticky value override that (that bug put the Claude icon on a bash
        # pane). Only bridge "unknown"/missing (a transient we genuinely can't tell) with
        # the last known agent, to cover the brief moment an agent shells out.
        # Tool identity. The prompt decides claude-vs-shell from the whole picture, so we
        # mostly trust it. Sticky bridge is TIME-BOUNDED to resolve the two failure modes
        # that fought each other: a Claude pane shelling out was claude *just now* (bridge
        # it), while a shell mis-tagged claude once was claude *a while ago* (let it go).
        # So: only override a shell/unknown read with a remembered agent if we saw that
        # agent within the last few seconds.
        tool = state.get("tool")
        if tool in ("claude", "codex", "gemini"):
            self._tool[pane.id] = (tool, now)
        elif tool in ("shell", "unknown", None):
            prev = self._tool.get(pane.id)
            if prev and now - prev[1] < 8:
                state["tool"] = prev[0]
            else:
                self._tool.pop(pane.id, None)  # agent is genuinely gone; forget it

        # The pane's self-published title (see Pane.display_title) — the agent's own
        # words for what it's doing, better than anything we could parse off the screen.
        state["title"] = pane.display_title
        hist = self.snapshots.get(pane.id, [])
        state["snapshot_id"] = hist[-1]["id"] if hist else None
        state["idle_seconds"] = idle
        state["updated_at"] = now
        self._state[pane.id] = state
        return state
