"""Export all: one .tgz holding what another machine needs to take this instance over, and
its import. `tmux-rc export` / `tmux-rc import`, and Settings' Export all.

PARTS is the one list of what an archive can hold: it names the CLI's flags, the web's
choices and the sizes shown beside them. Logins are off unless asked for: the file then
holds every sign-in and key. Agents' settings and conversations may hold keys either way.
Import replaces rather than merges: what it replaces is moved aside as
<name>.pre-import-<time>, and put back if the import fails part way. An archive can hold
hooks and settings that run code: import only one you made."""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from dotenv import dotenv_values

from . import accounts, config, history, push

FORMAT = 2
DONE = "complete"  # the archive's last member
# Part -> (label, note, in by default; None: always in).
PARTS = {
    "state": ("tmux-rc history & settings",
              "Pane history, plan usage, the tmux layout, accounts and mocks", None),
    "scratch": ("Scratch pages", "The preview gallery", True),
    "agents": ("Agent conversations", ("~/.claude*, ~/.codex*, so agents can resume. Their "
                                       "settings and conversations may still hold keys"), True),
    "secrets": ("Logins", ("Agents' sign-ins (with their conversations), "
                                "~/.config/tmux-rc, .env, push keys. The file then holds "
                                "every sign-in and key: keep it private."),
                False),
}
SECRETS = ("push.json", "config", ".env")  # the VAPID key and push endpoints; credentials
LOGINS = (".credentials.json", "auth.json")  # an agent dir's sign-in: Claude's, Codex's
IDENTITY = (*LOGINS, ".claude.json")  # the sign-in and who it is: they stay together
# What an agent rebuilds in its dir, left out: caches, debug logs, shell snapshots.
AGENT_DIRS = (".claude", ".codex", *accounts.MORE.values())  # what an import may replace in HOME
CACHES = ("cache", "debug", "statsig", "shell-snapshots", "log", "tmp", ".tmp")


def _data() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "tmux-rc"


def places() -> dict[str, Path]:
    """Archive name -> where it lives on this host, by the same helpers the daemon uses."""
    scratch = os.environ.get("TMUXRC_SCRATCH_DIR")
    return {"history.sqlite3": history.default_path(), "accounts.json": accounts.store_path(),
            "push.json": push.default_path(), "config": Path.home() / ".config/tmux-rc",
            ".env": config.dotenv_path(),
            "scratch": Path(scratch) if scratch else _data() / "scratch"}


def filename() -> str:
    return f"tmux-rc-export-{socket.gethostname()}-{time.strftime('%Y-%m-%d')}.tgz"


def _agent_dirs() -> list[Path]:
    return sorted({Path(d) for d in [*accounts.in_home(), *accounts.load()]
                   if Path(d).is_dir()})


def _roots():
    """(part, archive name, path) of each root an archive can hold, but the DB, which is
    snapshotted. docs/ is left out: `make docs` builds it."""
    where, home = places(), Path.home()
    yield "state", "accounts.json", where["accounts.json"]
    yield "scratch", "scratch", where["scratch"]
    yield from (("secrets", n, where[n]) for n in SECRETS)
    yield from (("state", f"data/{c.name}", c) for c in sorted(_data().glob("*"))
                if c.name != "docs")
    yield from (("agents", f"agents/{d.name}", d) for d in _agent_dirs() if d.parent == home)
    yield "agents", "agents/.claude.json", home / ".claude.json"  # the default ~/.claude's


def _part(part: str, name: str) -> str:
    """An agent's sign-in, and who it is, is a credential: it goes only with its agent."""
    bits = name.split("/")
    return "secrets" if part == "agents" and len(bits) <= 3 and bits[-1] in IDENTITY else part


def _own(name: str, path: Path, owner: dict) -> bool:
    """Whether `path` is no other place's: each place goes only as its own root."""
    return name in owner.get(path, {name})


def _within(at: Path, places) -> set[Path]:
    """The places that hold `at`, or what it links to."""
    return {*Path(os.path.abspath(at)).parents, *at.resolve().parents} & {*places}


def _spot(p: Path) -> Path:
    """Where `p` is, through any linked dir above it, so places compare as one; but not
    through `p` itself: a swap replaces a link rather than follow it."""
    return p.parent.resolve() / p.name


def _missing(error: OSError) -> None:
    """os.walk's error: a dir gone since its listing is skipped; any other ends the export,
    which would otherwise end whole but short."""
    if not isinstance(error, FileNotFoundError):
        raise error


def _entries(parts=PARTS):
    """(part, archive name, path) of every file, dir and link an archive of `parts` can
    hold: an agent dir without its caches, its sign-in as a credential, and each place
    (scratch, the account store, credentials, an agent dir) only as its own root, wherever
    it nests, nor the data dir's contents inside one. No import's backup, and a part not
    chosen is not walked."""
    where, home, at = places(), Path.home(), history.default_path()
    named = [(where[n], n) for n in ("scratch", "accounts.json", *SECRETS)] + [
        (d, f"agents/{d.name}" if d.parent == home else None) for d in _agent_dirs()]
    owner = {p.with_name(p.name + s): {None} for s in ("", "-wal", "-shm")  # snapshotted,
             for p in (Path(os.path.abspath(at)), _spot(at), at.resolve())}  # however named
    for at, n in named:  # a link, and its target: two names may share it (~/.claude linked)
        for p in (Path(os.path.abspath(at)), _spot(at), at.resolve()):
            owner.setdefault(p, set()).add(n)
    for part, name, at in _roots():
        root = at.resolve()  # a linked root: what it links to
        if part not in parts or not os.path.lexists(at) or ".pre-import-" in at.name \
                or not _own(name, root, owner) or (name.startswith("data/") and _within(at, owner)):
            continue
        yield _part(part, name), name, root
        for here, dirs, files in os.walk(root, onerror=_missing) if root.is_dir() else ():
            top, kept = here == str(root), []
            for child in sorted(dirs + files):
                path = Path(here, child)
                if (part == "agents" and top and child in CACHES) or ".pre-import-" in child \
                        or not _own(name, path, owner):
                    continue
                kept.append(child)
                arc = f"{name}/{path.relative_to(root)}"
                kind = _part(part, arc)  # a linked sign-in goes as what it links to
                yield kind, arc, path.resolve() if kind != part else path
            dirs[:] = [d for d in dirs if d in kept]


def _db() -> Path:
    return history.default_path().resolve()  # as_uri needs it absolute


def _read(db: Path):
    """A read-only connection: the backup API copies from it while the daemon writes."""
    return contextlib.closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True))


def sizes() -> dict[str, int]:
    """Each part's size on disk, uncompressed: the walk export makes, without reading, and
    the DB's pages, its WAL's included, as the snapshot holds them."""
    out = dict.fromkeys(PARTS, 0)
    with contextlib.suppress(sqlite3.Error), _read(_db()) as db:
        out["state"] = db.execute("SELECT page_count * page_size FROM pragma_page_count, "
                                  "pragma_page_size").fetchone()[0]
    for part, _, path in _entries():
        with contextlib.suppress(OSError):
            out[part] += path.lstat().st_size if not path.is_dir() or path.is_symlink() else 0
    return out


def export(out, parts) -> list[str]:
    """Stream the archive of `parts` (state always) into `out`, which need not seek.
    Returns what a person moves by hand: agent dirs deeper than HOME's top level."""
    parts = {"state", *parts}
    deeper = [str(d) for d in _agent_dirs() if d.parent != Path.home()] * ("agents" in parts)
    db = _db()
    # Beside the DB, not a /tmp that may be RAM, and gone before the walk could find it.
    with tarfile.open(fileobj=out, mode="w|gz") as tar:
        with tempfile.TemporaryDirectory(dir=db.parent if db.parent.is_dir() else None) as tmp:
            snapshot, schema = Path(tmp, "history.sqlite3"), None
            if db.exists():  # the backup API: a consistent copy while the daemon keeps writing
                with _read(db) as src, contextlib.closing(sqlite3.connect(snapshot)) as copy:
                    src.backup(copy)
                    schema = copy.execute("PRAGMA user_version").fetchone()[0]
            manifest = json.dumps({"format": FORMAT, "schema": schema,
                                   "host": socket.gethostname(), "home": str(Path.home()),
                                   "created": time.time(), "parts": sorted(parts),
                                   "left": deeper}).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mtime, info.mode = len(manifest), time.time(), 0o600
            tar.addfile(info, io.BytesIO(manifest))
            if schema is not None:
                snapshot.chmod(0o600)  # as private as the DB it copies, whatever the umask
                tar.add(snapshot, snapshot.name)
        for part, name, path in _entries(parts):
            if part in parts:
                with contextlib.suppress(FileNotFoundError):  # gone since the walk
                    info = tar.gettarinfo(path, name)
                    if info.islnk():  # a name sharing an inode goes as its bytes: import
                        info.type, info.size = tarfile.REGTYPE, path.stat().st_size  # drops links
                    with path.open("rb") if info.isreg() else contextlib.nullcontext() as f:
                        tar.addfile(info, f)
        tar.addfile(tarfile.TarInfo(DONE))  # last: an export that failed part way has none
    return deeper


def _running() -> bool:
    from .server import _bind  # noqa: PLC0415 - the daemon's own address, only here
    with contextlib.suppress(OSError), socket.create_connection(_bind(), timeout=1):
        return True
    return False


def _agents_running() -> list[str]:
    """Agents running here as this user, by exact process name: they rewrite ~/.claude.json
    and their settings from memory, over what an import put there."""
    return [n for n in ("claude", "codex") if shutil.which("pgrep") and subprocess.run(
        ["pgrep", "-xu", str(os.getuid()), n], capture_output=True, check=False).returncode == 0]


def _rehome(text: str, old: str, new: str) -> str:
    """JSON text with each string (a key too) under the old home moved to the new one: a
    literal prefix swap, nothing cleverer."""
    def swap(v):
        if isinstance(v, dict):
            return {swap(k): swap(x) for k, x in v.items()}
        if isinstance(v, list):
            return [swap(x) for x in v]
        return new + v[len(old):] if isinstance(v, str) and f"{v}/".startswith(f"{old}/") else v
    return json.dumps(swap(json.loads(text)), sort_keys=True, separators=(",", ":"))


def _move(src: Path, dst: Path, done: list, *, copy=False) -> None:
    """A move journaled in `done`, for _undo: a rename, which fails whole (a mount point,
    another disk) rather than copy, unless asked to."""
    (shutil.move if copy else os.rename)(src, dst)
    done.append((src, dst))


def _undo(done: list) -> None:
    """Each move journaled, back, last first."""
    for src, dst in reversed(done):
        shutil.move(dst, src)


def _keep_login(src: Path, dst: Path) -> None:
    """Staged agent dir `src` takes this host's sign-in, and who it is, from `dst` when the
    archive brought no sign-in: before any swap, while a link to it still reads."""
    if src.is_dir() and any((dst / n).is_file() and not os.path.lexists(src / n) for n in LOGINS):
        for name in IDENTITY:
            if (dst / name).is_file():  # a fresh file: no hard link from the archive shares it
                (src / name).unlink(missing_ok=True)
                shutil.copy2(dst / name, src / name)
                (src / name).chmod(0o600)  # its dir here may be open where this one wasn't


def _swap(src: Path, dst: Path, stamp: str, done: list, sidecars=("",), keep=()) -> None:
    """Move what is at dst aside (a DB with its WAL, named as SQLite looks for it), then src
    into place. A dir keeps each place of `keep` nested in it that the archive
    doesn't hold there: another part's, which its own swap replaces if it came. Each move
    is journaled in `done`. Only staging's may cross disks: what is kept goes after it, a
    rename beside what it was in."""
    old = dst.with_name(f"{dst.name}.pre-import-{stamp}")
    for suffix in sidecars:
        if os.path.lexists(here := dst.with_name(dst.name + suffix)):
            _move(here, old.with_name(old.name + suffix), done)
    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        _move(src, dst, done, copy=True)
    except BaseException:  # a copy from another disk cut short: what was here is aside
        if os.path.lexists(dst):
            shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink()
        raise
    for rel in (p.relative_to(dst) for p in keep if dst in p.parents):
        if dst.is_dir() and os.path.lexists(old / rel) and not os.path.lexists(dst / rel):
            (dst / rel).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _move(old / rel, dst / rel, done)


def _displaces(child: Path, dst: Path, name: str, at: Path) -> bool:
    """Whether staged `child`, swapped in at `dst`, would replace place `at`, not its own:
    holds something there, or a file or link on the way to it, here yet or not. A swap's
    root is its own place, but a data part owns none."""
    if not at.is_relative_to(dst) or (at == dst and not name.startswith("data/")):
        return False
    spot = child / at.relative_to(dst)
    return any(os.path.lexists(q) and (q == spot or q.is_symlink() or not q.is_dir())
               for q in (spot, *spot.parents) if q.is_relative_to(child))


def restore(src) -> list[str]:
    """Import an archive from the stream `src`. Returns the report's lines."""
    if _running():
        raise SystemExit("tmux-rc is running here: `systemctl --user stop tmux-rc`, then import.")
    if agents := _agents_running():
        sys.stderr.write(f"Warning: {', '.join(agents)} running here. Quit every agent session "
                         "first: they rewrite ~/.claude.json and their settings over this.\n")
    home, host, where = str(Path.home()), socket.gethostname(), places()
    # Staged under HOME, so each swap is a rename on the same disk, not a 10 GB copy.
    with tempfile.TemporaryDirectory(dir=home, prefix=".tmux-rc-import-") as tmp, \
            tarfile.open(fileobj=src, mode="r|gz") as tar:
        first = tar.next()
        ok = first and first.name == "manifest.json"
        manifest = json.load(tar.extractfile(first)) if ok else {}
        if manifest.get("format") != FORMAT or (manifest["schema"] or 0) > len(history.MIGRATIONS):
            raise SystemExit("This archive is from a newer tmux-rc, or isn't an export.")
        old = manifest["home"]

        def admit(m, path):  # nothing outside staging; links only where nothing writes
            if m.issym() and m.linkname.startswith(old + "/"):  # an account's shared files
                m = m.replace(linkname=home + m.linkname[len(old):], deep=False)
            # tar's filter strips a leading /, so judge the name after it; data's keeps a
            # hard link's target in staging too.
            m = (tarfile.data_filter if m.islnk() else tarfile.tar_filter)(m, path)
            # Where it lands, through any link extracted before it: an alias of a root is one.
            parent = os.path.realpath(os.path.join(path, os.path.dirname(m.name)))
            parts = Path(parent, os.path.basename(m.name)).relative_to(os.path.realpath(path)).parts
            root = len(parts) < (3 if parts[0] in ("data", "agents") else 2)  # what is swapped
            linked = not (m.isfile() or m.isdir())
            return None if linked and (root or parts[-1] in IDENTITY) else m
        tar.extractall(tmp, (m for m in tar if m is not first), filter=admit)
        stage = Path(tmp)
        if not (stage / DONE).is_file():
            raise SystemExit("This archive ends early, its export cut short: nothing imported.")
        if (stage / "history.sqlite3").exists():  # an import is consent to adopt the DB
            with contextlib.closing(sqlite3.connect(stage / "history.sqlite3")) as db, db:
                history.MIGRATIONS[0](db)  # one from before versioning may lack the metadata
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('host', ?)", (host,))
                tables = {t for (t,) in db.execute("SELECT name FROM sqlite_master")}
                if "plan_usage" in tables:  # Codex's accounts are its config dirs' paths
                    db.execute("UPDATE OR IGNORE plan_usage SET account=?||substr(account, ?) "
                               "WHERE substr(account, 1, ?)=?",
                               (home, len(old) + 1, len(old) + 1, old + "/"))
                if "layout_snapshots" in tables:  # schema 10+
                    db.executemany("UPDATE layout_snapshots SET payload=? WHERE server=?", [
                        (_rehome(p, old, home), s) for s, p in db.execute(
                            "SELECT server, payload FROM layout_snapshots")])
        if (store := stage / "accounts.json").exists():
            store.write_text(_rehome(store.read_text(), old, home))
        env = dotenv_values(stage / ".env") if (stage / ".env").is_file() else {}
        paths = [k for k in ("TMUXRC_HISTORY_DB", "TMUXRC_SCRATCH_DIR") if env.get(k)]
        # Unique per import: a second one within the second keeps the first's backups.
        stamp, done = f"{time.strftime('%Y%m%d-%H%M%S')}-{Path(tmp).name[-8:]}", []
        known = set(accounts.load())  # this host's accounts: never the archive's word for it
        for d in sorted(stage.glob("agents/*")):  # before any swap: HOME's other files aren't ours
            if not ((d.is_file() and d.name == ".claude.json") or (d.is_dir() and (
                    str(Path(home, d.name)) in known
                    or any(fnmatch.fnmatch(d.name, p) for p in AGENT_DIRS)))):
                raise SystemExit(f"This archive would replace {home}/{d.name}, no agent's dir "
                                 "here: add it as an account first.")
        db = where["history.sqlite3"]
        keep = sorted({q for p in (*where.values(), *_agent_dirs(), *(
            b.with_name(b.name + s) for b in (db, db.resolve()) for s in ("-wal", "-shm")))
            for q in (Path(os.path.abspath(p)), _spot(Path(p)), Path(p).resolve())},
                  key=lambda p: len(p.parts))
        # as named, through linked dirs, and itself resolved: a place nests in a swap however
        # it is reached; outermost first: a dir kept carries what is kept inside it
        if not (stage / "agents/.claude/.credentials.json").exists():  # who ~/.claude's login is
            (stage / "agents/.claude.json").unlink(missing_ok=True)  # stays with that login
        moves = []  # (staged, destination as named, archive name)
        for item in sorted(stage.iterdir()):
            parent = {"data": _data(), "agents": Path(home)}.get(item.name)
            moves += [(child, Path(os.path.abspath(parent / child.name)),
                       f"{item.name}/{child.name}") for child in sorted(item.iterdir())
                      ] if parent else [(item, Path(os.path.abspath(where[item.name])),
                                         item.name)] if item.name in where else []
        if len({_spot(path) for _, path, _ in moves}) < len(moves):  # e.g. data/scratch and scratch
            raise SystemExit("Two of this archive's parts would land at one place here.")
        for child, path, name in moves:  # before any swap: only a place's own part replaces it,
            dst = _spot(path)
            if name.startswith("data/") and (inside := _within(dst, keep)):  # nor lands inside one
                raise SystemExit(f"This archive's {name} would land in {inside.pop()}.")
            for at in keep:  # and one nested in another's swap must have a dir path to stay in
                if _displaces(child, dst, name, at):
                    raise SystemExit(f"This archive's {name} would replace {at}, not its own.")
        for child, path, _ in moves:
            _keep_login(child, _spot(path))
        journal = []
        try:
            for child, path, name in moves:  # where it lands now: a swap before it may
                _swap(child, _spot(path), stamp, journal,  # have replaced a link on its way
                      ("", "-wal", "-shm") if name == "history.sqlite3" else ("",), keep)
                done.append(name)
        except BaseException:
            sys.stderr.write(f"Import failed after {', '.join(done) or 'no part'}: putting back "
                             f"what it moved aside as *.pre-import-{stamp}.\n")
            _undo(journal)
            sys.stderr.write("Put back: nothing imported.\n")
            raise
    return [f"Restored: {', '.join(done)}.",
            *[f"Adopted the DB from {manifest['host']}."] * (manifest["host"] != host),
            *[(f"Rewrote paths under {old} to {home}. Agents' transcripts still name {old}: "
               "they resume in place only under that same path.")] * (old != home),
            f"What was replaced is kept as *.pre-import-{stamp}.",
            *[(f"The imported .env sets {', '.join(paths)}: check it names where this import "
               f"put {where['history.sqlite3']} and {where['scratch']}.")] * bool(paths),
            *[f"No credentials came: sign in again, or copy {', '.join(SECRETS)} by hand."
              ] * ("secrets" not in manifest["parts"]),
            *["No agent dirs came: copy ~/.claude* and ~/.codex* to resume their sessions."
              ] * ("agents" not in manifest["parts"]),
            *[f"Not in the archive, copy by hand: {', '.join(manifest['left'])}."
              ] * bool(manifest["left"]),
            "Next: `make install-units` (and `make docs`) in the checkout."]


def _open(name: str, mode: str):
    if name == "-":
        return contextlib.nullcontext((sys.stdin if "r" in mode else sys.stdout).buffer)
    f = open(name, mode)  # noqa: SIM115 - the caller's with closes it
    if "w" in mode:
        os.fchmod(f.fileno(), 0o600)  # private data inside, even over an existing file
    return f


def cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="tmux-rc")
    sub = parser.add_subparsers(dest="cmd", required=True)
    out = sub.add_parser("export", help="write this instance to a .tgz")
    flag = {part: f"{'no' if on else 'with'}_{part}" for part, (*_, on) in PARTS.items()
            if on is not None}  # an optional part: on by default is --no-<part>, else --with-
    for part, dest in flag.items():
        label, note, on = PARTS[part]
        out.add_argument(f"--{dest.replace('_', '-')}", action="store_true",
                         help=f"{'leave out' if on else 'add'} {label.lower()}: {note}")
    out.add_argument("-o", "--output", default=filename(), help="file, or - for stdout")
    sub.add_parser("import", help=("restore an export here. Import only an archive you made: "
                                   "its hooks and settings run code")).add_argument(
                                       "file", help="file, or -")
    args = parser.parse_args(argv)
    if args.cmd == "import":
        with _open(args.file, "rb") as f:
            sys.stdout.write("\n".join(restore(f)) + "\n")
        return
    with _open(args.output, "wb") as f:
        left = export(f, [p for p, dest in flag.items() if PARTS[p][2] != getattr(args, dest)])
    withheld = f" Move by hand: {', '.join(left)}." if left else ""
    sys.stderr.write(f"Wrote {args.output}. Left out, rebuilt: docs, agent caches "
                     f"({', '.join(CACHES)}).{withheld}\n")
