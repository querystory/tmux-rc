"""Export all: one .tgz holding what another machine needs to take this instance over, and
its import. `tmux-rc export` / `tmux-rc import`, and Settings' Export all.

PARTS is the one list of what an archive can hold: it names the CLI's flags, the web's
choices and the sizes shown beside them. Credentials are off unless asked for: the file
then holds every sign-in and key. Import replaces rather than merges: what it replaces is
moved aside as <name>.pre-import-<time>."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import socket
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from dotenv import dotenv_values

from . import accounts, history, push

FORMAT = 2
# Part -> (label, note, in by default; None: always in).
PARTS = {
    "state": ("tmux-rc history & settings",
              "Pane history, plan usage, the tmux layout, accounts and mocks", None),
    "scratch": ("Scratch pages", "The preview gallery", True),
    "agents": ("Agent conversations", "~/.claude*, ~/.codex*, so agents can resume", True),
    "secrets": ("Credentials", ("Sign-ins, ~/.claude.json, ~/.config/tmux-rc, .env, push keys. "
                                "The file then holds every sign-in and key: keep it private."),
                False),
}
SECRETS = ("push.json", "config", ".env")  # the VAPID key and push endpoints; credentials
LOGINS = (".credentials.json", "auth.json")  # an agent dir's sign-in: Claude's, Codex's
IDENTITY = (*LOGINS, ".claude.json")  # the sign-in and who it is: they stay together
# What an agent rebuilds in its dir, left out: caches, debug logs, shell snapshots.
CACHES = ("cache", "debug", "statsig", "shell-snapshots", "log", "tmp", ".tmp")


def _data() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "tmux-rc"


def places() -> dict[str, Path]:
    """Archive name -> where it lives on this host, by the same helpers the daemon uses."""
    scratch = os.environ.get("TMUXRC_SCRATCH_DIR")
    return {"history.sqlite3": history.default_path(), "accounts.json": accounts.store_path(),
            "push.json": push.default_path(), "config": Path.home() / ".config/tmux-rc",
            ".env": Path(__file__).resolve().parent.parent / ".env",
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
                if c.name != "docs" and ".pre-import-" not in c.name)
    yield from (("agents", f"agents/{d.name}", d) for d in _agent_dirs() if d.parent == home)
    yield "secrets", "agents/.claude.json", home / ".claude.json"  # the default ~/.claude's


def _entries():
    """(part, archive name, path) of every file, dir and link an archive can hold: an agent
    dir without its caches, its sign-in as a credential, and scratch only as scratch/."""
    pages = places()["scratch"].resolve()
    for part, name, where in _roots():
        root = where.resolve()  # a linked root: what it links to
        if not os.path.lexists(where) or (part != "scratch" and root == pages):
            continue
        yield part, name, root
        for here, dirs, files in os.walk(root):
            top, kept = here == str(root), []
            for child in sorted(dirs + files):
                path = Path(here, child)
                if (part == "agents" and top and child in CACHES) or (
                        part != "scratch" and path == pages):
                    continue
                kept.append(child)
                own = "secrets" if part == "agents" and top and child in IDENTITY else part
                yield own, f"{name}/{path.relative_to(root)}", path
            dirs[:] = [d for d in dirs if d in kept]


def sizes() -> dict[str, int]:
    """Each part's size on disk, uncompressed: the walk export makes, without reading."""
    out = dict.fromkeys(PARTS, 0)
    with contextlib.suppress(OSError):
        out["state"] = history.default_path().stat().st_size
    for part, _, path in _entries():
        with contextlib.suppress(OSError):
            out[part] += path.lstat().st_size if not path.is_dir() or path.is_symlink() else 0
    return out


def export(out, parts) -> list[str]:
    """Stream the archive of `parts` (state always) into `out`, which need not seek.
    Returns what a person moves by hand: agent dirs deeper than HOME's top level."""
    parts = {"state", *parts}
    db = history.default_path().resolve()  # as_uri needs it absolute
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=out, mode="w|gz") as tar:
        snapshot, schema = Path(tmp, "history.sqlite3"), None
        if db.exists():  # the backup API: a consistent copy while the daemon keeps writing
            with contextlib.closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as src, \
                    contextlib.closing(sqlite3.connect(snapshot)) as copy:
                src.backup(copy)
                schema = copy.execute("PRAGMA user_version").fetchone()[0]
        manifest = json.dumps({"format": FORMAT, "schema": schema, "host": socket.gethostname(),
                               "home": str(Path.home()), "created": time.time(),
                               "parts": sorted(parts)}).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mtime, info.mode = len(manifest), time.time(), 0o600
        tar.addfile(info, io.BytesIO(manifest))
        if schema is not None:
            tar.add(snapshot, snapshot.name)
        for part, name, path in _entries():
            if part in parts:
                with contextlib.suppress(FileNotFoundError):  # gone since the walk
                    tar.add(path, name, recursive=False)
    deeper = [str(d) for d in _agent_dirs() if d.parent != Path.home()]
    return deeper if "agents" in parts else []


def _running() -> bool:
    from .server import _bind  # noqa: PLC0415 - the daemon's own address, only here
    with contextlib.suppress(OSError), socket.create_connection(_bind(), timeout=1):
        return True
    return False


def _rehome(text: str, old: str, new: str) -> str:
    """A literal swap of the home prefix in JSON text: nothing cleverer."""
    return text.replace(f'"{old}/', f'"{new}/').replace(f'"{old}"', f'"{new}"')


def _swap(src: Path, dst: Path, stamp: str) -> None:
    """Move what is at dst aside (a DB with its WAL, named as SQLite looks for it), then src
    into place. An agent dir keeps this host's sign-in, and who it is, when the archive
    brought no sign-in."""
    old = dst.with_name(f"{dst.name}.pre-import-{stamp}")
    for suffix in ("", "-wal", "-shm"):
        if os.path.lexists(here := dst.with_name(dst.name + suffix)):
            here.rename(old.with_name(old.name + suffix))
    if src.is_dir() and any((old / n).is_file() and not os.path.lexists(src / n) for n in LOGINS):
        for name in IDENTITY:
            if (old / name).is_file():
                shutil.copy2(old / name, src / name)  # admit() let no link stand here
    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.move(src, dst)


def restore(src) -> list[str]:
    """Import an archive from the stream `src`. Returns the report's lines."""
    if _running():
        raise SystemExit("tmux-rc is running here: `systemctl --user stop tmux-rc`, then import.")
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
        if (stage / "history.sqlite3").exists():  # an import is consent to adopt the DB
            with contextlib.closing(sqlite3.connect(stage / "history.sqlite3")) as db, db:
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
        stamp, done = time.strftime("%Y%m%d-%H%M%S"), []
        for item in sorted(stage.iterdir()):
            parent = {"data": _data(), "agents": Path(home)}.get(item.name)
            for child in sorted(item.iterdir()) if parent else [item] * (item.name in where):
                _swap(child, parent / child.name if parent else where[item.name], stamp)
                done.append(f"{item.name}/{child.name}" if parent else item.name)
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
    sub.add_parser("import", help="restore an export here").add_argument("file", help="file, or -")
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
