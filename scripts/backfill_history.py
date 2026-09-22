#!/usr/bin/env python3
"""Match journal pane IDs to LLM trace states; never guess identity from agent titles.

The old trace omitted pane IDs. A watcher 'dropped re-emitted event' message includes
one, plus an exact prefix of an event in the model response. Accept only a UNIQUE
response within two seconds. This recovers partial state observations, not complete
inventories. Require independently verified pane lifetimes; a server lifetime alone
cannot distinguish pane ID reuse or respawn.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import json
import math
import re
import socket
from collections import Counter
from datetime import datetime
from pathlib import Path

from openbus.history import History, default_path, state_index

PATTERN = re.compile(r"(%\d+): dropped \d+ re-emitted event\(s\), e.g. (.+)$")


def reconstruct(trace: Path, journal: Path, server_uid: str, since: float,
                lifetimes: list[dict]):
    # Each interval must be evidenced independently (e.g. process start time and a
    # still-live pane PID), never inferred from the state observations being joined.
    for life in lifetimes:
        if (life.get("server") != server_uid or not re.fullmatch(r"%\d+", life.get("pane_id", ""))
                or not str(life.get("birth", "")) or not math.isfinite(life["start"])
                or not math.isfinite(life["end"]) or life["start"] >= life["end"]):
            raise ValueError("Invalid pane lifetime")
    boot = server_uid.split(":", maxsplit=1)[0].replace("-", "")
    outputs = []
    report = Counter()
    with trace.open() as stream:
        for line in stream:
            if " OUT: " not in line: continue
            stamp, raw = line.split(" OUT: ", 1)
            try:
                data = json.loads(raw)
                # FileHandler uses the host's local timezone. The journal's epoch is
                # the independent join key; a timezone mismatch yields no matches.
                t = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f").timestamp()  # noqa: DTZ007
            except (ValueError, TypeError):
                report["invalid_trace"] += 1
                continue
            if t < since or not isinstance(data, dict): continue
            if data.get("activity") not in {"running", "waiting", "idle", "compacting", "unknown"}:
                continue
            outputs.append((t, data))
    outputs.sort(key=lambda row: row[0])
    times = [r[0] for r in outputs]
    matches = {}
    with journal.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
                t = int(row["__REALTIME_TIMESTAMP"]) / 1e6
                match = PATTERN.search(row.get("MESSAGE", ""))
                if (t < since or row.get("_HOSTNAME") != socket.gethostname()
                        or row.get("_BOOT_ID", "").replace("-", "") != boot
                        or row.get("_SYSTEMD_USER_UNIT") != "tmux-rc.service" or not match):
                    continue
                example = ast.literal_eval(match[2])
            except (ValueError, TypeError, KeyError, SyntaxError):
                report["invalid_journal"] += 1
                continue
            candidates = []
            left, right = bisect.bisect_left(times, t - 2), bisect.bisect_right(times, t + .1)
            for index in range(left, right):
                ot, out = outputs[index]
                events = out.get("events")
                if not isinstance(events, list): continue
                if any(isinstance(e, dict) and str(e.get("text", ""))[:80] == example
                       for e in events):
                    candidates.append((index, ot, out))
            if len(candidates) != 1:
                report["ambiguous" if candidates else "unmatched"] += 1
                continue
            index, ot, out = candidates[0]
            lives = [life for life in lifetimes if life["pane_id"] == match[1]
                     and life["start"] <= ot <= t < life["end"]]
            if len(lives) != 1:
                report["unverified_lifetime"] += 1
                continue
            life = lives[0]
            uid = f"{server_uid}:{match[1]}:{life['birth']}"
            matches.setdefault(index, {})[uid] = (
                ot, uid, out.get("tool") or "other", state_index(out), life["end"],
            )
    observations = []
    for identities in matches.values():
        if len(identities) != 1:
            report["ambiguous_identity"] += len(identities)
            continue
        observations.extend(identities.values())
    report["matched"] = len(observations)
    return sorted(observations), dict(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--server-uid", required=True, help="Current tmux boot_id:pid")
    parser.add_argument("--since", type=float, required=True,
                        help="Current tmux server start, Unix seconds")
    parser.add_argument("--lifetimes", type=Path, required=True,
                        help="Verified JSON intervals: server, pane_id, birth, start, end")
    parser.add_argument("--db", type=Path, default=default_path())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    observations, report = reconstruct(args.trace, args.journal, args.server_uid, args.since,
                                       json.loads(args.lifetimes.read_text()))
    report["distinct_panes"] = len({r[1] for r in observations})
    report["first"] = observations[0][0] if observations else None
    report["last"] = observations[-1][0] if observations else None
    if not args.dry_run:
        report["inserted"] = History(args.db).import_logs(observations)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
