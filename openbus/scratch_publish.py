"""Publishing a scratch folder beyond this machine (docs/scratch.md): `tmux-rc scratch
publish <folder>` opts it in, writing its published URL into its meta.json as "publish",
and copies scratch.export's static version of it to the TMUXRC_SCRATCH_PUBLISH prefix,
served at TMUXRC_SCRATCH_PUBLISH_URL. The daemon copies it again each time its page is
ready (push.landed: when it sends Ready or Updated). Only what changed since the last copy
is sent, by a record of what was, so the writer needs no permission to list the bucket."""

import argparse
import hashlib
import logging
import mimetypes
import os
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

from . import accounts, scratch

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()  # one copy at a time: each reads and rewrites the record


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


def _record() -> Path:
    return accounts.store_path().with_name("published.json")  # {prefix/folder: {file: sha256}}


def url(name: str) -> str:
    return f"{_target()[1]}/{quote(name)}/"


def _sync(name: str, out: Path | None) -> None:
    """Make the copy at the target match `out` (None: nothing), sending only what changed."""
    target = _target()[0]
    with _LOCK:
        record = scratch.load(_record(), {})
        was = record.get(f"{target}/{name}", {})
        now = {f.relative_to(out).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
               for f in (out.rglob("*") if out else ()) if f.is_file()}
        driver = DRIVER(target)
        for rel in [r for r, digest in now.items() if was.get(r) != digest]:
            driver.put(f"{name}/{rel}", out / rel)
        for rel in was.keys() - now.keys():
            driver.drop(f"{name}/{rel}")
        record[f"{target}/{name}"] = now
        scratch.save(_record(), {k: v for k, v in record.items() if v})


def publish(root: Path, name: str, web: Path) -> str:
    """Copy the folder's current version to the target; its URL."""
    with tempfile.TemporaryDirectory() as tmp:
        scratch.export(root / name, Path(tmp) / name, web)
        _sync(name, Path(tmp) / name)
    return url(name)


def unpublish(name: str) -> None:
    _sync(name, None)


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


def ready(root: Path, item: dict, web: Path, tell) -> None:
    """A page landed (push.landed): copy it again if it is published and not being revised."""
    if item.get("publish") and not item["revising"]:
        later(lambda: publish(root, item["name"], web), item["name"], tell)


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
    if not scratch.folder_name(args.name) or (
            args.command == "publish" and not (root / args.name).is_dir()):
        cli.error(f"{args.name} is not a folder of {root}")
    meta, there = root / args.name / "meta.json", url(args.name)
    if args.command == "unpublish":
        unpublish(args.name)
        scratch.rewrite(meta, root, lambda m: {k: v for k, v in m.items() if k != "publish"})
        return sys.stdout.write(f"Unpublished {there}\n")
    if not scratch.rewrite(meta, root, lambda m: {**m, "publish": there}):
        cli.error(f"{meta} is unreadable")
    return sys.stdout.write(publish(root, args.name, web) + "\n")
