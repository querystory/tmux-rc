"""Mobile URLs work behind a TLS-terminating tunnel, with either slash form."""

import os

import pytest
from fastapi.testclient import TestClient

from daemon import server

app = server.app


@pytest.mark.parametrize("path", ["/m", "/m/"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_mobile_entrypoint(path, method):
    client = TestClient(app)
    response = client.request(method, path, follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "attachment" not in response.headers.get("content-disposition", "")
    if method == "GET":
        assert 'src="/m/app.js"' in response.text
        assert 'href="/m/style.css"' in response.text
    assert "no-store" in response.headers["cache-control"]


def test_mobile_assets_and_manifest():
    client = TestClient(app)
    for path in ("/m/app.js", "/m/style.css", "/m/live.js", "/m/composer.js", "/m/mic-tap.js", "/terminal.js", "/icon.svg"):
        assert client.get(path).status_code == 200
    manifest = client.get("/m/manifest.json").json()
    assert manifest["start_url"] == "/m"
    assert manifest["scope"] == "/m"
    assert 'src="/app.js"' in client.get("/").text


def test_version_tracks_nested_mobile_assets(tmp_path, monkeypatch):
    (tmp_path / "app.js").write_text("desktop")
    mobile = tmp_path / "m"
    mobile.mkdir()
    asset = mobile / "app.js"
    asset.write_text("mobile")
    monkeypatch.setattr(server, "WEB_DIR", tmp_path)
    before = server.get_version()["version"]
    stat = asset.stat()
    os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    edited = server.get_version()["version"]
    assert edited != before
    mobile.rename(tmp_path / "renamed")
    assert server.get_version()["version"] != edited
