"""Several machines in one fleet (docs/design/multi-machine.md).

Every machine runs its own daemon, unchanged: its own watcher, parser, accounts and
scratch, because all of those read local state (processes, transcripts, files). One of
them, the hub, lists the others in TMUXRC_PEERS and shows their panes beside its own. It
holds a long-poll on each peer's /api/state, and forwards every request that names a
peer's pane, session or machine to that peer.

A peer's ids reach the phone prefixed with the peer's name and a colon: pane "lab:%3" in
session "lab:main". tmux allows a colon in neither, so a prefixed id never collides with
a local one, and every surface that groups or labels by session shows the machine
without knowing machines exist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable

logger = logging.getLogger(__name__)

SEP = ":"
STATE_READ = 40.0  # a peer holds /api/state ~25s before answering unchanged
FORWARD_READ = 300.0  # a forwarded call may hold as long (live frames) or work longer (compose)
RETRY = 5.0  # after a failed poll: a peer that is down is retried, not hammered
ROUTED = ("pane_id", "pane", "session")  # fields, in a body or a reply, that name a peer's object
BODY_MAX = 1 << 20  # a JSON body larger than this is an upload, not an address: never parsed
# Not forwarded: the hop's own framing, and the browser's origin headers, which the hub has
# already checked and which name the hub, not the peer (browser_security.py).
DROP = {"host", "origin", "referer", "content-length", "connection", "keep-alive",
        "transfer-encoding", "upgrade", "accept-encoding", "content-encoding"}


@dataclass
class Peer:
    name: str
    url: str
    state: dict | None = None  # its last /api/state; None until it answers, or once it stops
    error: str | None = None


def parse(spec: str) -> list[Peer]:
    """TMUXRC_PEERS: `name=url` pairs, comma- or space-separated."""
    peers = []
    for item in spec.replace(",", " ").split():
        name, _, url = item.partition("=")
        if not name.replace("-", "").replace("_", "").isalnum() or not url.startswith(
                ("http://", "https://")):
            raise ValueError(f"TMUXRC_PEERS entry {item!r}: expected name=http://host:port")
        peers.append(Peer(name, url.rstrip("/")))
    if len({p.name for p in peers}) < len(peers):
        raise ValueError("TMUXRC_PEERS names a machine twice")
    return peers


def tag(value, name: str):
    return f"{name}{SEP}{value}" if isinstance(value, str) and value else value


class Fleet:
    def __init__(self, peers: list[Peer]):
        self.peers = {p.name: p for p in peers}
        self.seq = 0  # bumps on every change to any peer's state; part of the hub's version
        self._changed = asyncio.Event()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=FORWARD_READ))

    @classmethod
    def from_env(cls) -> Fleet | None:
        spec = os.environ.get("TMUXRC_PEERS", "").strip()
        return cls(parse(spec)) if spec else None

    async def run(self) -> None:
        await asyncio.gather(*(self._follow(p) for p in self.peers.values()))

    async def close(self) -> None:
        await self.client.aclose()

    def _bump(self) -> None:
        # A fresh Event per change: hold() captures the current one, so a change between its
        # caller's version check and its wait still wakes it.
        self.seq += 1
        self._changed.set()
        self._changed = asyncio.Event()

    async def _follow(self, peer: Peer) -> None:
        version = None
        while True:
            try:
                r = await self.client.get(f"{peer.url}/api/state",
                                          params={} if version is None else {"v": version},
                                          timeout=httpx.Timeout(10.0, read=STATE_READ))
                r.raise_for_status()
                state = r.json()
            except (httpx.HTTPError, ValueError) as e:
                if peer.error is None:
                    logger.warning("Machine %s unreachable at %s: %s", peer.name, peer.url, e)
                was = peer.state, peer.error
                peer.state, peer.error, version = None, str(e) or type(e).__name__, None
                if was != (None, peer.error):
                    self._bump()
                await asyncio.sleep(RETRY)
                continue
            if peer.state is None or state.get("version") != version:
                if peer.error:
                    logger.info("Machine %s is back", peer.name)
                peer.state, peer.error = state, None
                self._bump()
            version = state.get("version")

    async def hold(self, local: Awaitable) -> None:
        """Wait for the local watcher's change (`local`) or any peer's, whichever is first."""
        tasks = [asyncio.ensure_future(local), asyncio.ensure_future(self._changed.wait())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
                with suppress(asyncio.CancelledError):
                    await t

    def panes(self) -> list[dict]:
        out = []
        for peer in self.peers.values():
            for pane in (peer.state or {}).get("panes", []):
                out.append({**pane, "machine": peer.name,  # noqa: PERF401 (nested loop)
                            **{k: tag(pane.get(k), peer.name) for k in ("pane_id", "session")}})
        return out

    def machines(self, host: str) -> list[dict]:
        return [{"name": host, "online": True}] + [
            {"name": p.name, "online": p.state is not None, "error": p.error}
            for p in self.peers.values()]

    def route(self, value) -> tuple[Peer, str] | None:
        """The peer a prefixed id names, and the id as that peer knows it."""
        if not isinstance(value, str):
            return None
        name, sep, rest = value.partition(SEP)
        return (self.peers[name], rest) if sep and name in self.peers else None

    async def forward(self, request: Request, peer: Peer, path: str, query: list,
                      body: bytes | None) -> Response:
        headers = [(k, v) for k, v in request.headers.items()
                   if k not in DROP and not k.startswith("sec-fetch-")]
        req = self.client.build_request(request.method, peer.url + path, params=query,
                                        headers=headers,
                                        content=request.stream() if body is None else body)
        try:
            r = await self.client.send(req, stream=True)
        except httpx.HTTPError as e:
            logger.warning("Forward to %s failed: %s", peer.name, e)
            return PlainTextResponse(f"Machine {peer.name} is unreachable", status_code=502)
        keep = {k: v for k, v in r.headers.items() if k not in DROP}
        if not r.headers.get("content-type", "").startswith("application/json"):
            return StreamingResponse(r.aiter_bytes(), r.status_code, headers=keep,
                                     background=BackgroundTask(r.aclose))
        data = await r.aread()
        await r.aclose()
        with suppress(ValueError):  # a reply naming a pane or session names it as the phone does
            doc = json.loads(data)
            if isinstance(doc, dict) and any(isinstance(doc.get(k), str) for k in ROUTED):
                data = json.dumps({**doc, **{k: tag(doc[k], peer.name)
                                             for k in ROUTED if k in doc}}).encode()
        return Response(data, r.status_code, headers=keep)


class PeerProxy:
    """Send a request to the peer it names: by a pane id in its path, `?machine=<peer>`, or
    a prefixed pane or session in its JSON body. Anything else is the hub's own."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        fleet = scope["type"] == "http" and getattr(scope["app"].state, "fleet", None)
        if not fleet or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        query = [(k, v) for k, v in request.query_params.multi_items() if k != "machine"]
        path, body, hit = scope["path"], None, None
        pane, slash, rest = path.removeprefix("/api/panes/").partition("/")
        if path.startswith("/api/panes/") and (hit := fleet.route(pane)):
            path = f"/api/panes/{quote(hit[1], safe='')}{slash}{rest}"
        elif (machine := request.query_params.get("machine")) in fleet.peers:
            hit = (fleet.peers[machine], "")
        elif request.method in ("POST", "PUT") and request.headers.get(
                "content-type", "").startswith("application/json") and int(
                request.headers.get("content-length") or 0) <= BODY_MAX:
            body = await request.body()
            with suppress(ValueError):
                doc = json.loads(body)
                routes = {k: r for k in ROUTED
                          if isinstance(doc, dict) and (r := fleet.route(doc.get(k)))}
                if len({r[0].name for r in routes.values()}) > 1:
                    return await PlainTextResponse("One request, one machine",
                                                   status_code=400)(scope, receive, send)
                if routes:
                    hit = next(iter(routes.values()))
                    body = json.dumps({**doc, **{k: r[1] for k, r in routes.items()}}).encode()
        if hit:
            resp = await fleet.forward(request, hit[0], path, query, body)
            return await resp(scope, receive, send)
        if body is not None:  # read to look for an address: the hub's own route reads it again
            receive = _replay(body, receive)
        return await self.app(scope, receive, send)


def _replay(body: bytes, receive):
    sent = False

    async def again():
        nonlocal sent
        if sent:
            return await receive()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}
    return again
