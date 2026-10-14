"""Live chat routing: does a typed turn reach the pane the user means, and leave alone a
pane's prompt that the user was not answering?

    python -m research.eval.chat_routing [--model "Claude Sonnet 5.5"] [--runs 5] [--case NAME]

Each case in `chat_routing/` is a real conversation, anonymised: the panes as the chat saw
them, the turns before (the assistant's replies fixed, as the user saw them) and the turn
under test. Every turn is built by production code: the real loop (live_chat), the pane a
reply names becoming the next turn's current pane (live._named, live._focus) and a reply to
the assistant's own question marked as one. Only the last turn reaches the model, with the
real system prompt and tools. Nothing is sent: each call is recorded and answered declined.

A run FAILS if any call reaches a pane other than `expect.pane`, presses a key, or types a
bare menu answer (a word or two). Otherwise it is "relayed" (typed into that pane) or
"asked" (no call, and the reply names that pane); a reply naming no pane, or another, fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from openbus import live, live_chat
from openbus.live_providers import Split

CASES = Path(__file__).parent / "chat_routing"
_DECLINED = {"status": "declined", "reason": "eval: nothing is sent"}


class _Panes:
    """The watcher surface the system prompt reads: each pane's digest entry and screen."""

    def __init__(self, panes: list[dict]) -> None:
        self._digest = [{k: v for k, v in p.items() if k != "screen"} for p in panes]
        self.snapshots = {p["pane_id"]: [{"text": p.get("screen", "")}] for p in panes}

    def digest(self) -> list[dict]:
        return self._digest


async def _turn(s, digest: list[dict], text: str) -> tuple[list, str]:
    """One typed turn through the real loop, as live._receiver runs it: (calls, reply)."""
    await s.send_text(text)
    calls, said = [], []
    async for ev in s.events():
        if ev.kind == "transcript":
            said.append(ev.text)
            live._focus(s, live._named(digest, ev.text))
        elif ev.kind == "tool_call":
            calls.append(ev.call)
            await s.send_tool_result(ev.call, _DECLINED)
        elif ev.kind == "turn_complete":
            return calls, "".join(said)
    raise RuntimeError("the session ended mid-turn")


async def _run(case: dict, model) -> dict:
    panes = _Panes(case["panes"])
    async with live_chat.open_session(model, live._system_prompt(panes, text=True)) as s:
        for user, assistant in case["before"]:
            async def fixed(tools=True, reply=assistant):
                s._model(reply)
                return reply, [], Split(*[0] * len(Split._fields))
            s._complete = fixed
            await _turn(s, panes.digest(), user)
        s.__dict__.pop("_complete", None)  # the real model answers the turn under test
        calls, reply = await _turn(s, panes.digest(), case["then"])
    return _score(case["expect"]["pane"], calls, reply, panes.digest())


def _score(pane: str, calls: list, reply: str, digest: list[dict]) -> dict:
    shown = [(c.name, c.args) for c in calls]
    args = [c.args if isinstance(c.args, dict) else {} for c in calls]
    if any(a.get("pane_id") not in (None, pane) for a in args):
        verdict = "FAIL wrong pane"
    elif any(c.name == "press_key" for c in calls):
        verdict = "FAIL pressed a key"
    elif any(len(str(a.get("text", "")).split()) <= 2 for c, a in zip(calls, args, strict=True)
             if c.name == "type_in_pane"):
        verdict = "FAIL answered a menu"
    elif calls:
        verdict = "relayed"
    else:
        verdict = "asked" if (live._named(digest, reply) or {}).get("pane_id") == pane \
            else "FAIL asked about no pane, or another"
    return {"verdict": verdict, "calls": shown, "reply": reply}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", help="a text model's label (default: the first offered)")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--case", help="one case's file name, without .json")
    a = ap.parse_args()
    model = live.pick(a.model, text=True)
    if not model:
        raise SystemExit(f"no text model {a.model or ''} is configured and keyed")
    failed = 0
    for path in sorted(CASES.glob(f"{a.case or '*'}.json")):
        case = json.loads(path.read_text())
        for i in range(a.runs):
            r = asyncio.run(_run(case, model))
            failed += r["verdict"].startswith("FAIL")
            print(f"{path.stem} #{i + 1} [{model.label}] {r['verdict']}: "
                  f"{r['calls']} {r['reply']!r}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
