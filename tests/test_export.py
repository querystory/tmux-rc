"""Export all (openbus/export.py): a round trip into another HOME, and import's refusals.
Every file here is invented, in temp HOMEs."""

import io
import json
import socket
import sqlite3
import tarfile

import pytest

from openbus import export, history
from openbus.history import History


@pytest.fixture
def home(monkeypatch, tmp_path):
    for var in ("XDG_STATE_HOME", "XDG_DATA_HOME", "TMUXRC_HISTORY_DB", "TMUXRC_SCRATCH_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(export, "_running", lambda: False)
    places = export.places  # the checkout's own .env stays out of it
    monkeypatch.setattr(export, "places", lambda: {**places(), ".env": tmp_path / "none"})

    def at(name):
        (tmp_path / name).mkdir()
        monkeypatch.setenv("HOME", str(tmp_path / name))
        return tmp_path / name
    return at


def write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def source(home):
    old = home("oldé")  # JSON escapes the é
    History(history.default_path())
    with sqlite3.connect(history.default_path()) as db:
        db.execute("INSERT INTO layout_snapshots VALUES ('boot:1:2', 0, ?, NULL)",
                   (json.dumps({"cwd": f"{old}/src/demo"}),))
        db.execute("INSERT INTO plan_usage VALUES ('codex', ?, '5h', 18000, 0, 1, NULL)",
                   (f"{old}/.codex",))
    write(old / ".local/state/tmux-rc/accounts.json", json.dumps({f"{old}/.claude": {}}))
    write(old / ".local/state/tmux-rc/push.json", "{}")
    write(old / ".local/share/tmux-rc/mocks/a/index.html")
    write(old / ".local/share/tmux-rc/docs/index.html")
    write(old / ".local/share/tmux-rc/scratch/demo/index.html")
    write(old / ".claude/projects/-demo/s1.jsonl")
    write(old / ".claude/.credentials.json")
    write(old / ".claude/statsig/cache")
    write(old / ".claude-work/CLAUDE.md")
    (old / ".claude-work/settings.json").symlink_to(old / ".claude/projects")  # a shared file
    return old


def test_round_trip(home, monkeypatch):
    old = source(home)
    out = io.BytesIO()
    assert export.export(out, ["scratch", "agents"]) == []
    sizes = export.sizes()  # what the walk counts is what export holds: no cache, no login
    assert sizes["secrets"] == len("{}x") and sizes["scratch"] == 1
    assert sizes["agents"] == len("xx") + len(f"{old}/.claude/projects".encode())  # 2 files, a link
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
        names = tar.getnames()
    assert names[:2] == ["manifest.json", "history.sqlite3"]
    assert {"accounts.json", "data/mocks/a/index.html", "scratch/demo/index.html",
            "agents/.claude/projects/-demo/s1.jsonl"} <= set(names)
    assert not [n for n in names if n.endswith(("push.json", "credentials.json", "statsig"))
                or n.startswith(("data/docs", "data/scratch"))]

    alone = io.BytesIO()  # credentials without conversations: no agent dir to replace
    export.export(alone, ["secrets"])
    with tarfile.open(fileobj=io.BytesIO(alone.getvalue())) as tar:
        assert "push.json" in tar.getnames() and not [n for n in tar.getnames() if "agents" in n]

    new = home("new")
    write(new / ".claude/.credentials.json", "this host's")
    monkeypatch.setattr(socket, "gethostname", lambda: "newhost")
    report = export.restore(io.BytesIO(out.getvalue()))
    assert "Adopted the DB from" in "".join(report)
    History(history.default_path())  # the daemon starts: the DB is this host's now
    with sqlite3.connect(history.default_path()) as db:
        assert json.loads(db.execute("SELECT payload FROM layout_snapshots").fetchone()[0]) == (
            {"cwd": f"{new}/src/demo"})
        assert db.execute("SELECT account FROM plan_usage").fetchone()[0] == f"{new}/.codex"
    assert json.loads((new / ".local/state/tmux-rc/accounts.json").read_text()) == {
        f"{new}/.claude": {}}
    assert (new / ".claude/projects/-demo/s1.jsonl").exists()
    assert (new / ".claude-work/settings.json").readlink() == new / ".claude/projects"
    assert (new / ".claude/.credentials.json").read_text() == "this host's"  # kept, not imported
    assert [p.name for p in new.glob(".claude.pre-import-*")]
    assert (new / ".local/share/tmux-rc/scratch/demo/index.html").exists()
    assert not (new / ".local/state/tmux-rc/push.json").exists()


def test_refusals(home, monkeypatch):
    source(home)
    out = io.BytesIO()
    export.export(out, [])
    home("new")
    monkeypatch.setattr(history, "MIGRATIONS", history.MIGRATIONS[:1])
    with pytest.raises(SystemExit, match="newer"):
        export.restore(io.BytesIO(out.getvalue()))
    monkeypatch.setattr(export, "_running", lambda: True)
    with pytest.raises(SystemExit, match="running"):
        export.restore(io.BytesIO(out.getvalue()))


def test_a_linked_state_file_is_refused(home, tmp_path):
    """An archive whose accounts.json links outside staging never writes through it."""
    source(home)
    out, outside = io.BytesIO(), tmp_path / "outside.json"
    outside.write_text("{}")
    export.export(out, [])
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
        manifest = tar.extractfile("manifest.json").read()
    crafted = io.BytesIO()
    with tarfile.open(fileobj=crafted, mode="w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        link = tarfile.TarInfo("/accounts.json")
        link.type, link.linkname = tarfile.SYMTYPE, str(outside)
        tar.addfile(link)
    home("new")
    export.restore(io.BytesIO(crafted.getvalue()))
    assert outside.read_text() == "{}"


def test_nested_places_stay_their_parts(home, monkeypatch):
    """State and scratch inside the data dir: credentials go only as credentials, and an
    import without scratch leaves this host's pages where they are."""
    old = home("old")
    monkeypatch.setenv("XDG_STATE_HOME", str(old / "share"))
    monkeypatch.setenv("XDG_DATA_HOME", str(old / "share"))
    monkeypatch.setenv("TMUXRC_SCRATCH_DIR", str(old / "share/tmux-rc/previews/pages"))
    write(old / "share/tmux-rc/push.json", "{}")
    write(old / "share/tmux-rc/previews/pages/demo/index.html")
    write(old / "share/tmux-rc/previews/other.txt")
    out = io.BytesIO()
    export.export(out, [])
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
        names = tar.getnames()
    assert "data/previews/other.txt" in names
    assert not [n for n in names if "push.json" in n or "pages" in n]

    new = home("new")
    for var, path in (("XDG_STATE_HOME", "share"), ("XDG_DATA_HOME", "share"),
                      ("TMUXRC_SCRATCH_DIR", "share/tmux-rc/previews/pages")):
        monkeypatch.setenv(var, str(new / path))
    write(new / "share/tmux-rc/previews/pages/mine/index.html")
    export.restore(io.BytesIO(out.getvalue()))
    assert (new / "share/tmux-rc/previews/other.txt").exists()
    assert (new / "share/tmux-rc/previews/pages/mine/index.html").exists()
