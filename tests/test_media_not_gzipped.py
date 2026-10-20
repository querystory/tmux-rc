"""Video and audio skip the app's GZip: they come compressed already, and gzip over a 206
garbles its Content-Range, which breaks a player's ranged reads."""

from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from openbus import server


def test_media_is_not_gzipped():  # importing server excludes it from the app's GZip
    for kind, encoded in (("video/mp4", None), ("text/plain", "gzip")):
        c = TestClient(server.gzip.GZipMiddleware(PlainTextResponse("x" * 4096, media_type=kind)))
        assert c.get("/").headers.get("content-encoding") == encoded
