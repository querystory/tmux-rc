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

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set; cannot reach Vertex.")
    location = os.environ.get("VERTEX_AI_REGION_GEMINI", "global")
    return genai.Client(vertexai=True, project=project, location=location)


def classify_text(system: str, text: str, image_png: bytes | None = None) -> dict | None:
    """Send pane text (and optionally a rendered screenshot) to Flash Lite and get
    structured JSON back. Returns the parsed dict, or None on any failure (caller falls
    back). `image_png` is wired for the future image-input mode; text-only by default."""
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
        result = json.loads(resp.text)
        # Trace the tail we sent and what came back — grep /tmp/tmux-rc-llm.log.
        _trace.info("IN%s: %r", "+img" if image_png else "", text[-500:])
        _trace.info("OUT: %s", json.dumps(result))
        _record(resp)  # tokens/cost/latency → metrics jsonl + running totals
        last_error["msg"] = None  # success clears any prior error
        return result
    except Exception as e:  # noqa: BLE001 - parse pass must never break the watcher
        logger.warning("Gemini parse pass failed", exc_info=True)
        _trace.info("ERROR on IN: %r", text[-500:])
        # Remember WHY, so the UI can say "LLM unavailable: <reason>" instead of
        # silently degrading to a blank heuristic card. Map the common expired-ADC case
        # to actionable text.
        msg = str(e)
        if "Reauthentication is needed" in msg or "RefreshError" in type(e).__name__:
            msg = "Google auth expired — run: gcloud auth application-default login"
        elif "RESOURCE_EXHAUSTED" in msg or "429" in msg:
            msg = "rate limited (429) — backing off"
        last_error["msg"] = msg[:200]
        _totals["errors"] += 1
        _metrics.info(json.dumps({"ts": time.time(), "error": msg[:120]}))
        return None


def summarize_events(event_texts: list[str]) -> str | None:
    """One-line summary of a burst of activity events (for the idle collapse). Returns
    plain text, or None on failure. Cheap: no schema, tiny output."""
    if not event_texts:
        return None
    try:
        from google.genai import types

        joined = "\n".join(f"- {t}" for t in event_texts[-60:])
        resp = _client().models.generate_content(
            model=_MODEL,
            contents=[f"Summarize this burst of terminal activity in ONE short sentence "
                      f"(what was accomplished, past tense):\n{joined}"],
            config=types.GenerateContentConfig(temperature=0.0),
        )
        _record(resp)
        return (resp.text or "").strip()[:200] or None
    except Exception:  # noqa: BLE001
        _totals["errors"] += 1
        return None


def _record(resp) -> None:
    """Pull tokens from the response, estimate cost, update running totals, and append a
    metrics line. Best-effort — metering must never break a parse."""
    try:
        u = resp.usage_metadata
        in_tok = getattr(u, "prompt_token_count", 0) or 0
        out_tok = getattr(u, "candidates_token_count", 0) or 0
        cost = in_tok / 1e6 * _IN_PER_M + out_tok / 1e6 * _OUT_PER_M
        _totals["calls"] += 1
        _totals["in_tokens"] += in_tok
        _totals["out_tokens"] += out_tok
        _totals["cost"] += cost
        _metrics.info(json.dumps(
            {"ts": time.time(), "in": in_tok, "out": out_tok, "cost": round(cost, 6)}
        ))
    except Exception:  # noqa: BLE001
        pass


# Last LLM failure reason (or None if the last call succeeded), surfaced to the UI.
last_error: dict = {"msg": None}
