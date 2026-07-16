"""FastAPI app: serves the PWA and the state/answer API.

Endpoints:
  GET  /api/state                 -> list of raw parser-JSON dicts (list-shaped for M2)
  GET  /api/panes/{id}/snapshots  -> recent snapshot ids + timestamps
  GET  /api/panes/{id}/snapshots/{snap} -> raw captured text of one snapshot
  POST /api/panes/{id}/send       -> inject keys / answer a prompt
  GET  /                          -> PWA (static)
"""

from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env BEFORE importing the watcher/llm/telemetry chain — those read config from
# os.environ at import time (model, GOOGLE_CLOUD_PROJECT, OTEL endpoint). Without this, a
# launch that didn't inherit the env (e.g. a stray `make dev`) silently loses Vertex creds
# and every parse fails. Real environment vars still win over .env (override=False).
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import tmux  # noqa: E402
from .watcher import Watcher  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# Uploaded images land here so the agent can read them by path. Kept out of the repo.
IMG_DIR = Path(tempfile.gettempdir()) / "tmux-rc-images"
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


class SendBody(BaseModel):
    keys: str
    enter: bool = True
    literal: bool = True  # False ⇒ keys is a tmux key-name (Escape, Up, C-c)


@asynccontextmanager
async def lifespan(app: FastAPI):
    target = os.environ.get("TMUXRC_TARGET")
    use_llm = os.environ.get("TMUXRC_NO_LLM") != "1"
    app.state.watcher = Watcher(target=target, use_llm=use_llm)
    app.state.watcher.start()
    yield
    await app.state.watcher.stop()


app = FastAPI(title="tmux-rc", lifespan=lifespan)


@app.middleware("http")
async def no_cache(request, call_next):
    """Never let the browser cache anything. StaticFiles sends an ETag but no
    Cache-Control, so phones serve stale JS/HTML heuristically and edits never appear.
    Force no-store on every response — fine for a live dev tool on the LAN."""
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    for h in ("etag", "last-modified"):
        if h in resp.headers:
            del resp.headers[h]
    return resp


@app.get("/api/version")
def get_version():
    """Hash of the web assets, so the client can reload itself when they change
    (see app.js). Cheap to recompute per call — the web dir is tiny."""
    import hashlib

    h = hashlib.md5()
    for p in sorted(WEB_DIR.glob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(str(p.stat().st_mtime_ns).encode())
    return {"version": h.hexdigest()}


@app.get("/api/state")
def get_state():
    from .llm import last_error, usage_totals

    w = app.state.watcher
    return {
        "stale": w.is_stale(),
        "llm_error": last_error["msg"],  # transient; UI shows it subtly, not a big banner
        "usage": usage_totals(),  # running tokens/cost/calls/errors for the top-bar readout
        "prefix": tmux.prefix_key(),  # auto-detected tmux prefix, so the phone button matches
        "panes": w.states,
    }


@app.get("/api/panes/{pane_id}/snapshots")
def list_snapshots(pane_id: str):
    hist = app.state.watcher.snapshots.get(pane_id, [])
    return [{"id": s["id"], "ts": s["ts"]} for s in hist]


@app.get("/api/panes/{pane_id}/snapshots/{snap_id}", response_class=PlainTextResponse)
def get_snapshot(pane_id: str, snap_id: str):
    text = app.state.watcher.snapshot_text(pane_id, snap_id)
    if text is None:
        raise HTTPException(404, "snapshot not found")
    return text


@app.post("/api/panes/{pane_id}/send")
def send(pane_id: str, body: SendBody):
    if tmux.find_pane(pane_id) is None:
        raise HTTPException(404, "pane not found")
    tmux.send_keys(pane_id, body.keys, enter=body.enter, literal=body.literal)
    return {"ok": True}


@app.post("/api/panes/{pane_id}/select")
def select(pane_id: str):
    """Focus this pane in tmux itself — tapping a card on the phone follows on host."""
    if tmux.find_pane(pane_id) is None:
        raise HTTPException(404, "pane not found")
    tmux.select_pane(pane_id)
    return {"ok": True}


@app.post("/api/panes/{pane_id}/image")
async def send_image(pane_id: str, file: UploadFile):
    """Attach an image to the pane the way the user does on the host: put it on the
    system clipboard, then send Ctrl-V so Claude Code embeds it inline. (Typing a file
    path doesn't work — Claude reads the clipboard on paste.) Also keeps a temp file as
    a fallback path in the response."""
    if tmux.find_pane(pane_id) is None:
        raise HTTPException(404, "pane not found")
    mime = file.content_type or "image/png"
    if mime not in _EXT:
        raise HTTPException(415, f"unsupported image type: {mime}")
    data = await file.read()

    # Save a temp copy (fallback / debugging) and put the bytes on the clipboard.
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=_EXT[mime], dir=IMG_DIR)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)

    tools = tmux.set_clipboard_image(data, mime)
    if not tools:
        raise HTTPException(
            500,
            "No clipboard tool succeeded (need wl-copy or xclip on the host). "
            f"Image saved at {path}.",
        )
    tmux.send_keys(pane_id, "C-v", enter=False, literal=False)  # paste into the agent
    return {"ok": True, "clipboard": tools, "path": path, "bytes": len(data)}


# PWA static files last so /api/* wins. html=True serves index.html at /.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn

    # Reload watches the package source and restarts the process on edits (resetting
    # the watcher's in-memory cache — safe, tmux is the source of truth and state
    # rebuilds within a couple ticks). ON by default; set TMUXRC_RELOAD=0 to disable.
    reload = os.environ.get("TMUXRC_RELOAD", "1") != "0"
    uvicorn.run(
        "daemon.server:app" if reload else app,
        host=os.environ.get("TMUXRC_HOST", "0.0.0.0"),
        port=int(os.environ.get("TMUXRC_PORT", "8080")),
        reload=reload,
        reload_dirs=["daemon"] if reload else None,
    )


if __name__ == "__main__":
    main()
