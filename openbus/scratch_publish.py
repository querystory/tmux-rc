"""Publishing a scratch folder beyond this machine (docs/scratch.md): `tmux-rc scratch
publish <folder>` opts it in, writing its published URL into its meta.json as "publish",
and copies scratch.export's static version of it to the TMUXRC_SCRATCH_PUBLISH prefix,
served at TMUXRC_SCRATCH_PUBLISH_URL. The daemon copies it again each time its page is
ready (push.landed: when it sends Ready or Updated). Only what changed since the last copy
is sent, by a record of what was, so the writer needs no permission to list the bucket.
An index.html at the prefix lists what is published, while anything is."""

import argparse
import fcntl
import hashlib
import logging
import mimetypes
import os
import sys
import tempfile
import threading
from html import escape
from pathlib import Path
from urllib.parse import quote

from . import accounts, scratch

logger = logging.getLogger(__name__)


class Gcs:
    """The driver: objects under a gs://bucket/prefix, written through the JSON API as the
    account in TMUXRC_SCRATCH_PUBLISH_KEY: one of its own, never the Vertex classifier's."""

    API = "https://storage.googleapis.com"

    def __init__(self, target: str):
        self.bucket, _, self.prefix = target.removeprefix("gs://").strip("/").partition("/")
        if not (key := os.environ.get("TMUXRC_SCRATCH_PUBLISH_KEY", "").strip()):
            raise SystemExit("set TMUXRC_SCRATCH_PUBLISH_KEY (the publisher's key file)")
        import google.auth  # noqa: PLC0415 - only a publish pays for it
        from google.auth.transport.requests import AuthorizedSession  # noqa: PLC0415
        self.http = AuthorizedSession(google.auth.load_credentials_from_file(
            key, scopes=["https://www.googleapis.com/auth/devstorage.read_write"])[0])

    def _name(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, path: Path) -> None:
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.http.post(f"{self.API}/upload/storage/v1/b/{self.bucket}/o", data=path.read_bytes(),
                       params={"uploadType": "media", "name": self._name(key)},
                       headers={"Content-Type": kind}, timeout=60).raise_for_status()

    def drop(self, key: str) -> None:
        r = self.http.delete(f"{self.API}/storage/v1/b/{self.bucket}/o/"
                             + quote(self._name(key), safe=""), timeout=60)
        if r.status_code != 404:  # already gone is gone
            r.raise_for_status()


DRIVER = Gcs


def _target() -> tuple[str, str]:
    target, base = (os.environ.get(v, "").strip() for v in
                    ("TMUXRC_SCRATCH_PUBLISH", "TMUXRC_SCRATCH_PUBLISH_URL"))
    if not (target.startswith("gs://") and base):
        raise SystemExit("set TMUXRC_SCRATCH_PUBLISH (gs://bucket/prefix) and "
                           "TMUXRC_SCRATCH_PUBLISH_URL (where that prefix is served)")
    return target.rstrip("/"), base.rstrip("/")


def configured() -> bool:
    try:
        return bool(_target())
    except SystemExit:
        return False


def _record() -> Path:
    return accounts.store_path().with_name("published.json")  # {prefix/folder: {file: sha256}}


def url(name: str) -> str:
    return f"{_target()[1]}/{quote(name)}/"


def _index(root: Path, names: list[str]) -> bytes:
    """The publish root's page: each published folder's title and description, from the
    folder (live or archived) opted in at its URL, else just its name."""
    def card(name: str) -> str:
        i = next((c for at in (root, scratch.archive(root)) if (c := scratch.named(at, name))
                  and c["publish"] == url(name)), {})
        return (f'<li><a href="{quote(name)}/">{escape(i.get("title", name))}</a>'
                f'<p>{escape(i.get("description", ""))}</p></li>')
    return ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-'
            'width, initial-scale=1"><title>Published</title><style>body{max-width:720px;'
            'margin:auto;padding:16px;font:16px/1.5 system-ui,sans-serif}li{margin-bottom:12px}'
            f'p{{margin:0;opacity:.7}}</style><h1>Published</h1><ul>{"".join(map(card, names))}'
            '</ul>').encode()


def _sync(root: Path, name: str, out: Path | None) -> None:
    """Make the copy at the target match `out` (None: nothing), sending only what changed,
    and the index what is then published."""
    target = _target()[0]
    _record().parent.mkdir(parents=True, exist_ok=True)
    # one copy at a time, the CLI's and the daemon's alike: each reads and rewrites the record
    with _record().with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        card = scratch.named(root, name)  # a job superseded (opted out, or in again) does nothing
        if not (_copyable(card) if out else not (card and card["publish"])):
            return
        record = scratch.load(_record(), {})
        was = record.get(f"{target}/{name}", {})
        now = {f.relative_to(out).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
               for f in (out.rglob("*") if out else ()) if f.is_file()}
        sent = [r for r, digest in now.items() if was.get(r) != digest]
        # whatever may be sent is on record first (as unknown), so a failure midway leaves
        # nothing hosted that unpublish cannot find
        scratch.save(_record(), {**record, f"{target}/{name}": {**was, **dict.fromkeys(sent, "")}})
        driver = DRIVER(target)
        for rel in sent:
            driver.put(f"{name}/{rel}", out / rel)
        for rel in was.keys() - now.keys():
            driver.drop(f"{name}/{rel}")
        record[f"{target}/{name}"] = now
        names = sorted(k.removeprefix(f"{target}/") for k, v in record.items()
                       if v and k.startswith(f"{target}/"))
        page = _index(root, names) if names else b""
        digest = hashlib.sha256(page).hexdigest() if page else None
        if not page or (record.get(target) or {}).get("index.html") != digest:
            with tempfile.TemporaryDirectory() as tmp:
                (index := Path(tmp) / "index.html").write_bytes(page)
                if page:
                    driver.put("index.html", index)
                else:
                    driver.drop("index.html")
            record[target] = {"index.html": digest} if page else {}
        scratch.save(_record(), {k: v for k, v in record.items() if v})


def _indexed(card: dict | None) -> bool:
    return bool(card) and not card["pending"] and card["href"].endswith("/")  # it has one


def _copyable(card: dict | None) -> bool:
    """Opted in, with its index, and not being revised."""
    return _indexed(card) and bool(card["publish"]) and not card["revising"]


def publish(root: Path, name: str, web: Path) -> str:
    """Copy the folder's current version to the target; its URL. Only while it is opted in
    and not being revised: a job queued before an unpublish or an update copies nothing."""
    if not _copyable(scratch.named(root, name)):
        raise SystemExit(f"{name} is not published, has no index, or is being updated: it "
                         "is copied once it is ready")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            scratch.export(root / name, Path(tmp) / name, web)
            _sync(root, name, Path(tmp) / name)
    except (Exception, SystemExit) as e:
        _failed(root, name, str(e)[:300])
        raise
    _failed(root, name, None)
    return url(name)


def _failed(root: Path, name: str, why: str | None) -> None:
    """Keep why the last copy failed in meta.json as "publish_failed", for its card."""
    scratch.rewrite(root / name / "meta.json", root, lambda m: None if m.get(
        "publish_failed") == why else {**_without(m, "publish_failed"),
                                       **({"publish_failed": why} if why else {})})


def _without(meta: dict, *fields: str) -> dict:
    return {k: v for k, v in meta.items() if k not in fields}


def unpublish(root: Path, name: str) -> None:
    _sync(root, name, None)


def opt(root: Path, name: str, on: bool) -> str:
    """Opt folder `name` in ("publish": its URL, in its meta.json) or out, archived or not;
    that URL. Only a folder with an index is opted in: it is served at name/."""
    there = url(name)
    if why := _refused(root, name, on):
        raise ValueError(why)
    if on and not scratch.rewrite(meta := root / name / "meta.json", root,
                                  lambda m: {**m, "publish": there}):
        raise ValueError(f"{meta} is unreadable")
    for at in () if on else (root, scratch.archive(root)):
        scratch.rewrite(at / name / "meta.json", at, lambda m: _without(
            m, "publish", "publish_failed") if {"publish", "publish_failed"} & m.keys() else None)
        if (scratch.named(at, name) or {}).get("publish"):
            raise ValueError(f"{at / name / 'meta.json'} could not be rewritten")
    return there


def _refused(root: Path, name: str, on: bool) -> str | None:
    if not scratch.folder_name(name) or (on and not _indexed(scratch.named(root, name))):
        return f"{name} is not a folder of {root} with an index.html or index.md"
    return None


def act(root: Path, action: str, names: list[str], web: Path, tell) -> list[str]:
    """The gallery's Publish or Unpublish (`action`): each folder opted in or out at once,
    its copy made or removed in the background; none of them if any is refused."""
    publishing = action == "publish"
    if whys := [w for n in names if (w := _refused(root, n, publishing))]:
        raise ValueError("; ".join(whys))
    for name in names:
        opt(root, name, publishing)
        later(lambda n=name: publish(root, n, web) if publishing else unpublish(root, n),
              name, tell)
    return names


def later(job, name: str, tell) -> None:
    """Run job(), which publishes or unpublishes folder `name`, off the caller's thread: an
    upload never holds up the daemon. A failure is logged and pushed (`tell`)."""
    def run() -> None:
        try:
            job()
        except (Exception, SystemExit) as e:  # a refusal (export's, _target's) is SystemExit
            logger.warning("scratch: publishing %s failed", name, exc_info=True)
            tell({"title": f"Publish failed: {name}"[:100], "body": str(e)[:300],
                  "tag": f"publish:{name}", "url": "/m#view=scratch", "actions": []})
    threading.Thread(target=run, daemon=True, name="scratch-publish").start()


def ready(root: Path, name: str, web: Path, tell) -> None:
    """A page landed (push.landed), or its thumbnail did: copy it again if it is published
    and not being revised."""
    if _copyable(scratch.named(root, name)):
        later(lambda: publish(root, name, web), name, tell)


def cli(argv: list[str], web: Path) -> object:
    """`tmux-rc scratch export|publish|unpublish`."""
    cli = argparse.ArgumentParser(prog="tmux-rc scratch")
    sub = cli.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="write a scratch folder as static HTML any file "
                            "host can serve")
    export.add_argument("folder", type=Path)
    export.add_argument("out", type=Path)
    for command, why in (("publish", "copy a scratch folder to TMUXRC_SCRATCH_PUBLISH"),
                         ("unpublish", "remove a scratch folder's published copy")):
        sub.add_parser(command, help=why).add_argument("name", help="the folder's name")
    args = cli.parse_args(argv)
    if args.command == "export":
        return scratch.export(args.folder, args.out, web)
    root = Path(scratch.configured() or cli.error("TMUXRC_SCRATCH_DIR is not a folder"))
    try:
        there = opt(root, args.name, args.command == "publish")
    except ValueError as e:
        cli.error(str(e))
    if args.command == "unpublish":
        unpublish(root, args.name)
        return sys.stdout.write(f"Unpublished {there}\n")
    return sys.stdout.write(publish(root, args.name, web) + "\n")
