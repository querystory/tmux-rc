"""A ranged read of a big file is answered a piece at a time: a tunnel that carries each
response as one capped message still delivers a video, which its player reads in ranges."""

from fastapi.testclient import TestClient
from starlette.staticfiles import StaticFiles

from openbus import server


def test_long_ranges_come_back_a_piece_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RANGE_PIECE", 10)
    (tmp_path / "reel.mp4").write_bytes(bytes(range(25)))
    c = TestClient(server._short_ranges(StaticFiles(directory=tmp_path)))
    for asked, got in (("bytes=0-", "bytes 0-9/25"), ("bytes=20-", "bytes 20-24/25"),
                       ("bytes=3-24", "bytes 3-12/25"), ("bytes=3-5", "bytes 3-5/25"),
                       ("bytes=-20", "bytes 15-24/25"), ("bytes=0-1, 4-", "bytes 0-1/25"),
                       ("bytes=30-40,4-", "bytes 4-13/25")):
        r = c.get("/reel.mp4", headers={"Range": asked})
        assert (r.status_code, r.headers["content-range"]) == (206, got)
    for bad in ("bytes=0-" + "9" * 5000, "bytes=" + " " * 50000 + "x"):
        assert c.get("/reel.mp4", headers={"Range": bad}).status_code == 400
    assert c.get("/reel.mp4").content == bytes(range(25))  # unranged: whole, as before
