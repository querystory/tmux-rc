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
    fail = False

    def __init__(self, target: str):
        assert target == "gs://bkt/people/ada"

    def put(self, key, path):
        if self.fail:
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
    assert sorted(BUCKET) == ["tea notes/_app/scratch.svg", "tea notes/index.html",
                              "tea notes/index.md", "tea notes/leaf.txt"]  # never meta.json
    BUCKET["tea notes/index.md"] = b"as sent"  # the next copy sends only what changed
    (root / "tea notes/leaf.txt").unlink()
    (root / "tea notes/cup.txt").write_text("white")
    P.publish(root, "tea notes", WEB_DIR)
    assert BUCKET["tea notes/index.md"] == b"as sent" and BUCKET["tea notes/cup.txt"] == b"white"
    assert "tea notes/leaf.txt" not in BUCKET
    P.cli(["unpublish", "tea notes"], WEB_DIR)
    assert BUCKET == {} and "publish" not in _meta(root)


def test_a_ready_page_is_copied_unless_unpublished_or_revising(root, monkeypatch):
    jobs = []
    monkeypatch.setattr(P, "later", lambda job, name, tell: jobs.append(name))
    item = {"name": "tea notes", "publish": "https://pages.example/ada/tea%20notes/",
            "revising": False}
    P.ready(root, item, WEB_DIR, None)
    P.ready(root, {**item, "revising": True}, WEB_DIR, None)
    P.ready(root, {**item, "publish": None}, WEB_DIR, None)
    assert jobs == ["tea notes"]


def test_a_failed_copy_is_pushed_and_never_raises(root, monkeypatch):
    monkeypatch.setattr(Fake, "fail", True)
    told = []
    for job in (lambda: P.publish(root, "tea notes", WEB_DIR),
                lambda: monkeypatch.delenv("TMUXRC_SCRATCH_PUBLISH") or P.unpublish("tea notes")):
        P.later(job, "tea notes", told.append)
        for t in threading.enumerate():
            if t.name == "scratch-publish":
                t.join()
    assert [m["body"][:30] for m in told] == ["403 Forbidden", "set TMUXRC_SCRATCH_PUBLISH (gs"]
    assert {m["title"] for m in told} == {"Publish failed: tea notes"} and BUCKET == {}


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
