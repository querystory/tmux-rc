"""Env-driven JSON list config — the one shape launchers and Live models share: an env var
holding an inline JSON list, or a path to a file containing one."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)


def dotenv_path() -> Path:
    """The .env the daemon loads: the checkout's, beside the package, else tmux-rc's config
    dir's (installed as a wheel, or none yet: export's import writes it there)."""
    repo = Path(__file__).resolve().parent.parent / ".env"
    return repo if repo.exists() else Path.home() / ".config/tmux-rc/.env"


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
        good = [coerce(e) for e in json.loads(raw)]  # a non-dict raises inside coerce
        if not good:
            # Raised, not returned: "the config parsed to nothing usable" is the same
            # outcome as "the config did not parse", and routing both through the one
            # handler keeps one warning and one fallback instead of two of each.
            raise ValueError("no valid entries")  # noqa: TRY301
        return good
    except Exception:
        logger.warning("%s invalid; using defaults", var, exc_info=True)
        return default
