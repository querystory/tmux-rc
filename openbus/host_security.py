"""Keep browser-controlled DNS names from addressing the loopback API."""

import os
import re
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse


def _hostname(authority: str) -> str:
    """An HTTP authority's hostname, accepting bracketed IPv6 and validating its port."""
    if not authority or authority.endswith(":") or any(c in authority for c in "/\\?#@") or any(
            c.isspace() or ord(c) < 32 for c in authority):
        raise ValueError("expected a hostname, not a URL or credentials")
    url = urlsplit("//" + authority)
    host = url.hostname
    if not host or not re.fullmatch(r"[a-z0-9_.:-]+", host) or (url.port is not None and (
            not 0 < url.port <= 65535)):
        raise ValueError("invalid hostname or port")
    return host


class TrustedHostMiddleware:
    """An exact Host allowlist for HTTP and WebSockets, without wildcard suffixes."""

    def __init__(self, app):
        self.app = app
        self.hosts = {"localhost", "127.0.0.1", "::1"}
        bind = os.environ.get("TMUXRC_HOST", "127.0.0.1")
        if bind not in ("0.0.0.0", "::", "[::]"):
            self.hosts.add(_hostname(f"[{bind}]" if ":" in bind and not bind.startswith("[")
                                     else bind))
        for value in os.environ.get("TMUXRC_ALLOWED_HOSTS", "").split(","):
            if value.strip():
                self.hosts.add(_hostname(value.strip()))

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        hosts = Headers(scope=scope).getlist("host")
        allowed = False
        if len(hosts) == 1:
            try:
                allowed = _hostname(hosts[0]) in self.hosts
            except ValueError:
                pass
        if allowed:
            return await self.app(scope, receive, send)
        if kind == "websocket":
            return await send({"type": "websocket.close", "code": 1008,
                               "reason": "Host is not allowed"})
        return await PlainTextResponse("Host is not allowed", status_code=400)(scope, receive, send)
