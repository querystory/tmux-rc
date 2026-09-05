"""Same tool-calling cases as harness_gemini.py, against the OpenAI Realtime API (raw WebSocket).
Usage: harness_openai.py [model] [openai|azure]   (default gpt-realtime-2.1 on openai)
Keys come from ~/.config/tmux-rc/openai.env: OPENAI_API_KEY, or for azure AZURE_OPENAI_API_KEY +
AZURE_OPENAI_ENDPOINT (either *.openai.azure.com or *.cognitiveservices.azure.com; model = deployment name)."""
import asyncio, json, os, sys, time
from pathlib import Path
import websockets
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness_gemini import SNAPSHOT, CASES  # noqa: E402  (module-level Gemini client is not built on import)
from daemon.live_providers import LiveModel, openai_endpoint  # noqa: E402  (the daemon's own URL/header logic)
MODEL = sys.argv[1] if len(sys.argv) > 1 else "gpt-realtime-2.1"
BACKEND = "azure-openai" if (sys.argv[2:3] or ["openai"])[0] == "azure" else "openai"
URL, HDR = openai_endpoint(LiveModel(MODEL, MODEL, BACKEND))
TOOL = {"type": "function", "name": "type_in_pane", "description": "Type keys into a tmux pane, optionally pressing Enter.",
        "parameters": {"type": "object", "properties": {"pane_id": {"type": "string"}, "keys": {"type": "string"},
                       "enter": {"type": "boolean"}}, "required": ["pane_id", "keys"]}}
async def run_case(utt, pane, sub, expect_tool):
    async with websockets.connect(URL, additional_headers=HDR, max_size=None) as ws:
        await ws.send(json.dumps({"type": "session.update", "session": {"type": "realtime", "instructions": SNAPSHOT,
            "tools": [TOOL], "output_modalities": ["audio"],
            "audio": {"output": {"voice": "marin"}}}}))
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": utt}]}}))
        t0 = time.monotonic(); await ws.send(json.dumps({"type": "response.create"}))
        call = None; said = ""; t_call = None
        while time.monotonic() - t0 < 20:
            ev = json.loads(await asyncio.wait_for(ws.recv(), 20)); t = ev.get("type", "")
            if t == "response.function_call_arguments.done":
                call = json.loads(ev["arguments"]); t_call = time.monotonic() - t0
            elif t == "response.output_audio_transcript.done": said += ev.get("transcript", "")
            elif t == "error": print("ERR", ev.get("error")); break
            elif t == "response.done": break
    ok = (call is not None and call.get("pane_id") == pane and sub.lower() in str(call.get("keys", "")).lower()) if expect_tool else call is None
    print(f"{'PASS' if ok else 'FAIL'} {t_call and f'{t_call:.2f}s' or '  -  ':>6} {utt[:52]:52s} -> {json.dumps(call)}  said={said.strip()[:60]!r}")
    return ok
async def main():
    res = [await run_case(*c) for c in CASES]
    print(f"{MODEL}: {sum(res)}/{len(res)} passed")
asyncio.run(main())
