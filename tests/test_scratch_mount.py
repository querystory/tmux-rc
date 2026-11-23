"""/scratch/ serves TMUXRC_SCRATCH_DIR read-only, and nothing at all unless it is set.
The mount is decided at import, so each case runs in a fresh interpreter."""

import json
import os
import subprocess
import sys

PROBE = """
import io
import os
import sys
from fastapi.testclient import TestClient
from types import SimpleNamespace
from openbus import server
server.app.state.watcher = SimpleNamespace(states=[], pane_birth=lambda _: None)
scanned = []  # never this machine's accounts, and scanned only for a pending folder's page
server.accounts.report = lambda *_: scanned.append(1) or {"profiles": []}
server.scratch_sidecars.browser = lambda: None  # nor its browser
server.agent_history.reconcile = lambda: None  # nor its agent-history index
server.app.state.history = server.History(server.Path(sys.argv[1]))
c = TestClient(server.app, follow_redirects=False)
for path in ("/scratch/mock.html", "/scratch/%2e%2e/secret.txt", "/scratch/link.txt",
             "/scratch/sub/", "/scratch", "/scratch/notes.md", "/scratch/sub/.env",
             "/scratch/.git/config", "/scratch/.git/", "/scratch/wip/"):
    print(c.get(path).status_code)
wip = c.get("/scratch/wip/")  # a 202 passes `curl -f`: it says so, and its title is not the page's
print(int(wip.headers.get("x-scratch-pending") == "1" and "Retry-After" in wip.headers
          and "<title>In progress · A new page</title>" in wip.text))
# Scratch's own tab icon, on every page the daemon renders there (the placeholder's spins)
print(int(all('<link rel="icon" href="/scratch.svg">' in c.get(p).text
              for p in ("/scratch/", "/scratch/notes.md", "/scratch/wip/"))
          and c.get("/scratch.svg").status_code == 200 and "pathLength" in wip.text))
scanned.clear()
c.get("/scratch/nope/")
print(int(scanned == []))
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
for path in ("/scratch/", "/scratch/mock.html", "/scratch/nope/"):
    c.get(path, headers={"Sec-Fetch-Dest": "document"})
c.get("/scratch/sub/", headers={"Sec-Fetch-Dest": "document"})  # sent to the viewer, which:
for path in ("/scratch/sub/", "/scratch/viewed/"):
    c.get(path, headers={"Sec-Fetch-Dest": "iframe"})
c.get("/scratch/wip/", headers={"Sec-Fetch-Dest": "iframe"})  # its placeholder
print(int(sorted(seen()) == ["sub", "viewed", "wip"]))
# A tab opening a page of a folder gets it in the app's viewer, query and all; the viewer's
# Open in browser (?bare), a Markdown page's source (?raw), a save (?download), the gallery,
# a loose file, a frame and an agent's plain GET get the page itself.
doc = {"Sec-Fetch-Dest": "document"}
print(int(c.get("/scratch/sub/x%20v2.html?a=1", headers=doc).headers.get("location")
          == "/m?scratch=sub%2Fx%2520v2.html%3Fa%3D1"
          and all(c.get(p, headers=doc).status_code == 200 for p in (
              "/scratch/sub/?bare=", "/scratch/sub/?download=1", "/scratch/",
              "/scratch/mock.html", "/scratch/notes.md?raw"))
          and c.get("/scratch/sub/").status_code == 200))
if not server._scratch_dir:
    sys.exit()
# Archive: gone from the gallery, still served at its old link and at the archive's; a
# name that is a path is refused whole; Delete empties the archive, then the link 404s.
act = lambda action, *names: c.post("/api/scratch", json={"action": action, "names": names})
root = server.Path(server._scratch_dir)
tag = lambda: server.scratch.tag(server.scratch.listing(root))
before = tag()
print(int(act("archive", "viewed").json() == {"done": ["viewed"]}))
print(int('data-name="viewed"' not in c.get("/scratch/").text
          and 'data-name="viewed"' in c.get("/scratch/?archived").text))
print(c.get("/scratch/viewed/").status_code, c.get("/scratch/.archive/viewed/").status_code)
# its name taken again (a page in progress): each link is wholly one folder's
(root / "viewed").mkdir()
(root / "viewed" / "meta.json").write_text('{"title": "Again"}')
print(c.get("/scratch/viewed/").status_code, c.get("/scratch/.archive/viewed/").status_code)
(root / "viewed" / "meta.json").unlink()
(root / "viewed").rmdir()
# a page still in progress, archived: its placeholder answers at both links
act("archive", "wip")
print(c.get("/scratch/wip/").status_code, c.get("/scratch/.archive/wip/").status_code)
act("unarchive", "wip")
print(act("archive", "sub", "../sub").status_code, int(act("delete", "sub").json()["done"] == []))
# The open gallery's check: an image only when its tag is out of date (no fetch, sandboxed)
old, now = c.get(f"/scratch/?seen={before}"), c.get(f"/scratch/?seen={tag()}")
print(int(old.headers["content-type"] == "image/gif" and now.status_code == 204))
print(int(act("delete", "viewed").json() == {"done": ["viewed"]}),
      c.get("/scratch/viewed/").status_code)
# The placeholder asks by image whether to reload: a 1px-wide answer while it is unchanged,
# 2px once it changed or its page landed (never a navigation on a failed ask: client side).
if server._scratch_dir:
    from PIL import Image
    width = lambda q: Image.open(io.BytesIO(c.get(f"/scratch/wip/?seen={q}").content)).width
    mine = server.re.search(r'const SEEN = "([^"]+)"', c.get("/scratch/wip/").text)[1]
    before = width("stale"), width(mine)
    (server.Path(server._scratch_dir) / "wip" / "index.html").write_text("landed")
    print(*before, width(mine))
    # An updating folder's Markdown page wears the bar and asks the same way: unchanged, then
    # its files change (a revision lands), then the mark goes (the update is done).
    rev = server.Path(server._scratch_dir) / "rev"
    scanned.clear()
    page = c.get("/scratch/rev/").text  # asked every few seconds: no accounts scanned
    print(int("<title>Updating · Rev</title>" in page and "Updating: the intro · reloads" in page
              and scanned == []))
    width = lambda q: Image.open(io.BytesIO(c.get(f"/scratch/rev?seen={q}").content)).width
    mine = server.re.search(r'const SEEN = "([^"]+)"', page)[1]
    before = width(mine)
    os.utime(rev / "index.md", (2e9, 2e9))
    landed, mine = width(mine), server.re.search(r'const SEEN = "([^"]+)"',
                                                 c.get("/scratch/rev/").text)[1]
    # archived, it wears the bar at both its links and asks at its archived folder's
    act("archive", "rev")
    print(*(int("Updating: the intro" in c.get(f"/scratch/{at}rev/").text)
            for at in ("", ".archive/")),
          Image.open(io.BytesIO(c.get(f"/scratch/.archive/rev/?seen={mine}").content)).width)
    act("unarchive", "rev")
    (rev / "meta.json").write_text('{"title": "Re')  # mid-write: no answer, so no reload
    print(width(mine))
    (rev / "meta.json").write_text('{"title": "Rev"}')
    print(before, landed, width(mine), int("Updating" not in c.get("/scratch/rev/").text))
    # The viewer's check: its folder's version, new once a file changes, not its thumbnail;
    # archived, at the archive's and its old link; one in progress or not a folder, no answer
    v = lambda q="": c.get(f"/api/scratch/rev/version{q}").json()["version"]
    was = v()
    (rev / "thumb.webp").write_bytes(b"")
    same = v() == was
    os.utime(rev / "index.md", (2.1e9, 2.1e9))
    act("archive", "rev")
    (rev.parent / "next").mkdir()
    (rev.parent / "next" / "meta.json").write_text('{"title": "Next"}')
    print(int(same and v("?archived=1") not in (was, None) and v() == v("?archived=1")
              and c.get("/api/scratch/next/version").status_code
              == c.get("/api/scratch/..%2F/version").status_code == 404))
"""


RESUME = """
import sys
from fastapi.testclient import TestClient
from types import SimpleNamespace
from openbus import server
watcher = server.app.state.watcher = SimpleNamespace(states=[], pane_birth=lambda _: "77")
server.agent_history.reconcile = lambda: None
server.app.state.history = server.History(server.Path(sys.argv[1]))
asked = []
async def resume(sid, _watcher, _rec):  # Live Mode's, which opens the window
    asked.append(sid)
    if sid != "s1":
        return {"status": "rejected", "reason": "session directory is gone"}
    watcher.states = [{"pane_id": "%40", "birth": "77", "session": "s"}]
    return {"status": "opened", "pane_id": "%40"}
async def ready(_watcher, pane):
    asked.append(pane)
server.live.resume, server.restore.ready = resume, ready
c = TestClient(server.app)
to = lambda name: c.get(f"/api/scratch/{name}/pane").json()
print(int((to("made")["pane"], to("made")["session"], to("anon")["session"]) == (None, "s1", None)))
r = c.post("/api/scratch/made/resume")
print(r.status_code, int(asked == ["s1", "%40"] and (to("made")["pane"], to("made")["birth"],
                                                    to("made")["session"]) == ("%40", "77", None)))
for name in ("lost", "anon"):
    r = c.post(f"/api/scratch/{name}/resume")
    print(r.status_code, r.json()["detail"].replace(" ", "-"))
"""


def test_resume_reopens_the_makers_conversation_and_sends_the_page_there(tmp_path):
    # The maker's pane has closed: Resume reopens the conversation meta.json records, waits
    # for its prompt and moves the page to that pane. A conversation that can't come back
    # says why, and a page with none recorded is not offered one.
    scratch = tmp_path / "scratch"
    for name, session in (("made", "s1"), ("lost", "s2"), ("anon", None)):
        (scratch / name).mkdir(parents=True)
        (scratch / name / "index.html").write_text("page")
        (scratch / name / "meta.json").write_text(json.dumps(
            {"pane": "%7", **({"agent_session": session} if session else {})}))
    env = {"TMUXRC_SCRATCH_DIR": str(scratch), "XDG_STATE_HOME": str(tmp_path / "state")}
    assert _probe(env, tmp_path, RESUME, str) == [
        "1", "200", "1", "409", "session-directory-is-gone",
        "409", "no-conversation-was-recorded-for-this-page"]


def _probe(env, tmp_path, probe=PROBE, read=int):
    env = {**os.environ, "AGENT_HISTORY_DIR": "/nonexistent", **env}  # no real transcripts
    out = subprocess.run([sys.executable, "-c", probe, str(tmp_path / "state" / "h.db")],
                         stdout=subprocess.PIPE, text=True, check=True, env=env,
                         timeout=60).stdout
    return [read(code) for code in out.split()]


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
    (scratch / "wip" / "meta.json").write_text('{"title": "A new page"}')
    (scratch / "rev").mkdir()
    (scratch / "rev" / "index.md").write_text("# Rev")
    (scratch / "rev" / "meta.json").write_text('{"title": "Rev", "updating": "the intro"}')
    (tmp_path / "secret.txt").write_text("outside")
    (scratch / "link.txt").symlink_to(tmp_path / "secret.txt")
    # file, traversal and a symlink out both refused, directory index, bare prefix
    # redirected to the slash form, a Markdown page, the page (rendered or raw Markdown,
    # and a committed mock) sandboxed so its
    # scripts can't drive /api/* (nor frame it, or /m), and a nested directory's slash
    # redirect path-only (an absolute one would be http:// behind the tunnel), the
    # bare-prefix one keeping its query; and only the folder's page (or the placeholder of
    # one in progress, answering 202) counted as a visit.
    # Hidden files and directories (an .env, a .git) are refused, as the gallery skips them.
    # The placeholder marks itself apart from the page, and a plain 404 scans no accounts.
    assert _probe({"TMUXRC_SCRATCH_DIR": str(scratch)}, tmp_path) == [
        200, 404, 404, 200, 307, 200, 404, 404, 404, 202, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
        1, 1, 1, 200, 200, 202, 200, 202, 202, 400, 1, 1, 1, 404, 2, 1, 2,
        1, 1, 1, 1, 1, 1, 2, 2, 1, 1]


def test_scratch_is_off_unless_configured(tmp_path):
    # Empty rather than absent: the daemon loads .env without overriding, so an operator
    # who enabled scratch there would otherwise switch it back on under this test.
    assert _probe({"TMUXRC_SCRATCH_DIR": ""}, tmp_path)[:3] == [404, 404, 404]
