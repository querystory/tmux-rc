"""A ranged read of a big file is answered a piece at a time: a tunnel that carries each
response as one capped message still delivers a video, which its player reads in ranges."""

from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse
from starlette.staticfiles import StaticFiles

from openbus import server


def test_long_ranges_come_back_a_piece_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RANGE_PIECE", 10)
    (tmp_path / "reel.mp4").write_bytes(bytes(range(25)))
    c = TestClient(server._short_ranges(StaticFiles(directory=tmp_path)))
    for asked, got in (("bytes=0-", "bytes 0-9/25"), ("bytes=20-", "bytes 20-24/25"),
                       ("bytes=3-24", "bytes 3-12/25"), ("bytes=3-5", "bytes 3-5/25")):
        r = c.get("/reel.mp4", headers={"Range": asked})
        assert (r.status_code, r.headers["content-range"]) == (206, got)
    assert c.get("/reel.mp4").content == bytes(range(25))  # unranged: whole, as before


def test_an_answer_too_big_to_send_at_once_is_never_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RANGE_PIECE", 10)
    monkeypatch.setattr(server, "WHOLE_MAX", 20)
    (tmp_path / "my reel.mp4").write_bytes(bytes(25))
    (tmp_path / "data.bin").write_bytes(bytes(25))
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_bytes(bytes(25))
    c = TestClient(server._short_ranges(StaticFiles(directory=tmp_path, html=True)))
    for dest in ("document", "iframe"):  # a tab, or scratch's in-app viewer
        opened = c.get("/my%20reel.mp4", headers={"Sec-Fetch-Dest": dest})
        assert opened.headers["content-type"].startswith("text/html")
        assert 'src="my%20reel.mp4"' in opened.text  # its player reads it in ranges
    for path, headers in (("/my%20reel.mp4", {"Sec-Fetch-Dest": "empty"}),  # a download
                          ("/data.bin", {"Sec-Fetch-Dest": "document"}),
                          ("/site/", {}),
                          ("/data.bin", {"Range": "bytes=-21"}),
                          ("/data.bin", {"Range": "bytes=0-11,12-23"}),
                          ("/data.bin", {"Range": "bytes=0-1", "If-Range": '"stale"'})):
        r = c.get(path, headers=headers)
        assert (r.status_code, r.headers["accept-ranges"]) == (403, "bytes"), (path, headers)
    assert c.get("/data.bin", headers={"Range": "bytes=0-"}).status_code == 206
    assert c.head("/data.bin").headers["content-length"] == "25"


def test_media_is_not_gzipped():  # importing server excludes it from the app's GZip
    for kind, encoded in (("video/mp4", None), ("text/plain", "gzip")):
        c = TestClient(server.gzip.GZipMiddleware(PlainTextResponse("x" * 4096, media_type=kind)))
        assert c.get("/").headers.get("content-encoding") == encoded
