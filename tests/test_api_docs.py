"""Interactive CDN-backed docs require opt-in; the JSON schema stays available."""

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(("setting", "enabled"), [
    (None, False), ("0", False), ("false", False), ("", False),
    ("1", True), ("true", True), (" YES ", True), ("on", True),
])
def test_api_docs_opt_in(setting, enabled):
    # Server config is read at import; a child keeps each case independent of the
    # suite's shared app without starting the daemon or calling external services.
    env = {k: v for k, v in os.environ.items() if k != "TMUXRC_API_DOCS"}
    # An empty value prevents any checkout .env from silently opting the test in.
    env["TMUXRC_API_DOCS"] = setting or ""
    probe = """
import json
from fastapi.testclient import TestClient
from openbus.server import app
c = TestClient(app)
print(json.dumps([c.get(path).status_code for path in (
    '/apidocs', '/apiredoc', '/docs/oauth2-redirect', '/openapi.json')]))
"""
    run = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True,
                         text=True, check=True, timeout=30)
    docs = 200 if enabled else 404
    assert json.loads(run.stdout) == [docs, docs, docs, 200]
