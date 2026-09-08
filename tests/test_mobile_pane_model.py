"""Behavioural checks for the pure pane helpers in web/m/pane-model.js.

The module is plain ESM with no DOM dependency, so node can import it directly.
Each scenario below is rendered into one .mjs script that imports the module
and asserts with node:assert/strict; a passing script prints nothing, and a
failing one names the scenario in its stderr.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
MODULE = (WEB / "m" / "pane-model.js").as_uri()

NOW_MS = 1_700_000_000_000
NOW_S = NOW_MS / 1000

IDLE_5_MIN_AGO = {"activity": "idle", "state_since": NOW_S - 300}
IDLE_11_MIN_AGO = {"activity": "idle", "state_since": NOW_S - 660}
IDLE_AT_BOUNDARY = {"activity": "idle", "state_since": NOW_S - 600}

# (description, function, args, expected)
CASES = [
    ("constant: parked threshold is ten minutes", "PARKED_IDLE_SECS", None, 600),
    # needsYou
    (
        "needsYou: waiting with nobody named waits on the user",
        "needsYou",
        [{"activity": "waiting"}],
        True,
    ),
    (
        "needsYou: waiting on an external tool is not on the user",
        "needsYou",
        [{"activity": "waiting", "waiting_on": "external"}],
        False,
    ),
    (
        "needsYou: a running pane needs nobody",
        "needsYou",
        [{"activity": "running"}],
        False,
    ),
    # activityClass
    (
        "activityClass: waiting on external shows as running",
        "activityClass",
        [{"activity": "waiting", "waiting_on": "external"}],
        "running",
    ),
    (
        "activityClass: waiting on the user stays waiting",
        "activityClass",
        [{"activity": "waiting"}],
        "waiting",
    ),
    ("activityClass: idle stays idle", "activityClass", [{"activity": "idle"}], "idle"),
    # isRunning
    ("isRunning: running", "isRunning", [{"activity": "running"}], True),
    (
        "isRunning: compacting counts as running",
        "isRunning",
        [{"activity": "compacting"}],
        True,
    ),
    (
        "isRunning: waiting on external counts as running",
        "isRunning",
        [{"activity": "waiting", "waiting_on": "external"}],
        True,
    ),
    ("isRunning: idle is not running", "isRunning", [{"activity": "idle"}], False),
    (
        "isRunning: waiting on the user is not running",
        "isRunning",
        [{"activity": "waiting"}],
        False,
    ),
    # isRecent (nowMs injected)
    (
        "isRecent: idle for five minutes is recent",
        "isRecent",
        [IDLE_5_MIN_AGO, NOW_MS],
        True,
    ),
    (
        "isRecent: idle for eleven minutes is parked",
        "isRecent",
        [IDLE_11_MIN_AGO, NOW_MS],
        False,
    ),
    (
        "isRecent: idle for exactly ten minutes is parked",
        "isRecent",
        [IDLE_AT_BOUNDARY, NOW_MS],
        False,
    ),
    (
        "isRecent: without state_since, a short idle_seconds is recent",
        "isRecent",
        [{"activity": "idle", "idle_seconds": 30}, NOW_MS],
        True,
    ),
    (
        "isRecent: without state_since, a long idle_seconds is parked",
        "isRecent",
        [{"activity": "idle", "idle_seconds": 700}, NOW_MS],
        False,
    ),
    (
        "isRecent: a non-idle pane is recent however old its state",
        "isRecent",
        [{"activity": "running", "state_since": NOW_S - 86_400}, NOW_MS],
        True,
    ),
    (
        "isRecent: state_since as a numeric string is honoured",
        "isRecent",
        [{"activity": "idle", "state_since": str(NOW_S - 660)}, NOW_MS],
        False,
    ),
    # matchesFilter
    (
        "matchesFilter: attention keeps panes waiting on the user",
        "matchesFilter",
        [{"activity": "waiting"}, "attention", NOW_MS],
        True,
    ),
    (
        "matchesFilter: attention drops panes waiting on external",
        "matchesFilter",
        [{"activity": "waiting", "waiting_on": "external"}, "attention", NOW_MS],
        False,
    ),
    (
        "matchesFilter: running keeps compacting panes",
        "matchesFilter",
        [{"activity": "compacting"}, "running", NOW_MS],
        True,
    ),
    (
        "matchesFilter: running drops idle panes",
        "matchesFilter",
        [{"activity": "idle"}, "running", NOW_MS],
        False,
    ),
    (
        "matchesFilter: recent keeps a pane idle for five minutes",
        "matchesFilter",
        [IDLE_5_MIN_AGO, "recent", NOW_MS],
        True,
    ),
    (
        "matchesFilter: recent drops a pane idle for eleven minutes",
        "matchesFilter",
        [IDLE_11_MIN_AGO, "recent", NOW_MS],
        False,
    ),
    (
        "matchesFilter: all keeps a parked pane",
        "matchesFilter",
        [IDLE_11_MIN_AGO, "all", NOW_MS],
        True,
    ),
    (
        "matchesFilter: an unknown filter behaves like all",
        "matchesFilter",
        [IDLE_11_MIN_AGO, "bogus", NOW_MS],
        True,
    ),
    # lastActivity
    (
        "lastActivity: a server-provided last_activity_at wins verbatim",
        "lastActivity",
        [
            {
                "activity": "idle",
                "last_activity_at": 1234.5,
                "updated_at": 9999,
                "idle_seconds": 1,
                "state_since": 1,
            }
        ],
        1234.5,
    ),
    (
        "lastActivity: falls back to updated_at minus idle_seconds",
        "lastActivity",
        [{"activity": "running", "updated_at": 1000, "idle_seconds": 40}],
        960,
    ),
    (
        "lastActivity: an idle pane takes the earlier of that and state_since",
        "lastActivity",
        [
            {
                "activity": "idle",
                "updated_at": 1000,
                "idle_seconds": 40,
                "state_since": 900,
            }
        ],
        900,
    ),
    (
        "lastActivity: an idle pane keeps the fallback when state_since is later",
        "lastActivity",
        [
            {
                "activity": "idle",
                "updated_at": 1000,
                "idle_seconds": 40,
                "state_since": 980,
            }
        ],
        960,
    ),
    (
        "lastActivity: a non-idle pane ignores state_since",
        "lastActivity",
        [
            {
                "activity": "running",
                "updated_at": 1000,
                "idle_seconds": 40,
                "state_since": 900,
            }
        ],
        960,
    ),
    (
        "lastActivity: non-numeric fields are treated as zero",
        "lastActivity",
        [
            {
                "activity": "idle",
                "updated_at": "soon",
                "idle_seconds": None,
                "state_since": "never",
            }
        ],
        0,
    ),
]

FILTER_NAMES = ["all", "running", "recent", "attention"]
for invalid in (None, "", "idle extra-class", "__proto__", "constructor", 42, {}):
    for helper in ("activityClass", "activityLabel"):
        CASES.append((
            f"{helper}: reject unexpected activity {invalid!r}",
            helper, [{"activity": invalid}],
            "unknown" if helper == "activityClass" else "Unknown",
        ))


def render_script() -> str:
    checks = "\n".join(
        f"check({json.dumps(description)}, () => m.{name}"
        + ("" if args is None else f"(...{json.dumps(args)})")
        + f", {json.dumps(expected)});"
        for description, name, args, expected in CASES
    )
    return f"""
import assert from "node:assert/strict";
import * as m from {json.dumps(MODULE)};

const failures = [];
const check = (description, actual, expected) => {{
  try {{ assert.deepEqual(actual(), expected); }}
  catch (err) {{ failures.push(`${{description}}: ${{err.message}}`); }}
}};

{checks}
check("FILTERS exposes exactly the four filter names", () => Object.keys(m.FILTERS).sort(), {json.dumps(sorted(FILTER_NAMES))});

if (failures.length) {{
  console.error(failures.join("\\n\\n"));
  process.exit(1);
}}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_pane_model_behaviour(tmp_path):
    script = tmp_path / "pane_model_test.mjs"
    script.write_text(render_script())
    res = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, res.stderr
