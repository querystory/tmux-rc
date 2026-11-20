"""Live Mode session recordings: what the phone sent, what the model said back, and when.

A debugging aid for voice quality. Each voice session gets a folder under the state dir
holding mic.wav (the PCM the phone sent, exactly as received), model.wav (the provider's
voice), frames.jsonl (one line per mic frame: arrival, size, forwarded or why dropped,
and audio the phone itself dropped) and events.jsonl (transcripts, errors, start/end).
Local only and private to the user: it holds their voice, so it never goes near the repo,
scratch or telemetry. TMUXRC_LIVE_RECORD=0 turns it off.

Writes are plain buffered file appends (page cache, microseconds), so the event loop never
waits on the disk. A recorder that fails logs once and goes quiet: a full disk must never
end a conversation.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

MIC_RATE = 16000  # what web/m/live.js resamples the mic to
KEEP = 30  # newest sessions kept...
MAX_BYTES = 2 << 30  # ...within this many bytes, pruned when a session starts


def root() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state / "tmux-rc/live-audio"


class Recorder:
    """One session's recording. Without a folder (off, or failed) every call is a no-op."""

    def __init__(self, folder: Path | None = None) -> None:
        self.folder = folder
        self.t0 = time.monotonic()
        self._files: dict = {}

    def _open(self, name: str, rate: int = 0):
        if name not in self._files:
            if rate:
                w = wave.open(str(self.folder / name), "wb")  # noqa: SIM115 - closed in close()
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
            else:
                w = (self.folder / name).open("a", buffering=1 << 16)
            self._files[name] = w
        return self._files[name]

    def _guard(self, fn) -> None:
        if self.folder is None:
            return
        try:
            fn()
        except Exception:  # best-effort: whatever failed, the session goes on
            logger.warning("[live] recording to %s failed; stopped", self.folder, exc_info=True)
            self.close()

    def _line(self, name: str, **fields) -> None:
        ms = round((time.monotonic() - self.t0) * 1000)
        self._open(name).write(json.dumps({"t_ms": ms, **fields}) + "\n")

    def event(self, kind: str, **fields) -> None:
        self._guard(lambda: self._line("events.jsonl", kind=kind, **fields))

    def mic(self, pcm: bytes, dropped: str | None = None, client_lost_ms=0) -> None:
        """One browser frame as received. Even dropped ones are in mic.wav (it is what the
        phone sent), trimmed to whole samples so one odd frame can't shift the rest."""
        def write():
            self._open("mic.wav", MIC_RATE).writeframesraw(pcm[:len(pcm) & ~1])
            self._line("frames.jsonl", bytes=len(pcm), forwarded=not dropped,
                       **({"dropped": dropped} if dropped else {}),
                       **({"client_lost_ms": client_lost_ms} if client_lost_ms else {}))
        self._guard(write)

    def model(self, pcm: bytes, rate: int) -> None:
        self._guard(lambda: self._open("model.wav", rate).writeframesraw(pcm[:len(pcm) & ~1]))

    def close(self) -> None:
        """Flush and finish every file (wave patches its header here). Idempotent."""
        files, self._files, self.folder = self._files, {}, None
        for f in files.values():
            try:
                f.close()
            except Exception:  # close the rest regardless
                logger.warning("[live] closing a recording failed", exc_info=True)


def _prune(base: Path) -> None:
    """Keep the newest KEEP - 1 folders within MAX_BYTES, making room for the next one.
    Folder names start with a timestamp, so name order is age order."""
    total = 0
    for i, d in enumerate(sorted((d for d in base.iterdir() if d.is_dir()), reverse=True)):
        total += sum(f.stat().st_size for f in d.iterdir())
        if i >= KEEP - 1 or total > MAX_BYTES:
            shutil.rmtree(d, ignore_errors=True)


def start(session: str, model) -> Recorder:
    """A recorder for a new voice session (blocking: call it off the event loop)."""
    if os.environ.get("TMUXRC_LIVE_RECORD", "1") == "0":
        return Recorder()
    try:
        base = root()
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        _prune(base)
        stem, n = f"{time.strftime('%Y%m%d-%H%M%S')}-{session[:8]}", 1
        while (folder := base / f"{stem}-{n}").exists():
            n += 1
        folder.mkdir(mode=0o700)
    except Exception:  # no recording is no reason to refuse the session
        logger.warning("[live] cannot record this session", exc_info=True)
        return Recorder()
    rec = Recorder(folder)
    rec.event("start", session=session, label=model.label, model=model.model,
              provider=model.backend, mic_rate=MIC_RATE)
    return rec
