"""Play cases.json against a real model and score where its sends went. Billed: opt-in.

    python -m research.eval.live                          # GPT-Live's backend, 1 run
    python -m research.eval.live --runs 3 --case named_window
    python -m research.eval.live --model gemini-3-flash-preview   # Vertex (GOOGLE_CLOUD_PROJECT)
    python -m research.eval.live --effort medium          # A/B the backend's reasoning effort
    python -m research.eval.live --prompt candidate.txt   # A/B a live_prompt.txt edit

Exit 0 when every case passed every run. Rationale: research/eval/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from unittest import mock

import httpx
from dotenv import load_dotenv

from openbus import gpt_live, live, live_chat, live_providers
from openbus.live_providers import LiveModel, Split, ToolCall

from . import Fleet, cases, replay, score


class Backend(live_chat._Chat):  # the text sessions' own tool loop
    """GPT-Live's reasoning backend on its own: the Responses model gpt_live.run_session
    delegates to, with its instructions, tools and effort. A typed turn goes straight to it
    in a live session too; what this leaves out is the voice front end's handoff."""

    def __init__(self, model: LiveModel, system: str, client, effort: str) -> None:
        super().__init__(model, f"{system}\n\n{gpt_live.BACKEND_PROMPT}")
        self._client, self._effort = client, effort

    def _user(self, text: str, images=()) -> None:
        self.history.append(live_providers.openai_message("user", text, images))

    def _model(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})

    async def _complete(self, *, tools: bool = True):
        r = await self._client.post("responses", json={
            "model": self.model.model, "instructions": self.system, "input": self.history,
            "tools": gpt_live.tool_definitions(), "tool_choice": "auto" if tools else "none",
            "parallel_tool_calls": False, "reasoning": {"effort": self._effort},
            "store": False, "include": ["reasoning.encrypted_content"],
        })
        r.raise_for_status()
        body = r.json()
        out = [{k: v for k, v in o.items() if k not in {"id", "status"}} for o in body["output"]]
        self.history += out
        reply = "".join(c.get("text", "") for o in out if o["type"] == "message"
                        for c in o["content"])
        calls = [ToolCall(o["call_id"], o["name"], live_providers._args(o["arguments"]))
                 for o in out if o["type"] == "function_call"]
        u = body.get("usage") or {}
        cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
        return reply, calls, Split(u.get("input_tokens", 0) - cached,
                                   u.get("output_tokens", 0), 0, 0, cached, 0)

    def _results(self, answers) -> None:
        self.history += [{"type": "function_call_output", "call_id": c.id,
                          "output": json.dumps(p)} for c, p in answers]


def _opener(effort: str):
    """The provider seam for a real model: a chat entry through live_chat as the daemon
    opens one, anything else as GPT-Live's backend over the OpenAI API."""
    @contextlib.asynccontextmanager
    async def connect(model: LiveModel, prompt: str):
        if model.backend != "openai":
            async with live_chat.open_session(model, prompt) as session:
                yield session
            return
        headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
        async with httpx.AsyncClient(base_url="https://api.openai.com/v1/", headers=headers,
                                     timeout=120) as client:
            yield Backend(model, prompt, client, effort)
    return connect


def _backend(model: str) -> str:
    return ("vertex-chat" if model.startswith("gemini")
            else "anthropic" if model.startswith("claude") else "openai")


async def _run(case: dict, model: LiveModel, connect) -> list[dict]:
    trace = await replay(Fleet.load(), [t["say"] for t in case["turns"]], connect,
                         model=model, seconds=300)
    return [{"say": t["say"], **score(t, trace, n), "sent": trace.sends(n),
             "reply": trace.reply(n), "completed": _completed(trace, n)}
            for n, t in enumerate(case["turns"], 1)]


def _completed(trace, n: int) -> bool:
    """Whether turn n got a real answer: a failed model call (an error frame, the chat
    loop's failure line, a turn that never completed) sends nothing, which must not score
    as a turn that rightly sent nothing."""
    return bool(trace.of("turn_complete", n)) and not trace.of("error", n) and (
        live_chat._FAILED not in trace.reply(n))


def _passed(turns: list[dict]) -> bool:
    return all(t["completed"] and t[k] is not False
               for t in turns for k in ("target", "sends", "answered"))


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m research.eval.live")
    ap.add_argument("--model", default=gpt_live.BACKEND,
                    help="gemini-* on Vertex, claude-* on Anthropic, else OpenAI Responses")
    ap.add_argument("--effort", default="low", help="OpenAI reasoning effort (GPT-Live: low)")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--case", help="run only this case")
    ap.add_argument("--prompt", type=Path, help="candidate live_prompt.txt (A/B)")
    ap.add_argument("--json", type=Path, help="write every turn's outcome here")
    args = ap.parse_args()
    if args.runs < 1:
        ap.error("--runs must be at least 1")
    load_dotenv(Path.home() / ".config/tmux-rc/openai.env")  # keys live outside every checkout

    selected = [c for c in cases() if args.case in (None, c["name"])]
    model = LiveModel(args.model, args.model, _backend(args.model))
    load = live._load_prompt
    prompt = args.prompt.read_text(encoding="utf-8") if args.prompt else None
    with mock.patch.object(live, "_load_prompt", lambda name: (
            prompt if prompt and name == "live_prompt.txt" else load(name))):
        print(f"model: {model.model} via {model.backend}"
              + (f", effort {args.effort}" if model.backend == "openai" else "")
              + f"; prompt: {args.prompt or 'production'}; runs: {args.runs}\n")
        connect = _opener(args.effort)
        results = {c["name"]: [asyncio.run(_run(c, model, connect)) for _ in range(args.runs)]
                   for c in selected}

    width = max(map(len, results))
    print(f"{'case':<{width}}  pass  target  sends  answered")
    for name, runs in results.items():
        def tally(key, runs=runs):
            scored = [all(t[key] is not False for t in r) for r in runs
                      if any(t[key] is not None for t in r)]
            return f"{sum(scored)}/{len(scored)}" if scored else "-"
        print(f"{name:<{width}}  {sum(map(_passed, runs))}/{len(runs):<3} {tally('target'):<7} "
              f"{tally('sends'):<6} {tally('answered')}")
        for r in runs:
            for t in r if not _passed(r) else ():
                print(f"    {t['say']!r} -> {[p for p, _, _ in t['sent']]}"
                      f"{'' if t['completed'] else ' (model failed)'}: {t['reply'][:160]!r}")
    total = sum(_passed(r) for runs in results.values() for r in runs)
    print(f"\n{total}/{sum(map(len, results.values()))} case runs passed")
    if args.json:
        args.json.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    return 0 if total == sum(map(len, results.values())) else 1


if __name__ == "__main__":
    sys.exit(main())
