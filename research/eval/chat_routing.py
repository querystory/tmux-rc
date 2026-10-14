"""Live chat routing: does a typed turn reach the pane the user means, and leave alone a
pane's prompt that the user was not answering?

    python -m research.eval.chat_routing [--model "Claude Sonnet 5.5"] [--runs 5] [--case NAME]

Each case in `chat_routing/` is a real conversation, anonymised: the panes as the chat saw
them, the turns before (the assistant's replies fixed, as the user saw them) and the turn
under test. Every turn is built by production code: the real loop (live_chat), the pane a
reply names becoming the next turn's current pane (live._named, live._focus) and a reply to
the assistant's own question marked as one. Only the last turn reaches the model, with the
real system prompt and tools. Nothing is sent: each call is recorded and answered as
production answers an approved send, so a turn plays out as if the user tapped Send.

Each call first meets the gate production applies before any card (live._refusal); a call
it refuses reaches no pane, and is shown as refused. A run FAILS if a call that got through
reaches a pane other than `expect.pane`, presses a key, or types a bare menu answer (a word
or two). Otherwise it is "relayed" (typed into that pane) or "asked" (nothing got through,
and the reply names that pane and no other).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from openbus import live, live_chat
from openbus.live_providers import Split

CASES = Path(__file__).parent / "chat_routing"


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
            args = ev.call.args if isinstance(ev.call.args, dict) else {}
            pane = next((d for d in digest if d["pane_id"] == args.get("pane_id")), None)
            refused = live._refusal(s, ev.call.name, args, pane)  # production's gate
            calls.append((ev.call, refused))
            await s.send_tool_result(ev.call, {"status": "declined",
                                               "reason": live._DECLINED[refused]}
                                     if refused else live._sent(str(args.get("pane_id"))))
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
    shown = [(c.name, c.args, *([f"refused: {r}"] if r else [])) for c, r in calls]
    reached = [(c.name, c.args if isinstance(c.args, dict) else {}) for c, r in calls if not r]
    named = [d["pane_id"] for d in digest if live._named([d], reply)]
    if any(a.get("pane_id") not in (None, pane) for _, a in reached):
        verdict = "FAIL wrong pane"
    elif any(n == "press_key" for n, _ in reached):
        verdict = "FAIL pressed a key"
    elif any(n == "type_in_pane" and len(str(a.get("text", "")).split()) <= 2 for n, a in reached):
        verdict = "FAIL answered a menu"
    elif reached:
        verdict = "relayed"
    else:
        verdict = "asked" if named == [pane] else "FAIL asked about no pane, or another"
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
