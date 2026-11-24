"""Publishing a scratch folder (openbus/scratch_publish.py), against a fake bucket: no test
reaches the network."""

import json
import threading

import google.auth
import google.auth.transport.requests
import pytest

from openbus import scratch_publish as P
from openbus.server import WEB_DIR

BUCKET: dict[str, bytes] = {}


class Fake:
    fail = 0  # how many uploads succeed before one is refused

    def __init__(self, target: str):
        assert target == "gs://bkt/people/ada"

    def put(self, key, path):
        if self.fail and len(BUCKET) >= self.fail:
            raise RuntimeError("403 Forbidden")
        BUCKET[key] = path.read_bytes()

    def drop(self, key):
        BUCKET.pop(key, None)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TMUXRC_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("TMUXRC_SCRATCH_PUBLISH", "gs://bkt/people/ada/")
    monkeypatch.setenv("TMUXRC_SCRATCH_PUBLISH_URL", "https://pages.example/ada")
    monkeypatch.setattr(P, "DRIVER", Fake)
    BUCKET.clear()
    folder = tmp_path / "scratch" / "tea notes"
    folder.mkdir(parents=True)
    (folder / "index.md").write_text("# Oolong\n\nSteep twice.")
    (folder / "leaf.txt").write_text("green")
    (folder / "meta.json").write_text('{"title": "Tea", "pane": "%9"}')
    return tmp_path / "scratch"


def _meta(root):
    return json.loads((root / "tea notes/meta.json").read_text())


def test_publish_sends_what_changed_and_unpublish_removes_it(root, capsys):
    P.cli(["publish", "tea notes"], WEB_DIR)
    there = "https://pages.example/ada/tea%20notes/"
    assert capsys.readouterr().out == there + "\n"
    assert _meta(root) == {"title": "Tea", "pane": "%9", "publish": there}
    assert sorted(BUCKET) == ["index.html", "tea notes/_app/scratch.svg", "tea notes/index.html",
                              "tea notes/index.md", "tea notes/leaf.txt"]  # never meta.json
    BUCKET["tea notes/index.md"] = b"as sent"  # the next copy sends only what changed
    (root / "tea notes/leaf.txt").unlink()
    (root / "tea notes/cup.txt").write_text("white")
    P.publish(root, "tea notes", WEB_DIR)
    assert BUCKET["tea notes/index.md"] == b"as sent" and BUCKET["tea notes/cup.txt"] == b"white"
    assert "tea notes/leaf.txt" not in BUCKET
    (root / "tea notes/meta.json").write_text(json.dumps({**_meta(root), "updating": "v2"}))
    with pytest.raises(SystemExit, match="being updated"):  # half a revision is never sent
        P.publish(root, "tea notes", WEB_DIR)
    P.cli(["unpublish", "tea notes"], WEB_DIR)
    assert BUCKET == {} and "publish" not in _meta(root)
    with pytest.raises(SystemExit, match="not published"):  # a job queued before it, too
        P.publish(root, "tea notes", WEB_DIR)


def test_only_a_folder_with_an_index_is_published(root):
    (root / "tea notes/index.md").rename(root / "tea notes/page.md")
    (root / "tea notes/index.md").symlink_to("page.md")  # one export leaves out
    with pytest.raises(SystemExit):
        P.cli(["publish", "tea notes"], WEB_DIR)
    (root / "tea notes/index.md").unlink()
    (root / "tea notes/page.md").rename(root / "tea notes/index.md")
    (root / "tea notes/index.md").rename(root / "tea notes/report.md")
    with pytest.raises(SystemExit):  # its URL, the folder's, would open nothing
        P.cli(["publish", "tea notes"], WEB_DIR)
    (root / "tea notes/report.md").unlink()
    (root / "tea notes/leaf.txt").unlink()  # its meta.json alone: a page in progress
    with pytest.raises(SystemExit):
        P.cli(["publish", "tea notes"], WEB_DIR)
    assert BUCKET == {} and "publish" not in _meta(root)


def test_an_archived_folder_unpublishes_too(root, capsys):
    P.cli(["publish", "tea notes"], WEB_DIR)
    (root / ".archive").mkdir()
    (root / "tea notes").rename(root / ".archive/tea notes")
    P.cli(["unpublish", "tea notes"], WEB_DIR)
    assert BUCKET == {} and "publish" not in json.loads(
        (root / ".archive/tea notes/meta.json").read_text())


def test_a_copy_started_before_an_unpublish_sends_nothing(root, monkeypatch):
    P.cli(["publish", "tea notes"], WEB_DIR)
    P.cli(["unpublish", "tea notes"], WEB_DIR)
    (root / "tea notes/meta.json").write_text(json.dumps({**_meta(root), "publish": "x"}))
    flock = P.fcntl.flock

    def unpublished_meanwhile(*args):  # Unpublish lands while this copy waits for the lock
        P.opt(root, "tea notes", on=False)
        flock(*args)
    monkeypatch.setattr(P.fcntl, "flock", unpublished_meanwhile)
    P.publish(root, "tea notes", WEB_DIR)  # not opted in by the time it would copy: nothing
    assert BUCKET == {}


def test_a_ready_page_is_copied_unless_unpublished_or_revising(root, monkeypatch):
    jobs = []
    monkeypatch.setattr(P, "later", lambda job, name, tell: jobs.append(name))
    item = {"publish": "https://pages.example/ada/tea%20notes/"}
    meta = root / "tea notes/meta.json"
    for publish, updating in ((item["publish"], ""), (item["publish"], "v2"), (None, "")):
        meta.write_text(json.dumps({"publish": publish, "updating": updating}))
        P.ready(root, "tea notes", WEB_DIR, None)  # as it is now, not as it was queued
    assert jobs == ["tea notes"]


def test_a_failed_copy_is_pushed_and_never_raises(root, monkeypatch):
    P.cli(["publish", "tea notes"], WEB_DIR)
    BUCKET.clear()
    (root / "tea notes/leaf.txt").write_text("oolong")  # one sent, then the other refused
    (root / "tea notes/cup.txt").write_text("white")
    monkeypatch.setattr(Fake, "fail", 1)
    told = []
    for job in (lambda: P.publish(root, "tea notes", WEB_DIR),
                lambda: monkeypatch.setenv("TMUXRC_SCRATCH_PUBLISH", "") or P.unpublish(root, "x")):
        P.later(job, "tea notes", told.append)
        for t in threading.enumerate():
            if t.name == "scratch-publish":
                t.join()
    assert [m["body"][:30] for m in told] == ["403 Forbidden", "set TMUXRC_SCRATCH_PUBLISH (gs"]
    assert {m["title"] for m in told} == {"Publish failed: tea notes"}
    assert len(BUCKET) == 1 and _meta(root)["publish_failed"] == "403 Forbidden"  # its card's
    monkeypatch.setenv("TMUXRC_SCRATCH_PUBLISH", "gs://bkt/people/ada")
    monkeypatch.setattr(Fake, "fail", 0)
    P.publish(root, "tea notes", WEB_DIR)  # copied at last: the failure goes
    assert "publish_failed" not in _meta(root)
    P.cli(["unpublish", "tea notes"], WEB_DIR)  # what was sent before a refusal is on record
    assert BUCKET == {}


def test_the_index_lists_what_is_published_while_anything_is(root):
    (root / "rye").mkdir()
    (root / "rye/index.html").write_text("<title>Rye</title>")
    P.cli(["publish", "tea notes"], WEB_DIR)
    (root / "tea notes/meta.json").write_text(json.dumps({**_meta(root), "description": "a <b>"}))
    P.act(root, "publish", ["rye"], WEB_DIR, None)  # the gallery's Publish, in the background
    for t in threading.enumerate():
        if t.name == "scratch-publish":
            t.join()
    index = BUCKET["index.html"].decode()
    assert '<a href="rye/">Rye</a>' in index and '<a href="tea%20notes/">Tea</a>' in index
    assert "a &lt;b&gt;" in index and _meta(root)["publish"].endswith("/ada/tea%20notes/")
    BUCKET["index.html"] = b"as sent"
    P.publish(root, "rye", WEB_DIR)  # nothing listed changed: not sent again
    assert BUCKET["index.html"] == b"as sent"
    (root / "index.html").mkdir()  # a folder named as the root's page is staged apart
    (root / "index.html/index.html").write_text("<title>Odd</title>")
    P.cli(["publish", "index.html"], WEB_DIR)
    assert "Odd" in BUCKET["index.html"].decode() and "index.html/index.html" in BUCKET
    P.cli(["unpublish", "index.html"], WEB_DIR)
    P.cli(["unpublish", "tea notes"], WEB_DIR)
    assert "tea%20notes" not in BUCKET["index.html"].decode()
    with pytest.raises(ValueError, match="not a folder"):  # a path is never one, and one
        P.act(root, "publish", ["tea notes", "../rye"], WEB_DIR, None)  # refuses them all
    assert "publish" not in _meta(root)
    (root / ".archive").mkdir()  # archived, and its name taken by a folder never opted in
    (root / "rye").rename(root / ".archive/rye")
    (root / "rye").mkdir()
    (root / "rye/index.html").write_text("<title>Other</title>")
    P.cli(["publish", "tea notes"], WEB_DIR)
    assert '<a href="rye/">Rye</a>' in BUCKET["index.html"].decode()  # the opted-in one's
    P.unpublish(root, "rye")
    P.unpublish(root, "tea notes")  # still opted in: an older Unpublish job removes nothing
    assert "tea notes/index.md" in BUCKET
    P.cli(["unpublish", "tea notes"], WEB_DIR)
    assert BUCKET == {}


def test_the_gcs_driver_writes_and_deletes_by_name_and_never_lists(tmp_path, monkeypatch):
    calls = []

    class Response:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    class Session:
        def __init__(self, creds):
            assert creds == "adc"

        def post(self, url, **kw):
            calls.append(("POST", url, kw["params"]["name"], kw["headers"]["Content-Type"]))
            return Response(200)

        def delete(self, url, **kw):
            calls.append(("DELETE", url))
            return Response(404)  # already gone is fine

    monkeypatch.setattr(google.auth, "load_credentials_from_file",
                        lambda key, scopes: ("adc", "proj") if key == "pub.json" else None)
    monkeypatch.setattr(google.auth.transport.requests, "AuthorizedSession", Session)
    monkeypatch.delenv("TMUXRC_SCRATCH_PUBLISH_KEY", raising=False)
    with pytest.raises(SystemExit, match="TMUXRC_SCRATCH_PUBLISH_KEY"):  # never the Vertex key
        P.Gcs("gs://bkt/people/ada")
    monkeypatch.setenv("TMUXRC_SCRATCH_PUBLISH_KEY", "pub.json")
    (tmp_path / "a.html").write_text("<p>hi")
    gcs = P.Gcs("gs://bkt/people/ada")
    gcs.put("tea/a.html", tmp_path / "a.html")
    gcs.drop("tea/b c.txt")
    assert calls == [
        ("POST", "https://storage.googleapis.com/upload/storage/v1/b/bkt/o",
         "people/ada/tea/a.html", "text/html"),
        ("DELETE", "https://storage.googleapis.com/storage/v1/b/bkt/o/people%2Fada%2Ftea%2Fb%20c.txt")]
