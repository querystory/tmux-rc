"""Browser write admission, separate from the tunnel's user authentication."""

import os
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse


def _origin(value: str) -> tuple[str, str, int]:
    """Parse one serialized web origin without accepting URL paths or credentials."""
    url = urlsplit(value)
    if (not value or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value
            or url.scheme not in ("http", "https") or not url.hostname
            or "*" in url.hostname or url.netloc.endswith(":")
            or url.username is not None or url.password is not None
            or url.path or "?" in value or "#" in value):
        raise ValueError("expected an http(s) origin without a path or credentials")
    port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
    return url.scheme, url.hostname.lower(), port


class BrowserOriginMiddleware:
    """Refuse foreign browser writes before reading bodies or opening model sessions.

    Non-browser clients may omit Origin and Fetch Metadata. Those headers are a browser
    boundary, not credentials: the daemon remains an unauthenticated local API.
    """

    def __init__(self, app):
        self.app = app
        port = int(os.environ.get("TMUXRC_PORT", "18030"))
        self.origins = {("http", host, port) for host in ("localhost", "127.0.0.1", "::1")}
        for value in os.environ.get("TMUXRC_ALLOWED_ORIGINS", "").split(","):
            if value.strip():
                self.origins.add(_origin(value.strip()))

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket") or (
                kind == "http" and scope["method"] in ("GET", "HEAD", "OPTIONS")):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        origins = headers.getlist("origin")
        allowed = not origins and headers.get("sec-fetch-site") in (None, "same-origin", "none")
        if len(origins) == 1:
            try:
                allowed = _origin(origins[0]) in self.origins
            except ValueError:
                allowed = False
        if allowed:
            return await self.app(scope, receive, send)
        if kind == "websocket":
            return await send({"type": "websocket.close", "code": 1008,
                               "reason": "Browser origin is not allowed"})
        return await PlainTextResponse("Browser origin is not allowed", status_code=403)(
            scope, receive, send)
