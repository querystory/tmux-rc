"""Export all (openbus/export.py): a round trip into another HOME, and import's refusals.
Every file here is invented, in temp HOMEs."""

import contextlib
import io
import json
import shutil
import socket
import sqlite3
import tarfile
import tempfile
from pathlib import Path

import pytest

from openbus import config, export, history
from openbus.history import History


@pytest.fixture
def home(monkeypatch, tmp_path):
    for var in ("XDG_STATE_HOME", "XDG_DATA_HOME", "TMUXRC_HISTORY_DB", "TMUXRC_SCRATCH_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(export, "_running", lambda: False)
    monkeypatch.setattr(export, "_agents_running", list)
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


def test_round_trip(home, monkeypatch, tmp_path):
    old = source(home)
    out = io.BytesIO()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "none"))  # the snapshot: not /tmp
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


def crafted(home, *members: tuple[tarfile.TarInfo, bytes]) -> io.BytesIO:
    """A real export's manifest, then `members`: what a hostile archive could hold."""
    source(home)
    out = io.BytesIO()
    export.export(out, [])
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
        manifest = tar.extractfile("manifest.json").read()
    made = io.BytesIO()
    with tarfile.open(fileobj=made, mode="w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        for member, data in members:
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
        tar.addfile(tarfile.TarInfo(export.DONE))
    home("new")
    return io.BytesIO(made.getvalue())


def test_a_linked_state_file_is_refused(home, tmp_path):
    """An archive whose accounts.json links outside staging never writes through it."""
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    link = tarfile.TarInfo("/accounts.json")
    link.type, link.linkname = tarfile.SYMTYPE, str(outside)
    export.restore(crafted(home, (link, b"")))
    assert outside.read_text() == "{}"


def test_an_import_never_replaces_a_place_it_left_out(home, monkeypatch, tmp_path):
    """The archive's data/pages is this host's scratch: refused, before anything moves."""
    archive = crafted(home, (tarfile.TarInfo("data/pages/note.txt"), b"theirs"))
    monkeypatch.setenv("TMUXRC_SCRATCH_DIR", str(tmp_path / "new/.local/share/tmux-rc/pages"))
    write(tmp_path / "new/.local/share/tmux-rc/pages/mine/index.html", "mine")
    with pytest.raises(SystemExit, match="not its own"):
        export.restore(archive)
    assert (tmp_path / "new/.local/share/tmux-rc/pages/mine/index.html").read_text() == "mine"
    assert not list((tmp_path / "new").glob("**/*.pre-import-*"))


def test_only_agents_dirs_are_replaced_in_home(home):
    archive = crafted(home, (tarfile.TarInfo("agents/.bashrc"), b"echo hi"))
    with pytest.raises(SystemExit, match="no agent's dir"):
        export.restore(archive)


def test_an_archives_own_account_store_does_not_admit_a_dir(home, tmp_path):
    store = json.dumps({f"{tmp_path}/oldé/.ssh": {}}).encode()  # the archive's word is not enough
    archive = crafted(home, (tarfile.TarInfo("accounts.json"), store),
                      (tarfile.TarInfo("agents/.ssh/authorized_keys"), b"key"))
    with pytest.raises(SystemExit, match="no agent's dir"):
        export.restore(archive)


def test_a_db_from_before_versioning_is_adopted(home):
    home("old")
    history.default_path().parent.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(history.default_path())) as db:
        db.execute("CREATE TABLE snapshots (t REAL PRIMARY KEY, panes TEXT NOT NULL)")  # alone
    out = io.BytesIO()
    export.export(out, [])
    home("new")
    export.restore(io.BytesIO(out.getvalue()))
    History(history.default_path())  # this host's now


def test_two_parts_landing_at_one_place_are_refused(home):
    archive = crafted(home, (tarfile.TarInfo("data/scratch/a/index.html"), b"old"),
                      (tarfile.TarInfo("scratch/b/index.html"), b"new"))
    with pytest.raises(SystemExit, match="one place"):
        export.restore(archive)


def test_nested_places_stay_their_parts(home, monkeypatch):
    """State and scratch inside the data dir: credentials and the account store go only as
    themselves, a backup not at all, and an import without scratch leaves this host's pages
    (and its DB with its WAL) where they are."""
    old = home("old")
    monkeypatch.setenv("XDG_STATE_HOME", str(old / "share"))
    monkeypatch.setenv("XDG_DATA_HOME", str(old / "share"))
    monkeypatch.setenv("TMUXRC_SCRATCH_DIR", str(old / "share/tmux-rc/previews/pages"))
    write(old / "share/tmux-rc/push.json", "{}")
    blue = old / "share/tmux-rc/profiles/blue"  # an agent dir deeper than HOME: by hand
    write(old / "share/tmux-rc/accounts.json", json.dumps({str(blue): {}}))
    write(blue / ".credentials.json")
    write(old / "share/tmux-rc/previews/push.json.pre-import-1", "{}")
    write(old / "pages/demo/index.html")  # scratch's dir, linked in from elsewhere
    (old / "share/tmux-rc/previews").mkdir(parents=True, exist_ok=True)
    (old / "share/tmux-rc/previews/pages").symlink_to(old / "pages")
    write(old / "share/tmux-rc/previews/other.txt")
    out = io.BytesIO()
    assert export.export(out, []) == []  # its agents weren't asked for
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
        names = tar.getnames()
    assert {"data/previews/other.txt", "accounts.json"} <= set(names)
    assert not [n for n in names if "push.json" in n or "pages" in n or "data/acc" in n
                or "profiles/" in n]

    new = home("new")
    for var, path in (("XDG_STATE_HOME", "share"), ("XDG_DATA_HOME", "share"),
                      ("TMUXRC_SCRATCH_DIR", "share/tmux-rc/previews/pages"),
                      ("TMUXRC_HISTORY_DB", "share/tmux-rc/previews/pages/h.sqlite3")):
        monkeypatch.setenv(var, str(new / path))
    write(new / "share/tmux-rc/previews/pages/mine/index.html")
    write(new / "share/tmux-rc/previews/pages/h.sqlite3-wal")
    export.restore(io.BytesIO(out.getvalue()))
    assert (new / "share/tmux-rc/previews/other.txt").exists()
    assert (new / "share/tmux-rc/previews/pages/mine/index.html").exists()
    assert (new / "share/tmux-rc/previews/pages/h.sqlite3-wal").exists()


def test_a_linked_agent_dir_and_a_data_dir_inside_config(home, monkeypatch):
    """~/.claude linking to ~/.claude-work goes as both; a data dir that is tmux-rc's config
    dir holds credentials, so its contents go only as those."""
    old = home("old")
    write(old / ".claude-work/projects/-demo/s1.jsonl")
    (old / ".claude").symlink_to(old / ".claude-work")
    monkeypatch.setenv("XDG_DATA_HOME", str(old / ".config"))
    write(old / ".config/tmux-rc/openai.env", "KEY=x")
    out = io.BytesIO()
    export.export(out, ["agents"])
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
        names = tar.getnames()
    assert {"agents/.claude/projects/-demo/s1.jsonl",
            "agents/.claude-work/projects/-demo/s1.jsonl"} <= set(names)
    assert not [n for n in names if "openai" in n]


def test_the_dotenv_is_the_one_the_daemon_loads(monkeypatch, tmp_path):
    """No checkout .env: tmux-rc's config dir's, never one up from the working directory."""
    monkeypatch.setattr(config, "__file__", str(tmp_path / "site/openbus/config.py"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("")
    assert config.dotenv_path() == tmp_path / "home/.config/tmux-rc/.env"


def test_a_linked_place_is_kept_by_its_target(home, monkeypatch):
    """A scratch dir linked into the data dir here stays through an import without scratch,
    and a data dir linked into tmux-rc's config dir sends no provider keys as state."""
    old = home("old")
    write(old / "previews/other.txt")
    (old / ".local/share").mkdir(parents=True)
    (old / ".local/share/tmux-rc").symlink_to(old / ".config/tmux-rc")
    write(old / ".config/tmux-rc/openai.env", "KEY=x")
    (old / ".config/tmux-rc/previews").symlink_to(old / "previews")
    out = io.BytesIO()
    export.export(out, [])
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
        names = tar.getnames()
    assert "data/previews/other.txt" in names and not [n for n in names if "openai" in n]
    new = home("new")
    monkeypatch.setenv("TMUXRC_SCRATCH_DIR", str(new / "pages"))
    write(new / ".local/share/tmux-rc/previews/pages/mine/index.html")
    (new / "pages").symlink_to(new / ".local/share/tmux-rc/previews/pages")
    export.restore(io.BytesIO(out.getvalue()))
    assert (new / "pages/mine/index.html").exists()
    assert (new / ".local/share/tmux-rc/previews/other.txt").exists()


def test_a_file_in_the_way_of_a_kept_place_is_refused(home, monkeypatch, tmp_path):
    archive = crafted(home, (tarfile.TarInfo("data/previews"), b"a file"))
    monkeypatch.setenv("TMUXRC_SCRATCH_DIR", str(tmp_path / "new/.local/share/tmux-rc/previews/p"))
    write(tmp_path / "new/.local/share/tmux-rc/previews/p/mine/index.html")
    with pytest.raises(SystemExit, match="not its own"):
        export.restore(archive)


def test_a_data_part_never_lands_inside_another_place(home, monkeypatch, tmp_path):
    """Here the data dir is tmux-rc's config dir: a state-only archive can't write its keys."""
    archive = crafted(home, (tarfile.TarInfo("data/openai.env"), b"KEY=theirs"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "new/.config"))
    write(tmp_path / "new/.config/tmux-rc/openai.env", "KEY=mine")
    with pytest.raises(SystemExit, match="would land in"):
        export.restore(archive)
    assert (tmp_path / "new/.config/tmux-rc/openai.env").read_text() == "KEY=mine"


def test_an_export_cut_short_is_not_imported(home, monkeypatch):
    source(home)
    real, out = export._entries, io.BytesIO()

    def failing(parts):
        yield next(real(parts))
        raise OSError("disk gone")
    monkeypatch.setattr(export, "_entries", failing)
    with pytest.raises(OSError, match="disk gone"):
        export.export(out, [])
    home("new")
    with pytest.raises(SystemExit, match="ends early"):
        export.restore(io.BytesIO(out.getvalue()))
    assert not list(Path.home().iterdir())


def test_a_link_on_the_way_to_a_place_not_made_yet_is_refused(home, monkeypatch, tmp_path):
    """A fresh host: nothing at scratch's path yet, and the archive's data/p/r links out."""
    link = tarfile.TarInfo("data/p/r")
    link.type, link.linkname = tarfile.SYMTYPE, str(tmp_path)
    archive = crafted(home, (link, b""), (tarfile.TarInfo("scratch/a.html"), b"x"))
    monkeypatch.setenv("TMUXRC_SCRATCH_DIR", str(tmp_path / "new/.local/share/tmux-rc/p/r/pages"))
    with pytest.raises(SystemExit, match="not its own"):
        export.restore(archive)


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): str(p.readlink()) if p.is_symlink() else p.read_bytes()
            for p in root.rglob("*") if p.is_symlink() or p.is_file()}


def test_an_import_failing_part_way_puts_everything_back(home, monkeypatch, capsys):
    """A failure at any move, a kept nested place's included, and a copy from staging cut
    short part way, leaves this host as it was."""
    source(home)
    out = io.BytesIO()
    export.export(out, ["scratch", "agents"])
    new = home("new")
    monkeypatch.setenv("TMUXRC_SCRATCH_DIR", str(new / ".local/share/tmux-rc/mocks/pages"))
    write(new / ".local/share/tmux-rc/mocks/pages/mine/index.html", "this host's")
    write(new / ".local/share/tmux-rc/mocks/b/index.html", "this host's")
    write(new / ".local/state/tmux-rc/accounts.json", "{}")
    write(new / ".claude/projects/-mine/s.jsonl", "this host's")
    before, move = tree(new), shutil.move
    for n in range(1, 100):
        calls = []

        def flaky(*args, n=n, calls=calls):
            calls.append(args)
            if len(calls) == n:
                if ".tmux-rc-import-" in str(args[0]) and Path(args[0]).is_dir():
                    shutil.copytree(args[0], Path(args[1], "half"))  # another disk's copy
                raise OSError("disk full")
            return move(*args)
        monkeypatch.setattr(shutil, "move", flaky)
        try:
            export.restore(io.BytesIO(out.getvalue()))
        except OSError:
            assert tree(new) == before, n
            assert "pre-import-" in capsys.readouterr().err
        else:
            break
    assert n > 5 and tree(new) != before  # it failed at every move, then went through


def test_running_agents_are_warned_about(home, monkeypatch, capsys):
    source(home)
    out = io.BytesIO()
    export.export(out, [])
    home("new")
    monkeypatch.setattr(export, "_agents_running", lambda: ["claude"])
    export.restore(io.BytesIO(out.getvalue()))
    assert "claude running here" in capsys.readouterr().err
