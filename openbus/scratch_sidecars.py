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
from html.parser import HTMLParser
from pathlib import Path
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


def queue(root: Path, base: str, listed: list[dict], describe: bool = True) -> None:
    """Hand the listed folders that lack a fresh thumbnail or (with `describe`) a
    description to the worker; `base` is the daemon's own /scratch URL, so a page is shot
    under the same sandbox it is served with."""
    global _worker  # noqa: PLW0603 - the one worker
    with _lock:
        for i in listed:
            if (i["name"], i["updated"]) not in _tried and (
                    stale(i) or (describe and not i["description"])):
                _tried.add((i["name"], i["updated"]))
                url = f"{base}/{i['href']}"
                _pending.append((root, url, i | {"describe": describe}))
        if _pending and _worker is None:
            _worker = threading.Thread(target=_work, daemon=True, name="scratch-sidecars")
            _worker.start()


def _work() -> None:
    global _worker  # noqa: PLW0603
    while True:
        with _lock:
            if not _pending:
                _worker = None  # under the lock, so queue() starts a new one for what comes next
                return
            root, url, item = _pending.pop(0)
        folder = root / item["name"]
        try:
            if stale(item):
                shoot(url, folder / "thumb.webp", root, item["updated"])
            if item["describe"] and not item["description"] and not describe(
                    folder / "meta.json", item["page"], item["title"], root) and llm.backing_off():
                with _lock:  # the model is out, not the page at fault: ask again later
                    _tried.discard((item["name"], item["updated"]))
        except Exception:  # one bad page must not stop the rest
            logger.warning("Scratch sidecars for %s failed", folder.name, exc_info=True)


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
    """Render `url` at VIEWPORT in a fresh, throwaway profile and save it as a THUMB webp,
    dated `at` (the page's version it was queued for), so an edit made while it rendered
    still reads as newer."""
    exe = browser()
    if not exe:
        return False
    try:  # served, or the shot would be of the browser's error page (a 404 raises too)
        urlopen(url, timeout=10).close()
    except OSError:
        return False
    with tempfile.TemporaryDirectory(prefix="tmux-rc-thumb-") as profile:
        png = Path(profile) / "shot.png"
        subprocess.run([exe, "--headless", "--no-first-run", "--hide-scrollbars",
                        f"--user-data-dir={profile}", "--window-size={},{}".format(*VIEWPORT),
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
        f"previews: what it shows. Plain text, no preamble.\nTitle: {title}\n\n{text}")
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
