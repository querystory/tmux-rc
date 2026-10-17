"""/scratch/ serves TMUXRC_SCRATCH_DIR read-only, and nothing at all unless it is set.
The mount is decided at import, so each case runs in a fresh interpreter."""

import os
import subprocess
import sys

PROBE = """
from fastapi.testclient import TestClient
from types import SimpleNamespace
from openbus import server
server.app.state.watcher = SimpleNamespace(states=[], pane_birth=lambda _: None)
server.accounts.report = lambda *_: {"profiles": []}  # never this machine's accounts
server.scratch_sidecars.browser = lambda: None  # nor its browser
server.agent_history.reconcile = lambda: None  # nor its agent-history index
c = TestClient(server.app, follow_redirects=False)
for path in ("/scratch/mock.html", "/scratch/%2e%2e/secret.txt", "/scratch/link.txt",
             "/scratch/sub/", "/scratch", "/scratch/notes.md"):
    print(c.get(path).status_code)
csp = c.get("/scratch/mock.html").headers.get("content-security-policy", "")
print(int(csp.startswith("sandbox") and "allow-same-origin" not in csp
          and "form-action 'none'" in csp and "connect-src 'none'" in csp))
print(int(all(c.get(p).headers.get("content-security-policy") == csp
              for p in ("/docs/mocks/x.html", "/docs//mocks/x.html", "/scratch/",
                        "/scratch/notes.md", "/scratch/notes.md?raw"))))
print(int(all(c.get(p).headers.get("content-security-policy") == "frame-ancestors 'self'"
              for p in ("/api/version", "/m/"))))
print(int(c.get("/scratch?opt=E7").headers.get("location") == "/scratch/?opt=E7"))
print(int(c.get("/scratch/sub").headers.get("location") == "/scratch/sub/"))
"""


def _probe(env):
    env = {**os.environ, "AGENT_HISTORY_DIR": "/nonexistent", **env}  # no real transcripts
    out = subprocess.run([sys.executable, "-c", PROBE], stdout=subprocess.PIPE, text=True,
                         check=True, env=env, timeout=60).stdout
    return [int(code) for code in out.split()]


def test_scratch_serves_only_inside_the_configured_dir(tmp_path):
    scratch = tmp_path / "scratch"
    (scratch / "sub").mkdir(parents=True)
    (scratch / "mock.html").write_text("mock")
    (scratch / "sub" / "index.html").write_text("site")
    (scratch / "notes.md").write_text("# Notes")
    (tmp_path / "secret.txt").write_text("outside")
    (scratch / "link.txt").symlink_to(tmp_path / "secret.txt")
    # file, traversal and a symlink out both refused, directory index, bare prefix
    # redirected to the slash form, a Markdown page, the page (rendered or raw Markdown,
    # and a committed mock) sandboxed so its
    # scripts can't drive /api/* (nor frame it, or /m), and a nested directory's slash
    # redirect path-only (an absolute one would be http:// behind the tunnel), the
    # bare-prefix one keeping its query
    assert _probe({"TMUXRC_SCRATCH_DIR": str(scratch)}) == [200, 404, 404, 200, 307, 200,
                                                            1, 1, 1, 1, 1]


def test_scratch_is_off_unless_configured():
    # Empty rather than absent: the daemon loads .env without overriding, so an operator
    # who enabled scratch there would otherwise switch it back on under this test.
    assert _probe({"TMUXRC_SCRATCH_DIR": ""})[:3] == [404, 404, 404]
