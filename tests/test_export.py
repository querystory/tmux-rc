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

    def at(name):
        (tmp_path / name).mkdir()
        monkeypatch.setenv("HOME", str(tmp_path / name))
        return tmp_path / name
    return at


def write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def source(home):
    old = home("old")
    History(history.default_path())
    with sqlite3.connect(history.default_path()) as db:
        db.execute("INSERT INTO layout_snapshots VALUES ('boot:1:2', 0, ?, NULL)",
                   (json.dumps({"cwd": f"{old}/src/demo"}),))
    write(old / ".local/state/tmux-rc/accounts.json", json.dumps({f"{old}/.claude": {}}))
    write(old / ".local/state/tmux-rc/push.json", "{}")
    write(old / ".local/share/tmux-rc/mocks/a/index.html")
    write(old / ".local/share/tmux-rc/docs/index.html")
    write(old / ".local/share/tmux-rc/scratch/demo/index.html")
    write(old / ".claude/projects/-demo/s1.jsonl")
    write(old / ".claude/.credentials.json")
    write(old / ".claude/statsig/cache")
    return old


def test_round_trip(home, monkeypatch):
    source(home)
    out = io.BytesIO()
    left = export.export(out)
    assert "push.json" in left and "the agents' sign-ins (.credentials.json, auth.json)" in left
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
        names = tar.getnames()
    assert names[:2] == ["manifest.json", "history.sqlite3"]
    assert {"accounts.json", "data/mocks/a/index.html", "scratch/demo/index.html",
            "agents/.claude/projects/-demo/s1.jsonl"} <= set(names)
    assert not [n for n in names if n.endswith(("push.json", "credentials.json", "statsig"))
                or n.startswith("data/docs")]

    new = home("new")
    write(new / ".claude/.credentials.json", "this host's")
    monkeypatch.setattr(socket, "gethostname", lambda: "newhost")
    report = export.restore(io.BytesIO(out.getvalue()))
    assert "Adopted the DB from" in "".join(report)
    History(history.default_path())  # the daemon starts: the DB is this host's now
    with sqlite3.connect(history.default_path()) as db:
        assert db.execute("SELECT payload FROM layout_snapshots").fetchone()[0] == (
            json.dumps({"cwd": f"{new}/src/demo"}))
    assert json.loads((new / ".local/state/tmux-rc/accounts.json").read_text()) == {
        f"{new}/.claude": {}}
    assert (new / ".claude/projects/-demo/s1.jsonl").exists()
    assert (new / ".claude/.credentials.json").read_text() == "this host's"  # kept, not imported
    assert [p.name for p in new.glob(".claude.pre-import-*")]
    assert (new / ".local/share/tmux-rc/scratch/demo/index.html").exists()
    assert not (new / ".local/state/tmux-rc/push.json").exists()


def test_refusals(home, monkeypatch):
    source(home)
    out = io.BytesIO()
    export.export(out, scratch=False, agents=False)
    home("new")
    monkeypatch.setattr(history, "MIGRATIONS", history.MIGRATIONS[:1])
    with pytest.raises(SystemExit, match="newer"):
        export.restore(io.BytesIO(out.getvalue()))
    monkeypatch.setattr(export, "_running", lambda: True)
    with pytest.raises(SystemExit, match="running"):
        export.restore(io.BytesIO(out.getvalue()))
