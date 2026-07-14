"""FastAPI app: serves the PWA and the state/answer API.

Endpoints:
  GET  /api/state                 -> list[PaneState] (list-shaped for M2)
  GET  /api/panes/{id}/snapshots  -> recent snapshot ids + timestamps
  GET  /api/panes/{id}/snapshots/{snap} -> raw captured text of one snapshot
  POST /api/panes/{id}/send       -> inject keys / answer a prompt
  GET  /                          -> PWA (static)
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import tmux
from .watcher import Watcher

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class SendBody(BaseModel):
    keys: str
    enter: bool = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    target = os.environ.get("TERMIPHONE_TARGET")
    use_llm = os.environ.get("TERMIPHONE_NO_LLM") != "1"
    app.state.watcher = Watcher(target=target, use_llm=use_llm)
    app.state.watcher.start()
    yield
    await app.state.watcher.stop()


app = FastAPI(title="termiphone", lifespan=lifespan)


@app.get("/api/state")
def get_state():
    return app.state.watcher.states


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
    tmux.send_keys(pane_id, body.keys, enter=body.enter)
    return {"ok": True}


# PWA static files last so /api/* wins. html=True serves index.html at /.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("TERMIPHONE_HOST", "0.0.0.0"),
        port=int(os.environ.get("TERMIPHONE_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
