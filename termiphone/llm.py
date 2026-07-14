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

_MODEL = "gemini-2.5-flash-lite"


@cache
def _client():
    """Lazily construct the Vertex client once. Cached so we don't rebuild per call."""
    from google import genai

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set; cannot reach Vertex.")
    location = os.environ.get("VERTEX_AI_REGION_GEMINI", "global")
    return genai.Client(vertexai=True, project=project, location=location)


def classify_text(system: str, text: str) -> dict | None:
    """Send pane text to Flash Lite and get structured JSON back. Returns the parsed
    dict, or None on any failure (the caller falls back to heuristics-only)."""
    try:
        from google.genai import types

        resp = _client().models.generate_content(
            model=_MODEL,
            contents=[text],
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        return json.loads(resp.text)
    except Exception:  # noqa: BLE001 - lazy pass must never break the watcher
        logger.warning("Gemini classify pass failed; using heuristics only", exc_info=True)
        return None
