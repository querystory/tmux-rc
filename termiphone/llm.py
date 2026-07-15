"""Minimal Gemini-Flash-Lite-via-Vertex client for the lazy classification pass.

This is a deliberate ~1-screen reimplementation of the small slice of qs-app's
`backend.genai.llm` we need, so termiphone stays standalone (no FastAPI/SQLAlchemy/
WorkOS pull-in). Credentials come from Google ADC; only GOOGLE_CLOUD_PROJECT and an
optional region are read from the environment.
"""

from __future__ import annotations

import json
import logging
import os
from functools import cache

logger = logging.getLogger(__name__)

# Gemini 3.1 Flash Lite — cheap/fast, strong at reading terminal text & screenshots.
# Override with TERMIPHONE_GEMINI_MODEL if a newer flash-lite ships.
_MODEL = os.environ.get("TERMIPHONE_GEMINI_MODEL", "gemini-3.1-flash-lite")

# Dedicated LLM trace log so we can grep exactly what the model saw and returned.
# Path override via TERMIPHONE_LLM_LOG; default alongside the repo. tail -f to watch.
_trace = logging.getLogger("termiphone.llm.trace")
if not _trace.handlers:
    _path = os.environ.get("TERMIPHONE_LLM_LOG", "/tmp/termiphone-llm.log")
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
        # Trace the tail we sent and what came back — grep /tmp/termiphone-llm.log.
        _trace.info("IN%s: %r", "+img" if image_png else "", text[-500:])
        _trace.info("OUT: %s", json.dumps(result))
        return result
    except Exception:  # noqa: BLE001 - parse pass must never break the watcher
        logger.warning("Gemini parse pass failed", exc_info=True)
        _trace.info("ERROR on IN: %r", text[-500:])
        return None
