"""Tool-calling harness for Live models: a fake tmux snapshot in the system prompt, scripted
user commands sent as text turns, check the model calls type_in_pane with the right pane/keys.
Reports per-case pass/fail and latency to the tool call. Usage: harness_gemini.py <model> [vertex|apikey]"""
import asyncio, os, sys, time, json
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types
ROOT = Path(__file__).resolve().parents[2]  # the repo root: .env holds the Vertex project + SA key path
load_dotenv(ROOT / ".env"); load_dotenv(Path.home() / ".config/tmux-rc/openai.env")  # API keys live outside the checkout
MODEL = sys.argv[1] if len(sys.argv) > 1 else ""; MODE = sys.argv[2] if len(sys.argv) > 2 else "vertex"
SNAPSHOT = """You control a tmux server through ONE tool, type_in_pane(pane_id, keys, enter). Panes right now:
- %9 "pr-cleanups" (claude code): WAITING on a yes/no prompt: "Do you want to run `git push --force`? (y/n)"
- %10 "output-iteration-ui" (claude code): working; headline "Refactoring the card renderer"
- %4 "app-performance-issues" (bash shell): idle at prompt in ~/src/qs/qs-app
- %24 "pane management" (claude code): idle, waiting for input
Answer briefly out loud, and act with the tool when asked to type, answer, run or tell a pane something. Never type into a pane the user did not clearly indicate."""
CASES = [  # (utterance, expected pane, substring expected in keys, expect_tool)
    ("Tell the pr cleanups pane no.", "%9", "n", True),
    ("Say yes to claude in pane nine.", "%9", "y", True),
    ("In the app performance pane run make test.", "%4", "make test", True),
    ("Ask the output iteration UI pane to also add unit tests for the renderer.", "%10", "test", True),
    ("What is pane ten working on?", None, None, False),
    ("Type ls in the shell pane.", "%4", "ls", True),
]
TOOL = types.Tool(function_declarations=[types.FunctionDeclaration(name="type_in_pane",
    description="Type keys into a tmux pane, optionally pressing Enter.",
    parameters=types.Schema(type="OBJECT", properties={
        "pane_id": types.Schema(type="STRING"), "keys": types.Schema(type="STRING"),
        "enter": types.Schema(type="BOOLEAN")}, required=["pane_id", "keys"]))])
async def run_case(client, utt, pane, sub, expect_tool):
    cfg = types.LiveConnectConfig(response_modalities=["AUDIO"], system_instruction=SNAPSHOT, tools=[TOOL],
                                  output_audio_transcription=types.AudioTranscriptionConfig())
    async with client.aio.live.connect(model=MODEL, config=cfg) as s:
        t0 = time.monotonic(); call = None; said = ""; t_call = None
        await s.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=utt)]), turn_complete=True)
        async for msg in s.receive():
            if msg.tool_call:
                call = msg.tool_call.function_calls[0]; t_call = time.monotonic() - t0
                await s.send_tool_response(function_responses=[types.FunctionResponse(name=call.name, id=call.id, response={"ok": True})])
            sc = msg.server_content
            if sc and sc.output_transcription and sc.output_transcription.text: said += sc.output_transcription.text
            if sc and sc.turn_complete and (call or not expect_tool): break
            if time.monotonic() - t0 > 20: break
    if expect_tool:
        ok = call is not None and call.args.get("pane_id") == pane and sub.lower() in str(call.args.get("keys", "")).lower()
    else:
        ok = call is None
    args = dict(call.args) if call else None
    print(f"{'PASS' if ok else 'FAIL'} {t_call and f'{t_call:.2f}s' or '  -  ':>6} {utt[:52]:52s} -> {json.dumps(args)}  said={said.strip()[:60]!r}")
    return ok
async def main():
    client = genai.Client(vertexai=True, project=os.environ["GOOGLE_CLOUD_PROJECT"], location="us-central1") if MODE == "vertex" \
        else genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    res = [await run_case(client, *c) for c in CASES]
    print(f"{MODEL}: {sum(res)}/{len(res)} passed")
if __name__ == "__main__":
    asyncio.run(main())
