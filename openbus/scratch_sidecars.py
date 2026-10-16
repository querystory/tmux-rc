"""The scratch gallery's sidecars, made in the background: a thumbnail (thumb.webp) for
a folder whose page has none or changed since, and a one-sentence description in its
meta.json when the agent wrote none (docs/design/scratch-previews.md).

One worker, one folder at a time, so a first listing over dozens of folders costs no
more than the slowest of them at once, and the listing never waits on it. Each folder
is tried once per version of its files: a page that cannot be shot or described is not
retried until it changes, unless the model was out (llm's shared backoff)."""

import json
import logging
import os
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from PIL import Image

from . import llm, scratch

logger = logging.getLogger(__name__)

VIEWPORT, THUMB = (1280, 800), (480, 300)
_SHOOTABLE = {".html", ".htm", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
_lock = threading.Lock()
_pending: list[tuple[Path, str, dict]] = []
_tried: set[tuple[str, float]] = set()
_worker: threading.Thread | None = None


def browser() -> str | None:
    """Playwright's bundled headless Chromium ($TMUXRC_THUMB_BROWSER overrides), the newest
    installed; never the user's own browser, whose profile it would share."""
    override = os.environ.get("TMUXRC_THUMB_BROWSER")
    if override:
        return override if os.access(override, os.X_OK) else None
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache/ms-playwright")
    found = sorted(root.glob("chromium_headless_shell-*/*/chrome-headless-shell"))
    return str(found[-1]) if found else None


def stale(item: dict) -> bool:
    """Whether the folder's thumbnail is missing or older than its newest file."""
    shot = item["thumb"]
    return item["page"].suffix in _SHOOTABLE and (not shot or shot < item["updated"])


def queue(root: Path, listed: list[dict], describe: bool = True) -> None:
    """Hand the listed folders that lack a fresh thumbnail or (with `describe`) a
    description to the worker."""
    global _worker  # noqa: PLW0603 - the one worker
    with _lock:
        for i in listed:
            if (i["name"], i["updated"]) not in _tried and (
                    stale(i) or (describe and not i["description"])):
                _tried.add((i["name"], i["updated"]))
                _pending.append(i | {"describe": describe})
        if _pending and _worker is None:
            _worker = threading.Thread(target=_work, args=(root,), daemon=True,
                                       name="scratch-sidecars")
            _worker.start()


def _work(root: Path) -> None:
    global _worker  # noqa: PLW0603
    with serving(root) as base:
        while True:
            with _lock:
                if not _pending:
                    _worker = None  # under the lock, so queue() starts one for what comes next
                    return
                item = _pending.pop(0)
            for step in (_thumbnail, _description):
                try:  # each on its own: one failing must stop neither the other nor the queue
                    step(root, f"{base}/{item['href']}", item)
                except Exception:
                    logger.warning("Scratch %s for %s failed", step.__name__, item["name"],
                                   exc_info=True)


class _Static(SimpleHTTPRequestHandler):
    """scratch's files and nothing else, under the sandbox the daemon serves them with."""

    def translate_path(self, path):
        """At the daemon's own /scratch/ paths, so a page's root-relative links resolve; a
        path or the index a folder would serve that leads out of scratch is not found."""
        if not path.startswith("/scratch/"):
            return "/nonexistent"
        full = Path(super().translate_path(path.removeprefix("/scratch")))
        served = [full, full / "index.html", full / "index.htm"] if full.is_dir() else [full]
        return str(full) if all(scratch.inside(p, Path(self.directory)) for p in served
                                if p.exists() or p.is_symlink()) else "/nonexistent"

    def end_headers(self):
        self.send_header("Content-Security-Policy", scratch.SANDBOX)
        super().end_headers()

    def log_message(self, *_):
        pass


class _Server(ThreadingHTTPServer):
    def handle_error(self, *_):
        pass  # a renderer that stops loading mid-file (its time budget is up) is no error


@contextmanager
def serving(root: Path):
    """A loopback listener for the renderer that serves scratch alone: through the daemon,
    a page could frame, or navigate itself to, the API, and the shot would publish it."""
    server = _Server(("127.0.0.1", 0), partial(_Static, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True, name="scratch-static").start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/scratch"
    finally:
        server.shutdown()
        server.server_close()


def _thumbnail(root: Path, url: str, item: dict) -> None:
    if stale(item):
        shoot(url, root / item["name"] / "thumb.webp", root, item["updated"])


def _description(root: Path, _url: str, item: dict) -> None:
    folder = root / item["name"]
    if item["describe"] and not item["description"] and not describe(
            folder / "meta.json", item["page"], item["title"], root) and llm.backing_off():
        with _lock:  # the model is out, not the page at fault: ask again later
            _tried.discard((item["name"], item["updated"]))


def _replace(path: Path, root: Path, write) -> None:
    """Write `path` whole or not at all, never through a symlink planted at its name, and
    only while its folder is still really in scratch: it may have been swapped for a
    symlink out since it was listed."""
    if not scratch.inside(path.parent, root):
        raise PermissionError(f"{path.parent} is no longer in scratch")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    write(tmp)
    tmp.replace(path)


def shoot(url: str, out: Path, root: Path, at: float | None = None) -> bool:
    """Render `url` at VIEWPORT in a fresh, throwaway profile, offline but for `url`'s own
    host, and save it as a THUMB webp dated `at` (the page's version it was queued for),
    so an edit made while it rendered still reads as newer."""
    exe = browser()
    if not exe:
        return False
    try:  # served, or the shot would be of the browser's error page (a 404 raises too)
        urlopen(url, timeout=10).close()
    except OSError:
        return False
    with tempfile.TemporaryDirectory(prefix="tmux-rc-thumb-") as profile:
        png = Path(profile) / "shot.png"
        # Every request but `url`'s host (serving's) goes to a proxy that isn't there,
        # loopback included: a page could otherwise frame a local or private-network
        # service, which the shot would then publish.
        subprocess.run([exe, "--headless", "--no-first-run", "--hide-scrollbars",
                        f"--user-data-dir={profile}", "--window-size={},{}".format(*VIEWPORT),
                        "--proxy-server=http://127.0.0.1:9",
                        f"--proxy-bypass-list=<-loopback>;{urlsplit(url).netloc}",
                        "--virtual-time-budget=3000", f"--screenshot={png}", url],
                       capture_output=True, timeout=60, check=False)
        if not png.is_file():
            return False
        with Image.open(png) as im:
            im.thumbnail(THUMB)
            _replace(out, root, lambda tmp: im.save(tmp, "WEBP", quality=70))
    if at:
        os.utime(out, (at, at))
    return True


class _Text(HTMLParser):
    """A page's visible text: everything outside script, style and template."""

    HIDDEN = ("script", "style", "template")

    def __init__(self):
        super().__init__()
        self.hidden, self.parts = 0, []

    def handle_starttag(self, tag, _attrs):
        self.hidden += tag in self.HIDDEN

    def handle_endtag(self, tag):
        self.hidden -= tag in self.HIDDEN and self.hidden > 0

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def describe(meta: Path, page: Path, title: str, root: Path) -> str | None:
    """Ask the parse model for one sentence on the page and add it to meta.json, marked as
    generated. Never over a description already there, nor into a meta.json that is
    unreadable or a symlink: those are the agent's to fix."""
    text = ""
    if not scratch.inside(page, root):
        return None
    if page.suffix in (".html", ".htm"):
        parser = _Text()
        with page.open(errors="replace") as f:
            parser.feed(f.read(524288))
        text = " ".join(" ".join(parser.parts).split())[:4000]
    sentence = llm.one_sentence(
        "Describe this web page in ONE short sentence (under 20 words) for a gallery of "
        f'previews: what it shows, starting with the subject (never "This page"). Plain '
        f"text.\nTitle: {title}\n\n{text}")
    if not sentence or meta.is_symlink():
        return None
    try:  # read again: the agent may have written one meanwhile
        current = json.loads(meta.read_text()) if meta.exists() else {}
    except (OSError, ValueError):
        return None
    if not isinstance(current, dict) or str(current.get("description") or "").strip():
        return None
    data = {**current, "description": sentence, "generated": ["description"]}
    _replace(meta, root, lambda tmp: tmp.write_text(json.dumps(data, indent=1) + "\n"))
    return sentence
