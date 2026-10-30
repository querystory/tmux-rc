"""The scratch gallery's sidecars, made in the background: a thumbnail (thumb.webp) for
a folder whose page has none or changed since, and a one-sentence description in its
meta.json when the agent wrote none (docs/design/scratch-previews.md).

One worker, one folder at a time, so a first listing over dozens of folders costs no
more than the slowest of them at once, and the listing never waits on it. Each folder
is tried once per version of its files: a page that cannot be shot or described is not
retried until it changes, unless the model was out (llm's shared backoff)."""

import base64
import io
import itertools
import json
import logging
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import contextmanager, suppress
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import urlopen

from PIL import Image
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from . import llm, scratch, scratch_markdown

logger = logging.getLogger(__name__)

VIEWPORT, THUMB = (1280, 800), (480, 300)
MAX_HEIGHT = 6000  # CSS px of a whole page drawn for a message; the rest is cut
MAX_PIXELS = 8000  # and its image's height, shrunk to fit: the most a model API takes
_SHOOTABLE = {".html", ".htm", ".md", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
_lock = threading.Lock()
_VIEW = re.compile(r"(?:[\w-][\w.-]*/)*(?:[\w-][\w.-]*)?(?:[?#].*)?")  # a page in a folder
_pending: list[tuple[Path, str, dict]] = []
_tried: set[tuple] = set()  # folder versions queued
_done: set[tuple] = set()  # and worked
_moves: dict[str, int] = defaultdict(int)  # times each name was archived or restored (forget)
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


def forget(names: list[str]) -> None:
    """These folders were archived or restored (scratch.act): whatever was worked out for them
    is asked afresh, and a job of theirs already running counts for nothing. A move keeps a
    folder's inode and times, so its version alone would call it done."""
    with _lock:
        for name in names:
            _moves[name] += 1
        for seen in (_tried, _done):
            seen.difference_update({key for key in seen if key[0] in names})


def _work(root: Path, web: Path) -> None:
    global _worker  # noqa: PLW0603
    with serving(root, web) as base:
        while True:
            with _lock:
                if not _pending:
                    _worker = None  # under the lock, so queue() starts one for what comes next
                    return
                item = _pending.pop(0)
                moves = _moves[item["name"]]
            for step in (_thumbnail, _description):
                try:  # each on its own: one failing must stop neither the other nor the queue
                    step(root, f"{base}/{item['href']}", item)
                except Exception:
                    logger.warning("Scratch %s for %s failed", step.__name__, item["name"],
                                   exc_info=True)
            with _lock:  # a folder archived or restored meanwhile was not worked: ask again
                moved = _moves[item["name"]] != moves
                (_tried.discard if moved else _done.add)(_version(item))


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
        if not path.startswith("/scratch/") or any(  # nor anything hidden, as the daemon
                part.startswith(".") for part in Path(unquote(urlsplit(path).path)).parts):
            return "/nonexistent"
        full = Path(super().translate_path(path.removeprefix("/scratch")))
        served = [full, full / "index.html", full / "index.htm"] if full.is_dir() else [full]
        # The daemon's order: index.md next, once the folder's slash is there (a slashless
        # folder is redirected first, as the daemon does, so its relative links resolve in it).
        if full.is_dir() and urlsplit(path).path.endswith("/") and not served[1].exists():
            served.append(full := full / "index.md")
        elif full.name == "index.html" and not full.exists():  # as the daemon: its index.md
            served.append(full := full.with_suffix(".md"))
        return str(full) if all(scratch.inside(p, Path(self.directory)) for p in served
                                if p.exists() or p.is_symlink()) else "/nonexistent"

    def send_head(self):
        page = Path(self.translate_path(self.path))
        raw = "raw" in parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if page.suffix != ".md" or not page.is_file() or raw:  # ?raw: the file, as the daemon
            return super().send_head()
        body = scratch_markdown.page(page, self.web).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def guess_type(self, path):
        return "text/plain; charset=utf-8" if path.endswith(".md") else super().guess_type(path)

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
        shoot(url, root / item["name"] / "thumb.webp", root, item["updated"], item["inode"])


def _description(root: Path, _url: str, item: dict) -> None:
    folder = root / item["name"]
    if item["describe"] and not item["description"] and not describe(
            folder / "meta.json", item["page"], item["title"], root,
            item["inode"]) and llm.backing_off():
        with _lock:  # the model is out, not the page at fault: ask again later
            _tried.discard(_version(item))


def _isolated(exe: str, profile: str, url: str) -> list[str]:
    """The headless shell in a fresh, throwaway profile. Every request but `url`'s host
    (serving's) goes to a proxy that isn't there, loopback included: a page could otherwise
    frame a local or private-network service, which the shot would then publish."""
    return [exe, "--headless", "--no-first-run", "--hide-scrollbars", f"--user-data-dir={profile}",
            "--proxy-server=http://127.0.0.1:9",
            f"--proxy-bypass-list=<-loopback>;{urlsplit(url).netloc}"]


def _served(url: str, timeout: float = 10) -> bool:
    """Served, or the shot would be of the browser's error page (a 404 raises too)."""
    try:
        urlopen(url, timeout=timeout).close()
    except OSError:
        return False
    return True


def render(url: str) -> bytes | None:
    """`url` as a PNG at VIEWPORT; None when it cannot be drawn."""
    if not (exe := browser()) or not _served(url):
        return None
    with tempfile.TemporaryDirectory(prefix="tmux-rc-thumb-") as profile:
        png = Path(profile) / "shot.png"
        try:
            subprocess.run([*_isolated(exe, profile, url),
                            "--window-size={},{}".format(*VIEWPORT),
                            "--virtual-time-budget=3000", f"--screenshot={png}", url],
                           capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return png.read_bytes() if png.is_file() else None


def shoot(url: str, out: Path, root: Path, at: float | None = None, inode: int = 0) -> bool:
    """Render `url` at VIEWPORT and save it as a THUMB webp dated `at` (the page's version it
    was queued for), so an edit made while it rendered still reads as newer. With `inode`,
    only while its folder is still that one: not into another that took its name meanwhile."""
    if not (data := render(url)):
        return False
    with Image.open(io.BytesIO(data)) as im:
        im.thumbnail(THUMB)
        try:
            if inode and out.parent.stat().st_ino != inode:
                return False
        except OSError:  # archived meanwhile
            return False
        scratch.replace(out, root, lambda tmp: im.save(tmp, "WEBP", quality=70))
    if at:
        os.utime(out, (at, at))
    return True


def whole(url: str, size: tuple[int, int], scale: int, *, dark: bool,
          timeout: float = 20) -> tuple[bytes, int, int] | None:
    """`url` as a PNG of the whole page, `size[0]` CSS px wide (laid out in a `size` window,
    at `scale` device pixels per px and in `dark` or light), cut at MAX_HEIGHT: the PNG,
    the height drawn and the page's own. The shell's --screenshot draws only its window, so
    this drives it over the DevTools protocol; None when it cannot be drawn in `timeout`s."""
    deadline = time.monotonic() + timeout
    if not (exe := browser()) or not _served(url, timeout / 4):
        return None
    # The shell's helpers may still be writing the profile when it is removed: no error then.
    with tempfile.TemporaryDirectory(prefix="tmux-rc-shot-", ignore_cleanup_errors=True) as profile:
        proc = None
        try:
            proc = subprocess.Popen([*_isolated(exe, profile, url), "--remote-debugging-port=0",
                                     "about:blank"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            port = Path(profile) / "DevToolsActivePort"  # the shell writes it once listening
            while not port.is_file() or len(port.read_text().split()) < 2:
                if time.monotonic() > deadline or proc.poll() is not None:  # or it died
                    return None
                time.sleep(0.05)
            number, path = port.read_text().split()[:2]
            with connect(f"ws://127.0.0.1:{number}{path}", max_size=None) as ws:
                ids = itertools.count(1)

                def call(method, session=None, **params):
                    ws.send(json.dumps({"id": (n := next(ids)), "method": method, "params": params,
                                        **({"sessionId": session} if session else {})}))
                    while (reply := json.loads(ws.recv(max(0.1, deadline - time.monotonic())))
                           ).get("id") != n:
                        pass  # an event
                    return reply["result"]  # none for an error: KeyError

                target = call("Target.createTarget", url="about:blank")["targetId"]
                tab = call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
                call("Emulation.setDeviceMetricsOverride", tab, width=size[0], height=size[1],
                     deviceScaleFactor=scale, mobile=False)  # laid out as in a frame: no 980px page
                call("Emulation.setEmulatedMedia", tab, features=[
                    {"name": "prefers-color-scheme", "value": "dark" if dark else "light"}])
                call("Page.navigate", tab, url=url)
                while not call("Runtime.evaluate", tab, returnByValue=True, expression=(
                        # off the blank page it started on, wherever a redirect took it
                        "location.href !== 'about:blank' && document.readyState === 'complete'"
                        ))["result"].get("value"):
                    if time.monotonic() > deadline:
                        return None
                    time.sleep(0.1)
                time.sleep(0.5)  # what the page draws once loaded (a chart, a fetch-free table)
                height = math.ceil(call("Page.getLayoutMetrics", tab)["cssContentSize"]["height"])
                drawn = min(height, MAX_HEIGHT)
                png = call("Page.captureScreenshot", tab, format="png", captureBeyondViewport=True,
                           clip={"x": 0, "y": 0, "width": size[0], "height": drawn,
                                 "scale": min(1, MAX_PIXELS / (drawn * scale))})
                return base64.b64decode(png["data"]), drawn, height
        except (OSError, TimeoutError, WebSocketException, KeyError, ValueError):
            return None
        finally:  # the shell and its helpers, which share its new session
            if proc:
                with suppress(ProcessLookupError):  # all gone already
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()


def capture(root: Path, web: Path, name: str, view: str, size: tuple[int, int], scale: int, *,
            dark: bool) -> tuple[bytes, int, int] | None:
    """Folder `name`'s page as the scratch viewer showed it: `view` (a path, query and hash
    in the folder, else its own page), at its width, pixel ratio and theme, whole (whole()).
    For a message to its agent, served as the thumbnails are, scratch and nothing else."""
    # Plain names only: nothing encoded, hidden or climbing, so nothing a server or the
    # browser could resolve to another folder or a file the viewer would not show.
    if not (item := scratch.named(root, name)) or not _VIEW.fullmatch(view):
        return None
    with serving(root, web) as base:
        url = f"{base}/{quote(name)}/{view}" if view else f"{base}/{item['href']}"
        return whole(url, size, scale, dark=dark)  # inside a send's 45s


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


def describe(meta: Path, page: Path, title: str, root: Path, inode: int = 0) -> str | None:
    """Ask the parse model for one sentence on the page and add it to meta.json, marked as
    generated. Never over a description already there, nor into a meta.json that is
    unreadable or a symlink: those are the agent's to fix. With `inode`, only while its
    folder is still that one (scratch.annotate)."""
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
    if not (sentence and scratch.annotate(meta, root, "description", sentence, inode)):
        return None
    return sentence
