"""Paid GPT-Live smoke test against a fake terminal; never touches a real pane.

Run from the repo: uv run python research/live-eval/smoke_gpt_live.py
Uses the external provider key file, TTS for a fixed test utterance, and ~20 seconds
of Live audio. Requires ffmpeg. Prints only synthetic conversation/test outcomes.
"""

import asyncio
import base64
import os
import subprocess
import sys
import wave
from pathlib import Path
from unittest.mock import patch

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from openbus import gpt_live, live


class Watcher:
    def __init__(self):
        self.snapshots = {"%1": [{"id": "smoke", "text": "$ "}]}

    def digest(self):
        return [
            {
                "pane_id": "%1",
                "window_index": "1",
                "label": "smoke",
                "tool": "shell",
                "activity": "idle",
                "tmux_active": True,
            }
        ]

    def state_version(self):
        return 1

    async def wait_for_state_change(self, *args, **kwargs):
        await asyncio.Event().wait()

    def request_reparse(self, pane):
        self.snapshots[pane][-1]["text"] = "$ echo hello\nhello\n$ "


class Browser:
    def __init__(self, pcm):
        self.pcm = pcm + bytes(16000 * 2 * 20)
        self.at = 0
        self.messages = []
        self.audio = bytearray()
        self.ready = asyncio.Event()
        self.acted_at = None

    async def receive_json(self):
        await self.ready.wait()
        if self.acted_at and asyncio.get_running_loop().time() - self.acted_at > 5:
            return {"action": "stop"}
        frame = self.pcm[self.at : self.at + 1280]
        if not frame:
            return {"action": "stop"}
        self.at += len(frame)
        await asyncio.sleep(0.04)
        return {"action": "audio", "data": base64.b64encode(frame).decode()}

    async def send_json(self, message):
        self.messages.append(message)
        if message["type"] == "status":
            print(message["status"], flush=True)
            if message["status"] == "listening":
                self.ready.set()
        elif message["type"] == "typed":
            print("Fake terminal action:", message["text"], flush=True)
            self.acted_at = asyncio.get_running_loop().time()
        elif message["type"] == "audio":
            self.audio.extend(base64.b64decode(message["data"]))
        elif message["type"] == "error":
            raise AssertionError(message["message"])


async def main():
    load_dotenv(Path.home() / ".config/tmux-rc/openai.env")
    key = os.environ["OPENAI_API_KEY"]
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": "Bearer " + key},
            json={
                "model": "gpt-4o-mini-tts",
                "voice": "alloy",
                "response_format": "wav",
                "input": "In window one, type echo hello and press enter.",
            },
        )
        response.raise_for_status()
    pcm = (
        await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                "pipe:1",
            ],
            input=response.content,
            capture_output=True,
            check=True,
        )
    ).stdout
    browser = Browser(bytes(16000) + pcm)
    meter = live._Meter("gpt-live-smoke", "smoke")
    actions = []
    with (
        patch.object(live.tmux, "send_keys", side_effect=lambda *a: actions.append(a)),
        patch.object(live.tmux, "server_uid", return_value="smoke"),
    ):
        async with asyncio.timeout(60):
            await gpt_live.run_session(browser, Watcher(), "smoke", meter)
        # Let post-action refresh tasks finish before the fake terminal is unpatched.
        if live._tasks:
            await asyncio.gather(*list(live._tasks), return_exceptions=True)
    for role in ("user", "model"):
        print(
            role + ":",
            "".join(
                m.get("text", "") for m in browser.messages if m.get("role") == role
            ),
        )
    assert actions == [("%1", "echo hello", True, True)], actions
    assert any(m.get("role") == "user" for m in browser.messages), "No input transcript"
    assert any(m.get("role") == "model" for m in browser.messages), (
        "No output transcript"
    )
    assert meter.usage.in_tokens > 0, "No backend usage received"
    assert meter.usage.final, "Missing final voice usage"
    assert browser.audio, "No audio returned"
    if len(sys.argv) == 2:
        with wave.open(sys.argv[1], "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(browser.audio)
    print(
        f"PASS: voice, delegation, one tool action, continuation, final usage; ${meter.usage.cost():.4f}"
    )


if __name__ == "__main__":
    asyncio.run(main())
