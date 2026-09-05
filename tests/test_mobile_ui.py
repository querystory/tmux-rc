"""Mobile URLs work behind a TLS-terminating tunnel, with either slash form."""

import pytest
from fastapi.testclient import TestClient

from daemon.server import app


@pytest.mark.parametrize("path", ["/m", "/m/"])
def test_mobile_entrypoint(path):
    client = TestClient(app)
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 200
    assert 'src="/m/app.js"' in response.text
    assert 'href="/m/style.css"' in response.text
    assert "no-store" in response.headers["cache-control"]


def test_mobile_assets_and_manifest():
    client = TestClient(app)
    for path in ("/m/app.js", "/m/style.css", "/terminal.js", "/icon.svg"):
        assert client.get(path).status_code == 200
    manifest = client.get("/m/manifest.json").json()
    assert manifest["start_url"] == "/m"
    assert manifest["scope"] == "/m"
    assert 'src="/app.js"' in client.get("/").text
