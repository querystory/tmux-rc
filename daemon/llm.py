"""Minimal Gemini-Flash-Lite-via-Vertex client for the lazy classification pass.

This is a deliberate ~1-screen reimplementation of the small slice of qs-app's
`backend.genai.llm` we need, so tmux-rc stays standalone (no FastAPI/SQLAlchemy/
WorkOS pull-in). Credentials come from Google ADC; only GOOGLE_CLOUD_PROJECT and an
optional region are read from the environment.
"""

from __future__ import annotations

import json
import logging
import os
import time
from functools import cache

logger = logging.getLogger(__name__)

# Gemini 3.1 Flash Lite — cheap/fast, strong at reading terminal text & screenshots.
# Override with TMUXRC_GEMINI_MODEL if a newer flash-lite ships.
_MODEL = os.environ.get("TMUXRC_GEMINI_MODEL", "gemini-3.1-flash-lite")
# Flash-Lite pricing (USD per 1M tokens); override if it changes.
_IN_PER_M = float(os.environ.get("TMUXRC_IN_PER_M", "0.10"))
_OUT_PER_M = float(os.environ.get("TMUXRC_OUT_PER_M", "0.40"))

# Durable per-call metrics log (JSONL): one line per LLM call with tokens/cost/latency,
# so "add it all up / averages" is a real query (and the seed for QueryStory
# introspection). We had this data in every response's usage_metadata and were throwing
# it away — now it's recorded. Live totals also kept in-memory (see usage_totals()).
_metrics = logging.getLogger("daemon.llm.metrics")
if not _metrics.handlers:
    _mpath = os.environ.get("TMUXRC_METRICS_LOG", "/tmp/tmux-rc-metrics.jsonl")
    _mh = logging.FileHandler(_mpath)
    _mh.setFormatter(logging.Formatter("%(message)s"))
    _metrics.addHandler(_mh)
    _metrics.setLevel(logging.INFO)
    _metrics.propagate = False

_totals = {"calls": 0, "in_tokens": 0, "out_tokens": 0, "cost": 0.0, "errors": 0}
_started = time.time()


def usage_totals() -> dict:
    """Running totals since the server started, plus a plain average calls/min over the
    whole session (total calls / uptime) — stable, no noisy per-poll deltas."""
    mins = max((time.time() - _started) / 60, 1 / 60)
    return {**_totals, "rate_per_min": round(_totals["calls"] / mins, 1)}


# Dedicated LLM trace log so we can grep exactly what the model saw and returned.
# Path override via TMUXRC_LLM_LOG; default alongside the repo. tail -f to watch.
_trace = logging.getLogger("daemon.llm.trace")
if not _trace.handlers:
    _path = os.environ.get("TMUXRC_LLM_LOG", "/tmp/tmux-rc-llm.log")
    _h = logging.FileHandler(_path)
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _trace.addHandler(_h)
    _trace.setLevel(logging.INFO)
    _trace.propagate = False


@cache
def _client():
    """Lazily construct the Vertex client once. Cached so we don't rebuild per call."""
    from google import genai

    from google.genai import types

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set; cannot reach Vertex.")
    location = os.environ.get("VERTEX_AI_REGION_GEMINI", "global")
    # Per-request timeout on EVERY call. Without it, a hung ADC token refresh or a stalled
    # Vertex socket blocks the parse thread forever — the watcher loop awaits that thread
    # and never ticks again, freezing all cards with NO recorded error (exactly the ~9h
    # "stale, errors:0" wedge we hit). A timeout turns that hang into a caught exception
    # the parse path already handles (falls back, records the error).
    #
    # HttpOptions.timeout is MILLISECONDS in google-genai (verified against the field's own
    # description). That unit has historically been seconds in some clients, so a future
    # SDK bump that flipped it would silently turn 20s into a ~5.5h no-op timeout — assert
    # the ms contract so such a change fails loudly at startup instead.
    timeout_ms = int(os.environ.get("TMUXRC_LLM_TIMEOUT_MS", "20000"))
    _desc = (types.HttpOptions.model_fields["timeout"].description or "").lower()
    if "millisecond" not in _desc:  # not assert: must survive `python -O`
        raise RuntimeError(
            f"google-genai HttpOptions.timeout unit changed ({_desc!r}); "
            "re-check TMUXRC_LLM_TIMEOUT_MS is still milliseconds"
        )
    return genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=types.HttpOptions(timeout=timeout_ms),
    )


# Shared 429 backoff. Vertex quota errors come in bursts; without a backoff every
# heartbeat re-parse burns another call (and logged another full traceback) while the
# quota window resets. On 429 we stop calling for `delay` seconds (15s doubling to a
# 2min cap), and any success resets it.
_backoff = {"delay": 0.0, "until": 0.0}


def _backoff_remaining() -> float:
    """Seconds until we may call Vertex again (0 = not backing off)."""
    return max(0.0, _backoff["until"] - time.time())


def _handle_llm_error(e: Exception) -> str:
    """Log an LLM failure at the right fidelity and return a short user-facing message.

    Known OPERATIONAL failures — quota (429), expired auth, timeouts, malformed model
    JSON — are states of the world, not bugs: they log as one line. A full stack trace
    for an expected condition is noise that buries real bugs (a 429 burst was dumping
    dozens of 40-line tracebacks into the daemon output). Unknown failures keep
    exc_info so real bugs stay debuggable.
    A 429 also arms the shared backoff so the watcher stops hammering Vertex."""
    msg = str(e)
    if getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in msg or " 429 " in msg:
        delay = min(_backoff["delay"] * 2, 120.0) or 15.0
        _backoff.update(delay=delay, until=time.time() + delay)
        short = f"rate limited (429) — backing off {delay:.0f}s"
        logger.warning("LLM %s", short)
    elif "Reauthentication is needed" in msg or "RefreshError" in type(e).__name__:
        short = "Google auth expired — run: gcloud auth application-default login"
        logger.warning("LLM parse failed: %s", short)
    elif "timeout" in msg.lower() or "Timeout" in type(e).__name__:
        short = "Vertex request timed out"
        logger.warning("LLM parse failed: %s", short)
    elif "JSONDecodeError" in type(e).__name__:
        # A misbehaving model is the same class of event as a 429 — a state of the
        # world, not a bug in this code. One line, no traceback.
        short = f"model returned malformed JSON: {msg[:80]}"
        logger.warning("LLM parse failed: %s", short)
    else:
        short = msg[:200]
        logger.warning("LLM parse failed (unexpected)", exc_info=True)
    return short


def _parse_json(text: str) -> dict:
    """Parse the model's JSON reply, tolerating trailing garbage.

    flash-lite sometimes emits a valid object and then keeps going (e.g. the same block
    duplicated) even with response_mime_type=application/json. Plain json.loads raises
    "Extra data" on that tail and throws away a perfectly good parse. Take the first
    object and ignore the rest; only output with no leading object still raises."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        stripped = text.strip()  # raw_decode chokes on leading whitespace
        obj, end = json.JSONDecoder().raw_decode(stripped)
        logger.warning(
            "LLM model returned trailing data after JSON; salvaged first object"
        )
        _trace.info("TRAILING after salvaged JSON: %r", stripped[end:][:500])
        return obj


def classify_text(
    system: str, text: str, image_png: bytes | None = None, changed: bool = True
) -> dict | None:
    """Send pane text (and optionally a rendered screenshot) to Flash Lite and get
    structured JSON back. Returns the parsed dict, or None on any failure (caller falls
    back). `image_png` is wired for the future image-input mode; text-only by default.
    `changed` (a real content-change parse vs a heartbeat re-parse) is passed straight
    through to the benchmark telemetry."""
    wait = _backoff_remaining()
    if wait > 0:
        # Rate-limited: skip the call entirely (no tokens burned, no telemetry — nothing
        # happened). The UI still shows why cards are degrading via last_error.
        last_error["msg"] = f"rate limited (429) — retrying in {wait:.0f}s"
        return None
    t0 = time.time()
    try:
        from google.genai import types

        parts: list = [text]
        if image_png is not None:
            parts.append(types.Part.from_bytes(data=image_png, mime_type="image/png"))
        resp = _client().models.generate_content(
            model=_MODEL,
            contents=parts,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        result = _parse_json(resp.text)
        # Trace the tail we sent and what came back — grep /tmp/tmux-rc-llm.log.
        _trace.info("IN%s: %r", "+img" if image_png else "", text[-500:])
        _trace.info("OUT: %s", json.dumps(result))
        _record(resp)  # tokens/cost/latency → metrics jsonl + running totals
        _emit(
            text, result, time.time() - t0, resp, changed, None
        )  # OTLP benchmark record
        last_error["msg"] = None  # success clears any prior error
        _backoff.update(delay=0.0, until=0.0)  # healthy again; forget the 429 streak
        return result
    except Exception as e:  # noqa: BLE001 - parse pass must never break the watcher
        _trace.info("ERROR on IN: %r", text[-500:])
        # Remember WHY (last_error → UI shows "LLM unavailable: <reason>" instead of
        # silently degrading to a blank heuristic card). _handle_llm_error picks the log
        # fidelity and arms the 429 backoff.
        msg = _handle_llm_error(e)
        last_error["msg"] = msg[:200]
        _totals["errors"] += 1
        _metrics.info(json.dumps({"ts": time.time(), "error": msg[:120]}))
        _emit(
            text, None, time.time() - t0, None, changed, msg
        )  # record the failure too
        return None


def summarize_events(event_texts: list[str]) -> str | None:
    """One-line summary of a burst of activity events (for the idle collapse). Returns
    plain text, or None on failure. Cheap: no schema, tiny output."""
    if not event_texts or _backoff_remaining() > 0:
        return None  # skip while rate-limited — same gate as classify_text
    try:
        from google.genai import types

        joined = "\n".join(f"- {t}" for t in event_texts[-60:])
        resp = _client().models.generate_content(
            model=_MODEL,
            contents=[
                f"Summarize this burst of terminal activity in ONE short sentence "
                f"(what was accomplished, past tense):\n{joined}"
            ],
            config=types.GenerateContentConfig(temperature=0.0),
        )
        _record(resp)
        return (resp.text or "").strip()[:200] or None
    except Exception as e:  # noqa: BLE001
        _handle_llm_error(e)  # was fully silent before; now logs + arms 429 backoff
        _totals["errors"] += 1
        return None


def _tokens_cost(resp) -> tuple[int, int, float]:
    """(in_tokens, out_tokens, cost) off a Gemini response — one source of truth for how
    usage comes off the wire, shared by _record (running totals) and _emit (telemetry)."""
    u = getattr(resp, "usage_metadata", None)
    in_tok = getattr(u, "prompt_token_count", 0) or 0
    out_tok = getattr(u, "candidates_token_count", 0) or 0
    return in_tok, out_tok, in_tok / 1e6 * _IN_PER_M + out_tok / 1e6 * _OUT_PER_M


def _record(resp) -> None:
    """Update running totals and append a metrics line. Best-effort — metering must
    never break a parse."""
    try:
        in_tok, out_tok, cost = _tokens_cost(resp)
        _totals["calls"] += 1
        _totals["in_tokens"] += in_tok
        _totals["out_tokens"] += out_tok
        _totals["cost"] += cost
        _metrics.info(
            json.dumps(
                {
                    "ts": time.time(),
                    "in": in_tok,
                    "out": out_tok,
                    "cost": round(cost, 6),
                }
            )
        )
    except Exception:  # noqa: BLE001
        pass


def _emit(
    text: str,
    result: dict | None,
    latency: float,
    resp,
    changed: bool,
    error: str | None,
) -> None:
    """Adapt a Gemini call to the OTLP benchmark record. Best-effort. TTFT isn't exposed
    by the non-streaming google-genai call, so it's left None here (a streaming provider
    path can fill it)."""
    try:
        from .telemetry import emit_parse

        in_tok, out_tok, cost = _tokens_cost(resp) if resp is not None else (0, 0, 0.0)
        emit_parse(
            model=_MODEL,
            provider="vertex",
            pane_text=text,
            output=result,
            latency=latency,
            ttft=None,
            in_tokens=in_tok,
            out_tokens=out_tok,
            cost=cost,
            activity=(result or {}).get("activity"),
            changed=changed,
            error=error,
        )
    except Exception:  # noqa: BLE001 - telemetry must never break a parse
        logger.debug("telemetry emit failed", exc_info=True)  # visible, but never fatal


# Last LLM failure reason (or None if the last call succeeded), surfaced to the UI.
last_error: dict = {"msg": None}
