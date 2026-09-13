"""Which Live model IDs does Vertex actually serve for this project/region? Connect, send one
text turn, measure time to first audio byte and total turn, count audio bytes."""
import asyncio, os, sys, time
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types
ROOT = Path(__file__).resolve().parents[2]  # the repo root: .env holds the Vertex project + SA key path
load_dotenv(ROOT / ".env"); load_dotenv(Path.home() / ".config/tmux-rc/openai.env")  # API keys live outside the checkout
PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
CANDIDATES = sys.argv[1:] or [
    "gemini-live-2.5-flash-native-audio",
    "gemini-live-2.5-flash-preview-native-audio-09-2025",
    "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini-3.1-flash-live-preview",
    "gemini-3.1-flash-live",
    "gemini-live-3.1-flash",
]
async def probe(model, region):
    client = genai.Client(vertexai=True, project=PROJECT, location=region)
    cfg = types.LiveConnectConfig(response_modalities=["AUDIO"],
        output_audio_transcription=types.AudioTranscriptionConfig())
    t0 = time.monotonic()
    try:
        async with client.aio.live.connect(model=model, config=cfg) as s:
            t_conn = time.monotonic() - t0
            t1 = time.monotonic(); first = None; nbytes = 0; text = ""
            await s.send_client_content(turns=types.Content(role="user", parts=[types.Part(text="Say hello in five words.")]), turn_complete=True)
            async for msg in s.receive():
                sc = msg.server_content
                if not sc: continue
                if sc.model_turn:
                    for p in sc.model_turn.parts or []:
                        if p.inline_data and p.inline_data.data:
                            nbytes += len(p.inline_data.data)
                            if first is None: first = time.monotonic() - t1
                if sc.output_transcription and sc.output_transcription.text: text += sc.output_transcription.text
                if sc.turn_complete: break
            print(f"OK   {model:55s} {region:12s} connect={t_conn:.2f}s ttfa={first and round(first,2)}s turn={time.monotonic()-t1:.2f}s audio={nbytes//1024}KB said={text.strip()!r}")
    except Exception as e:
        print(f"FAIL {model:55s} {region:12s} {type(e).__name__}: {str(e)[:160]}")
async def main():
    for m in CANDIDATES:
        for region in ("us-central1",):
            try: await asyncio.wait_for(probe(m, region), timeout=25)
            except (asyncio.TimeoutError, TimeoutError): print(f"HANG {m:55s} {region:12s} no reply in 25s")
asyncio.run(main())
