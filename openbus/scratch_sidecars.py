"""The scratch gallery's sidecars, made in the background: a thumbnail (thumb.webp) for
a folder whose page has none or changed since, and a one-sentence description in its
meta.json when the agent wrote none (docs/design/scratch-previews.md).

One worker, one folder at a time, so a first listing over dozens of folders costs no
more than the slowest of them at once, and the listing never waits on it. Each folder
is tried once per version of its files: a page that cannot be shot or described is not
retried until it changes, unless the model was out (llm's shared backoff)."""

import io
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
from urllib.parse import quote, unquote, urlsplit
from urllib.request import urlopen

from PIL import Image

from . import llm, scratch, scratch_markdown

logger = logging.getLogger(__name__)

VIEWPORT, THUMB = (1280, 800), (480, 300)
_SHOOTABLE = {".html", ".htm", ".md", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
_lock = threading.Lock()
_pending: list[tuple[Path, str, dict]] = []
_tried: set[tuple] = set()  # folder versions queued
_done: set[tuple] = set()  # and worked
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


def _version(item: dict) -> tuple:
    """A folder's version: the folder itself (a replacement is new even with old mtimes)
    and its newest file."""
    return item["name"], item["inode"], item["updated"]


def stale(item: dict) -> bool:
    """Whether the folder's thumbnail is missing or older than its newest file."""
    shot = item["thumb"]
    return item["page"].suffix in _SHOOTABLE and (not shot or shot < item["updated"])


def queue(root: Path, web: Path, listed: list[dict], describe: bool = True) -> None:
    """Hand the listed folders that lack a fresh thumbnail or (with `describe`) a
    description to the worker; `web` is the app's static dir, which a Markdown page's
    styles and mermaid.js come from."""
    global _worker  # noqa: PLW0603 - the one worker
    can_shoot = browser() is not None
    with _lock:
        for i in listed:
            key = _version(i)
            if key not in _tried and (stale(i) or (describe and not i["description"])):
                _tried.add(key)
                _pending.append(i | {"describe": describe})
            # What a card without a fresh thumbnail shows meanwhile.
            i["shot"] = stale(i) and ("failed" if key in _done or not can_shoot else "pending")
        if _pending and _worker is None:
            _worker = threading.Thread(target=_work, args=(root, web), daemon=True,
                                       name="scratch-sidecars")
            _worker.start()


def _work(root: Path, web: Path) -> None:
    global _worker  # noqa: PLW0603
    with serving(root, web) as base:
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
            with _lock:
                _done.add(_version(item))


class _Static(SimpleHTTPRequestHandler):
    """scratch's files and nothing else, under the sandbox the daemon serves them with, a
    Markdown page rendered as the daemon renders it, with the mermaid.js it loads."""

    def __init__(self, *args, web: Path, **kwargs):
        self.web = web  # before handling, which the base class does on construction
        super().__init__(*args, **kwargs)

    def translate_path(self, path):
        """At the daemon's own /scratch/ paths, so a page's root-relative links resolve; a
        path or the index a folder would serve that leads out of scratch is not found."""
        if path.startswith(scratch_markdown.MERMAID):
            return str(self.web / scratch_markdown.MERMAID.removeprefix("/"))
        if not path.startswith("/scratch/"):
            return "/nonexistent"
        full = Path(super().translate_path(path.removeprefix("/scratch")))
        served = [full, full / "index.html", full / "index.htm"] if full.is_dir() else [full]
        if full.is_dir() and not served[1].exists():  # the daemon's order: index.md next
            served.append(full := full / "index.md")
        return str(full) if all(scratch.inside(p, Path(self.directory)) for p in served
                                if p.exists() or p.is_symlink()) else "/nonexistent"

    def send_head(self):
        page = Path(self.translate_path(self.path))
        if page.suffix != ".md" or not page.is_file():
            return super().send_head()
        body = scratch_markdown.page(page, self.web).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def end_headers(self):
        self.send_header("Content-Security-Policy", scratch.SANDBOX)
        super().end_headers()

    def log_message(self, *_):
        pass


class _Server(ThreadingHTTPServer):
    def handle_error(self, *_):
        pass  # a renderer that stops loading mid-file (its time budget is up) is no error


@contextmanager
def serving(root: Path, web: Path):
    """A loopback listener for the renderer that serves scratch alone: through the daemon,
    a page could frame, or navigate itself to, the API, and the shot would publish it."""
    server = _Server(("127.0.0.1", 0), partial(_Static, directory=str(root), web=web))
    threading.Thread(target=server.serve_forever, daemon=True, name="scratch-static").start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/scratch"
    finally:
        server.shutdown()
        server.server_close()


def _thumbnail(root: Path, url: str, item: dict) -> None:
    if stale(item) and _version(item) not in _done:  # once per version, even
        # when the description is asked again
        shoot(url, root / item["name"] / "thumb.webp", root, item["updated"])


def _description(root: Path, _url: str, item: dict) -> None:
    folder = root / item["name"]
    if item["describe"] and not item["description"] and not describe(
            folder / "meta.json", item["page"], item["title"], root) and llm.backing_off():
        with _lock:  # the model is out, not the page at fault: ask again later
            _tried.discard(_version(item))


def render(url: str, size: tuple[int, int] = VIEWPORT, dark: bool = False,
           timeout: float = 60) -> bytes | None:
    """`url` as a PNG at `size` (CSS px), drawn in a fresh, throwaway profile, offline but for
    `url`'s own host, within `timeout`s; None when it cannot be drawn."""
    exe = browser()
    if not exe:
        return None
    try:  # served, or the shot would be of the browser's error page (a 404 raises too)
        urlopen(url, timeout=min(10, timeout / 4)).close()
    except OSError:
        return None
    with tempfile.TemporaryDirectory(prefix="tmux-rc-thumb-") as profile:
        png = Path(profile) / "shot.png"
        # Every request but `url`'s host (serving's) goes to a proxy that isn't there,
        # loopback included: a page could otherwise frame a local or private-network
        # service, which the shot would then publish.
        try:
            subprocess.run([exe, "--headless", "--no-first-run", "--hide-scrollbars",
                            f"--user-data-dir={profile}", "--window-size={},{}".format(*size),
                            *(["--force-dark-mode"] if dark else []),
                            "--proxy-server=http://127.0.0.1:9",
                            f"--proxy-bypass-list=<-loopback>;{urlsplit(url).netloc}",
                            "--virtual-time-budget=3000", f"--screenshot={png}", url],
                           capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return png.read_bytes() if png.is_file() else None


def shoot(url: str, out: Path, root: Path, at: float | None = None) -> bool:
    """Render `url` at VIEWPORT and save it as a THUMB webp dated `at` (the page's version it
    was queued for), so an edit made while it rendered still reads as newer."""
    if not (data := render(url)):
        return False
    with Image.open(io.BytesIO(data)) as im:
        im.thumbnail(THUMB)
        scratch.replace(out, root, lambda tmp: im.save(tmp, "WEBP", quality=70))
    if at:
        os.utime(out, (at, at))
    return True


def capture(root: Path, web: Path, name: str, view: str, size: tuple[int, int], *,
            dark: bool) -> bytes | None:
    """Folder `name`'s page as the scratch viewer showed it: `view` (a path and query in the
    folder, else its own page), its size and theme, from the top. For a message to its
    agent, served as the thumbnails are, scratch and nothing else."""
    if not (item := scratch.named(root, name)) or ".." in unquote(urlsplit(view).path).split("/"):
        return None
    with serving(root, web) as base:
        url = f"{base}/{quote(name)}/{view.lstrip('/')}" if view else f"{base}/{item['href']}"
        return render(url, size, dark, 20)  # well inside a send's 45s


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
    if not scratch.inside(page, root):
        return None
    parser, html = _Text(), ""
    if page.suffix == ".md":  # what it renders to, not its markup
        html = scratch_markdown.render(page)[1]
    elif page.suffix in (".html", ".htm"):
        with page.open(errors="replace") as f:
            html = f.read(524288)
    parser.feed(html)
    text = " ".join(" ".join(parser.parts).split())[:4000]
    sentence = llm.one_sentence(
        "Describe this web page in ONE short sentence (under 20 words) for a gallery of "
        f'previews: what it shows, starting with the subject (never "This page"). Plain '
        f"text.\nTitle: {title}\n\n{text}")
    if not (sentence and scratch.annotate(meta, root, "description", sentence)):
        return None
    return sentence
