"""/scratch/ serves TMUXRC_SCRATCH_DIR read-only, and nothing at all unless it is set.
The mount is decided at import, so each case runs in a fresh interpreter."""

import os
import subprocess
import sys

PROBE = """
import sys
from fastapi.testclient import TestClient
from types import SimpleNamespace
from openbus import server
server.app.state.watcher = SimpleNamespace(states=[], pane_birth=lambda _: None)
server.accounts.report = lambda *_: {"profiles": []}  # never this machine's accounts
server.scratch_sidecars.browser = lambda: None  # nor its browser
server.agent_history.reconcile = lambda: None  # nor its agent-history index
server.app.state.history = server.History(server.Path(sys.argv[1]))
c = TestClient(server.app, follow_redirects=False)
for path in ("/scratch/mock.html", "/scratch/%2e%2e/secret.txt", "/scratch/link.txt",
             "/scratch/sub/", "/scratch", "/scratch/notes.md", "/scratch/sub/.env",
             "/scratch/.git/config", "/scratch/.git/", "/scratch/wip/"):
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
# Visits: a browser opening the folder's page and nothing else (not the gallery, an asset, a
# 404, nor the plain GETs above, as an agent checks its page is served)
seen = lambda: [k for k, _ in server.app.state.history.visits("scratch")]
print(int(seen() == []))
for path in ("/scratch/", "/scratch/mock.html", "/scratch/sub/", "/scratch/nope/"):
    c.get(path, headers={"Sec-Fetch-Dest": "document"})
c.get("/scratch/viewed/", headers={"Sec-Fetch-Dest": "iframe"})  # the app's viewer
c.get("/scratch/wip/", headers={"Sec-Fetch-Dest": "iframe"})  # its placeholder
print(int(sorted(seen()) == ["sub", "viewed", "wip"]))
"""


def _probe(env, tmp_path):
    env = {**os.environ, "AGENT_HISTORY_DIR": "/nonexistent", **env}  # no real transcripts
    out = subprocess.run([sys.executable, "-c", PROBE, str(tmp_path / "state" / "h.db")],
                         stdout=subprocess.PIPE, text=True, check=True, env=env,
                         timeout=60).stdout
    return [int(code) for code in out.split()]


def test_scratch_serves_only_inside_the_configured_dir(tmp_path):
    scratch = tmp_path / "scratch"
    (scratch / "sub").mkdir(parents=True)
    (scratch / "mock.html").write_text("mock")
    (scratch / "sub" / "index.html").write_text("site")
    (scratch / "viewed").mkdir()
    (scratch / "viewed" / "index.html").write_text("site")
    (scratch / "notes.md").write_text("# Notes")
    (scratch / "sub" / ".env").write_text("TOKEN=x")
    (scratch / ".git").mkdir()
    (scratch / ".git" / "config").write_text("[core]")
    (scratch / ".git" / "index.html").write_text("hidden")
    (scratch / "wip").mkdir()
    (scratch / "wip" / "meta.json").write_text('{"title": "Being made"}')
    (tmp_path / "secret.txt").write_text("outside")
    (scratch / "link.txt").symlink_to(tmp_path / "secret.txt")
    # file, traversal and a symlink out both refused, directory index, bare prefix
    # redirected to the slash form, a Markdown page, the page (rendered or raw Markdown,
    # and a committed mock) sandboxed so its
    # scripts can't drive /api/* (nor frame it, or /m), and a nested directory's slash
    # redirect path-only (an absolute one would be http:// behind the tunnel), the
    # bare-prefix one keeping its query; and only the folder's page (or the placeholder of
    # one being made, answering 202) counted as a visit.
    # Hidden files and directories (an .env, a .git) are refused, as the gallery skips them.
    assert _probe({"TMUXRC_SCRATCH_DIR": str(scratch)}, tmp_path) == [
        200, 404, 404, 200, 307, 200, 404, 404, 404, 202, 1, 1, 1, 1, 1, 1, 1]


def test_scratch_is_off_unless_configured(tmp_path):
    # Empty rather than absent: the daemon loads .env without overriding, so an operator
    # who enabled scratch there would otherwise switch it back on under this test.
    assert _probe({"TMUXRC_SCRATCH_DIR": ""}, tmp_path)[:3] == [404, 404, 404]
