"""Export all: one .tgz holding what another machine needs to take this instance over, and
its import. `tmux-rc export` / `tmux-rc import`, and Settings' Export all (tmux-rc's own
state only: the agents' dirs are ~10 GB, more than a tunnel carries).

Secrets travel only with --with-secrets, meant for `tmux-rc export --with-secrets | ssh
host tmux-rc import -`, so the bundle is never a file at rest. Import replaces rather than
merges: what it replaces is moved aside as <name>.pre-import-<time>."""
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

from . import accounts, history, push

FORMAT = 1
SECRETS = ("push.json", "config", ".env")  # the VAPID key and push endpoints; credentials
LOGINS = (".credentials.json", "auth.json")  # an agent dir's sign-in: Claude's, Codex's
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


def _members(scratch: bool, secrets: bool, agents: bool) -> list[tuple[str, Path]]:
    """Everything but the DB, which is snapshotted. docs/ is left out: `make docs` builds it."""
    where, home = places(), Path.home()
    out = [(n, where[n]) for n in ["accounts.json", *["scratch"] * scratch, *SECRETS * secrets]]
    out += [(f"data/{c.name}", c) for c in sorted(_data().glob("*"))
            if c.name != "docs" and c != where["scratch"] and ".pre-import-" not in c.name]
    dirs = {Path(d) for d in [*accounts.in_home(), *accounts.load()]} if agents else ()
    out += [(f"agents/{d.name}", d) for d in sorted(dirs) if d.parent == home and d.is_dir()]
    out += [("agents/.claude.json", home / ".claude.json")] * (agents and secrets)  # ~/.claude's
    return [(n, p) for n, p in out if p.exists()], [
        str(d) for d in sorted(dirs) if d.parent != home and d.is_dir()]


def export(out, *, scratch=True, secrets=False, agents=True) -> list[str]:
    """Stream the archive into `out`, which need not seek. Returns what was left out that a
    person moves by hand: secrets, and agent dirs deeper than HOME's top level."""
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
                               "scratch": scratch, "secrets": secrets, "agents": agents}).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mtime, info.mode = len(manifest), time.time(), 0o600
        tar.addfile(info, io.BytesIO(manifest))
        if schema is not None:
            tar.add(snapshot, snapshot.name)
        members, deeper = _members(scratch, secrets, agents)
        for name, path in members:
            def keep(m, name=name):  # an agent dir without its caches, nor its login unless asked
                rest = m.name[len(name) + 1:] if name.startswith("agents/") else ""
                skip = rest.split("/")[0] in CACHES or (not secrets and rest in LOGINS)
                return None if skip else m
            tar.add(path, name, filter=keep)
    return deeper + ([] if secrets else [n for n in SECRETS if places()[n].exists()] + (
        ["the agents' sign-ins (" + ", ".join(LOGINS) + ", ~/.claude.json)"] if agents else []))


def _running() -> bool:
    from .server import _bind  # noqa: PLC0415 - the daemon's own address, only here
    with contextlib.suppress(OSError), socket.create_connection(_bind(), timeout=1):
        return True
    return False


def _rehome(text: str, old: str, new: str) -> str:
    """A literal swap of the home prefix in JSON text: nothing cleverer."""
    return text.replace(f'"{old}/', f'"{new}/').replace(f'"{old}"', f'"{new}"')


def _swap(src: Path, dst: Path, stamp: str) -> None:
    """Move what is at dst aside (with a DB's WAL), then src into place. A dir keeps this
    host's agent sign-in when the archive brought none."""
    for suffix in ("", "-wal", "-shm"):
        here = dst.with_name(dst.name + suffix)
        if here.exists() or here.is_symlink():
            here.rename(here.with_name(f"{here.name}.pre-import-{stamp}"))
    old = dst.with_name(f"{dst.name}.pre-import-{stamp}")
    for login in LOGINS:
        if (old / login).is_file() and src.is_dir() and not (src / login).exists():
            shutil.copy2(old / login, src / login)
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

        def admit(m, path):  # nothing outside staging; a top-level name is a file or a dir
            if m.issym() and m.linkname.startswith(old + "/"):  # an account's shared files
                m = m.replace(linkname=home + m.linkname[len(old):], deep=False)
            keep = "/" in os.path.normpath(m.name) or m.isfile() or m.isdir()
            return tarfile.tar_filter(m, path) if keep else None
        tar.extractall(tmp, (m for m in tar if m is not first), filter=admit)
        stage = Path(tmp)
        if (stage / "history.sqlite3").exists():  # an import is consent to adopt the DB
            with contextlib.closing(sqlite3.connect(stage / "history.sqlite3")) as db, db:
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('host', ?)", (host,))
                layout = "SELECT 1 FROM sqlite_master WHERE name='layout_snapshots'"  # schema 10+
                if db.execute(layout).fetchone():
                    db.executemany("UPDATE layout_snapshots SET payload=? WHERE server=?", [
                        (_rehome(p, old, home), s) for s, p in db.execute(
                            "SELECT server, payload FROM layout_snapshots")])
        if (store := stage / "accounts.json").exists():
            store.write_text(_rehome(store.read_text(), old, home))
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
            *[f"No secrets came: copy {', '.join(SECRETS)} by hand, or sign in again."
              ] * (not manifest["secrets"]),
            *["No agent dirs came: copy ~/.claude* and ~/.codex* to resume their sessions."
              ] * (not manifest["agents"]),
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
    out.add_argument("--no-scratch", action="store_true", help="leave out the scratch pages")
    out.add_argument("--no-agents", action="store_true", help="leave out ~/.claude*, ~/.codex*")
    out.add_argument("--with-secrets", action="store_true",
                     help="add credentials: pipe it over ssh, never keep it as a file")
    out.add_argument("-o", "--output", default=filename(), help="file, or - for stdout")
    sub.add_parser("import", help="restore an export here").add_argument("file", help="file, or -")
    args = parser.parse_args(argv)
    if args.cmd == "import":
        with _open(args.file, "rb") as f:
            sys.stdout.write("\n".join(restore(f)) + "\n")
        return
    with _open(args.output, "wb") as f:
        left = export(f, scratch=not args.no_scratch, secrets=args.with_secrets,
                      agents=not args.no_agents)
    withheld = f" Move by hand: {', '.join(left)}." if left else ""
    sys.stderr.write(f"Wrote {args.output}. Left out, rebuilt: docs, agent caches "
                     f"({', '.join(CACHES)}).{withheld}\n")
