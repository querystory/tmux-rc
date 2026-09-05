"""Env-driven JSON list config — the one shape launchers and Live models share: an env var
holding an inline JSON list, or a path to a file containing one."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)


def json_list(var: str, default: list, coerce: Callable[[dict], object]) -> list:
    """Parse `var` (inline JSON list or a path to one), passing each dict entry through
    `coerce` — which raises on anything malformed. A broken config must never brick the
    feature: it logs and yields `default`, the same as an unset var. All-or-nothing on
    purpose: a typo in one entry is a warning in the log, not one silently missing item."""
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    try:
        if not raw.startswith("["):
            raw = Path(raw).read_text(encoding="utf-8")
        good = [coerce(e) for e in json.loads(raw) if isinstance(e, dict)]
        if not good:
            raise ValueError("no valid entries")
        return good
    except Exception:
        logger.warning("%s invalid; using defaults", var, exc_info=True)
        return default
