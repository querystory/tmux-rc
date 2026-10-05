"""/scratch/ serves TMUXRC_SCRATCH_DIR read-only, and nothing at all unless it is set.
The mount is decided at import, so each case runs in a fresh interpreter."""

import os
import subprocess
import sys

PROBE = """
from fastapi.testclient import TestClient
from openbus import server
c = TestClient(server.app, follow_redirects=False)
for path in ("/scratch/mock.html", "/scratch/%2e%2e/secret.txt", "/scratch/sub/", "/scratch"):
    print(c.get(path).status_code)
csp = c.get("/scratch/mock.html").headers.get("content-security-policy", "")
print(int(csp.startswith("sandbox") and "allow-same-origin" not in csp
          and "form-action 'none'" in csp and "connect-src 'none'" in csp))
print(int(c.get("/scratch/sub").headers.get("location") == "/scratch/sub/"))
"""


def _probe(env):
    env = {k: v for k, v in {**os.environ, **env}.items() if v is not None}
    out = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True,
                         check=True, env=env).stdout
    return [int(code) for code in out.split()]


def test_scratch_serves_only_inside_the_configured_dir(tmp_path):
    scratch = tmp_path / "scratch"
    (scratch / "sub").mkdir(parents=True)
    (scratch / "mock.html").write_text("mock")
    (scratch / "sub" / "index.html").write_text("site")
    (tmp_path / "secret.txt").write_text("outside")
    # file, traversal refused, directory index, bare prefix redirected to the slash form,
    # the page sandboxed so its scripts can't drive /api/*, and a nested directory's
    # slash redirect path-only (an absolute one would be http:// behind the tunnel)
    assert _probe({"TMUXRC_SCRATCH_DIR": str(scratch)}) == [200, 404, 200, 307, 1, 1]


def test_scratch_is_off_unless_configured():
    assert _probe({"TMUXRC_SCRATCH_DIR": None})[:3] == [404, 404, 404]
