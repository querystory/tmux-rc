"""GET /scratch/: a gallery of what agents published to the scratch dir, newest first,
each card saying who made it and linking to that pane while it is still open
(docs/deploy/previews.md).

Who made a folder comes from its meta.json "pane" while that pane lives. Otherwise the
agent transcripts are searched for the folder's path, once per folder: a found author
never changes, so it is kept for the daemon's lifetime, and a miss is retried after a
while, since agent-history indexes a session only some time after it starts."""

import json
import os
import re
import threading
import time
from datetime import datetime
from html import escape, unescape
from pathlib import Path
from urllib.parse import quote

from . import agent_history, tmux

_TITLE = re.compile(r"<title[^>]*>([^<]*)", re.IGNORECASE)
_TOKENS = re.compile(r"^:root(?:\.light)? \{.*?^\}", re.MULTILINE | re.DOTALL)  # app colours
# How an agent names the folder: the served URL, the usual dir, or one of the variables.
# The whole path component, so "mock" never takes "mock+draft"'s author, up to what
# ends a path in prose, markdown, JSON or a URL.
_MENTION = r"(scratch|SCRATCH_[A-Z_]+)/[^]/\"'\\<>()[{}`,;:*|?#[:space:]]+"
_RETRY = 600
_LOCK = threading.Lock()
_found: dict[tuple[str, int], tuple[float, dict | None]] = {}
_PAGE = Path(__file__).resolve().parent / "scratch.html"
SIDECARS = ("meta.json", "thumb.webp")  # what tmux-rc and agents add beside a page
# What every /scratch/ response is served under (see server.py's no_cache middleware).
SANDBOX = "sandbox allow-scripts allow-popups; form-action 'none'; connect-src 'none'"


def inside(path: Path, root: Path) -> bool:
    """The file is really in this folder of scratch, not a symlink out of it."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):  # 3.12 raises RuntimeError for a symlink loop
        return False


def _meta(folder: Path) -> dict:
    """meta.json's string fields; anything malformed counts as absent."""
    path = folder / "meta.json"
    try:
        meta = json.loads(path.read_text()) if inside(path, folder) else {}
    except (OSError, ValueError):
        return {}
    if not isinstance(meta, dict):
        return {}
    return {k: v.strip() for k, v in meta.items() if isinstance(v, str) and v.strip()}


def _when(stamp: str | None) -> float | None:
    try:
        return datetime.fromisoformat(stamp).timestamp() if stamp else None
    except ValueError:
        return None


def _files(folder: Path):
    """Every file under `folder`, never descending into a hidden directory (.git)."""
    for top, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        yield from (Path(top, n) for n in names if not n.startswith("."))


def _item(root: Path, folder: Path) -> dict | None:
    """One card's data; None for a folder with nothing to open. Its sidecars and anything
    hidden (a sidecar half-written, a .git) are not the page and do not date it."""
    files = [(f.lstat().st_mtime, f) for f in _files(folder)
             if f.name not in SIDECARS and f.is_file() and inside(f, folder)]
    if not files:
        return None
    meta, index, thumb = _meta(folder), folder / "index.html", folder / "thumb.webp"
    page = index if any(f == index for _, f in files) else max(files)[1]
    title = meta.get("title")
    if not title and page.suffix in (".html", ".htm"):
        with page.open(errors="replace") as f:
            m = _TITLE.search(f.read(65536))
        title = m and unescape(m.group(1)).strip()
    return {"name": folder.name, "title": title or folder.name,
            "description": meta.get("description", ""), "pane": meta.get("pane"),
            "href": quote(page.relative_to(root).as_posix() if page != index
                          else folder.name + "/"),
            "created": _when(meta.get("created")) or min(files)[0], "updated": max(files)[0],
            "inode": folder.stat().st_ino,  # the folder until replaced, however rewritten
            "page": page, "thumb": inside(thumb, folder) and thumb.is_file()
            and thumb.stat().st_mtime}


def items(root: Path) -> list[dict]:
    """Every folder of scratch, most recently updated first."""
    out = []
    for folder in root.iterdir():
        try:  # a folder deleted or rebuilt mid-scan is left out, not the whole gallery
            if not (folder.name.startswith(".") or folder.is_symlink()) and folder.is_dir() and (
                    item := _item(root, folder)):
                out.append(item)
        except OSError:
            continue
    return sorted(out, key=lambda i: -i["updated"])


def _writers(todo: list[dict]) -> None:
    """Fill each item's `session` from the transcripts, through the cache."""
    now, keys = time.time(), {i["name"]: (i["name"], i["inode"]) for i in todo}
    with _LOCK:  # a second request waits for the first's search instead of repeating it
        stale = {k for k, (at, hit) in _found.items() if hit is None and now - at > _RETRY}
        due = {i["name"]: i["created"] for i in todo
               if keys[i["name"]] not in _found or keys[i["name"]] in stale}
        hits = agent_history.writers(_MENTION, due) if due else {}
        for n in due:
            _found[keys[n]] = (now, hits.get(n))
    for i in todo:
        i["session"] = _found[keys[i["name"]]][1]


def _since(pane: dict, created: float) -> bool:
    """Whether the pane was already open when the folder appeared (unknown counts as yes)."""
    stat = tmux.proc_stat(pane["birth"]) if pane.get("birth") else []
    return len(stat) < 20 or tmux.started_at(stat[19]) <= created + 60


def _who(tool: str | None, account: str | None) -> str:
    """"claude · Work"; the account left out when it only repeats the agent ("Codex")."""
    if account and account.lower() != (tool or "").lower():
        return f"{tool} · {account}" if tool else account
    return tool or "unknown"


def resolve(listed: list[dict], states: list[dict], profiles: list[dict],
            pane_of=lambda _entry: None) -> None:
    """Who made each item: `who` (agent and account), `where` (session:window, or the
    agent session's title) and `pane` when that pane is still open."""
    panes = {s.get("pane_id"): s for s in states}
    for i in listed:  # a pane id from before a tmux restart names some other pane now
        if (pane := panes.get(i["pane"])) and not _since(pane, i["created"]):
            i["pane"] = None
    account = {p: a.get("name") for a in profiles for p in a.get("panes", [])}
    todo = [i for i in listed if i["pane"] not in panes]
    _writers(todo)
    ids = sorted({i["session"]["session_id"] for i in todo if i["session"]})
    running = agent_history.get_many(ids) or {} if ids else {}
    for i in listed:
        s = i.pop("session", None) or {}
        i["pane"] = pane_of(running.get(s.get("session_id")) or {}) or i["pane"]  # still open
        if (pane := panes.get(i["pane"])) is not None:
            i["who"] = _who(pane.get("tool"), account.get(i["pane"]))
            i["where"] = f"{pane.get('session')}:{pane.get('window_name')}"
            continue
        source = s.get("source") or ""
        acct = next((a.get("name") for a in profiles
                     if source.startswith(a.get("dir", "") + "/")), None)
        i["pane"] = None
        i["who"] = _who(s.get("harness"), acct)
        i["where"] = s.get("title") or ""


def page(root: Path, states: list[dict], profiles: list[dict], style: Path,
         pane_of=lambda _entry: None, sidecars=lambda _listed: None) -> str:
    """The gallery, with `style` the app's stylesheet, `pane_of` the pane a running
    agent-history entry is in (live.running_pane) and `sidecars` what queues the missing
    thumbnails and descriptions (scratch_sidecars.queue)."""
    listed = items(root)
    sidecars(listed)
    resolve(listed, states, profiles, pane_of)
    # The app's colour tokens, inlined: the page is sandboxed like the rest of /scratch/,
    # so it cannot count on a credentialed stylesheet load behind the front door.
    tokens = _TOKENS.findall(style.read_text())
    cards = "".join(_card(i) for i in listed) or '<p class="muted">Nothing here yet.</p>'
    return _PAGE.read_text().replace("/*tokens*/", "\n".join(tokens)).replace("<!--cards-->", cards)


def _card(i: dict) -> str:
    e = {k: escape(str(v)) for k, v in i.items() if v is not None}
    thumb = f'<img src="{quote(i["name"])}/thumb.webp" alt="" loading="lazy">' if i["thumb"] else ""
    where = (f'<a href="/m/#pane={quote(i["pane"], safe="")}">{e["where"]}</a>' if i["pane"]
             else e["where"])
    words = " ".join([i["title"], i["description"], i["who"], i["where"]]).lower()
    return (f'<article data-q="{escape(words)}">'
            f'<a class="thumb" href="{e["href"]}" tabindex="-1" aria-hidden="true">{thumb}</a>'
            f'<h2><a href="{e["href"]}">{e["title"]}</a></h2>'
            + (f'<p class="desc">{e["description"]}</p>' if i["description"] else "")
            + f'<p class="by"><button type="button" class="who">{e["who"]}</button>'
            f'<span class="where">{where}</span></p>'
            f'<p class="muted"><time data-t="{i["created"]:.0f}"></time>'
            + (f' · updated <time data-t="{i["updated"]:.0f}"></time>'
               if i["updated"] - i["created"] > 60 else "") + "</p></article>")
