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
from itertools import islice
from pathlib import Path
from urllib.parse import quote

from . import agent_history, scratch_markdown, tmux

_TITLE = re.compile(r"<title[^>]*>([^<]*)", re.IGNORECASE)
# How an agent names the folder: the served URL, the usual dir, or one of the variables.
# The whole path component, so "mock" never takes "mock+draft"'s author, up to what
# ends a path in prose, markdown, JSON or a URL.
_MENTION = r"(scratch|SCRATCH_[A-Z_]+)/[^]/\"'\\<>()[{}`,;:*|?#[:space:]]+"
_RETRY = 600
_LOCK = threading.Lock()
_found: dict[tuple[str, int], tuple[float, dict | None]] = {}
_reconciled = [0.0]
_PAGE = Path(__file__).resolve().parent / "scratch.html"
_PENDING = Path(__file__).resolve().parent / "scratch_pending.html"
STALL = 30 * 60  # an in-progress folder's agent quiet this long has stopped
# What a card, then the placeholder, says of a page being made, by progress().
STATES = {"working": ("Being made…", "An agent is making this page. It opens here once it lands."),
          "quiet": ("Being made…", "The agent's pane is idle. It may still be working."),
          "stopped": ("Stopped without publishing",
                      "The agent stopped without publishing this page.")}
DOCS = "https://github.com/querystory/tmux-rc/blob/main/docs/deploy/scratch-gallery.md"
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
    hidden (a sidecar half-written, a .git) are not the page and do not date it. A folder
    whose meta.json came first is `pending` until its index lands: its page is being made
    (a site's assets may land ahead of its index, for a minute; loose files older than
    that are the publish)."""
    sidecars = {(folder, name) for name in SIDECARS}  # its own, not a page's images/thumb.webp
    files = [(f.lstat().st_mtime, f) for f in _files(folder)
             if (f.parent, f.name) not in sidecars and f.is_file() and inside(f, folder)]
    meta, thumb, names = _meta(folder), folder / "thumb.webp", {f for _, f in files}
    if not (files or meta):
        return None
    files = files or [((folder / "meta.json").stat().st_mtime, None)]
    # The folder's own page, served at folder/: index.html, or an index.md (rendered).
    index = next((i for i in (folder / "index.html", folder / "index.md") if i in names), None)
    page = index or max(files)[1]
    pending = not index and bool(meta) and (not page or time.time() - max(files)[0] < 60)
    title = meta.get("title")
    if not title and page and page.suffix == ".md":
        title = scratch_markdown.render(page)[2]
    elif not title and page and page.suffix in (".html", ".htm"):
        with page.open(errors="replace") as f:
            m = _TITLE.search(f.read(65536))
        title = m and unescape(m.group(1)).strip()
    return {"name": folder.name, "title": title or folder.name,
            "description": meta.get("description", ""), "pane": meta.get("pane"),
            "href": quote(page.relative_to(root).as_posix() if page != index and not pending
                          else folder.name + "/"),
            "created": _when(meta.get("created")) or min(files)[0], "updated": max(files)[0],
            "inode": folder.stat().st_ino,  # the folder until replaced, however rewritten
            "page": page, "pending": pending, "thumb": inside(thumb, folder)
            and thumb.is_file() and thumb.stat().st_mtime}


def read(root: Path, folder: Path) -> dict | None:
    """A folder's card, None for one hidden, a symlink, or gone (deleted or rebuilt mid-scan,
    which leaves it out, not the whole gallery)."""
    try:
        if not (folder.name.startswith(".") or folder.is_symlink()) and folder.is_dir():
            return _item(root, folder)
    except OSError:
        pass
    return None


def items(root: Path) -> list[dict]:
    """Every folder of scratch, most recently updated first."""
    return sorted(filter(None, (read(root, f) for f in root.iterdir())),
                  key=lambda i: -i["updated"])


def opened(root: Path, visits: list[tuple[str, float]], n: int) -> list[dict]:
    """The first `n` folders of these visits, (name, when), still in scratch: the title and
    link the gallery gives each, and when it was opened."""
    found = ((item, at) for name, at in visits if (item := read(root, root / name)))
    return [{"title": i["title"], "href": i["href"], "pending": i["pending"], "at": at}
            for i, at in islice(found, n)]


def _writers(todo: list[dict]) -> None:
    """Fill each item's `session` from the transcripts, through the cache."""
    now, keys = time.time(), {i["name"]: (i["name"], i["inode"]) for i in todo}
    with _LOCK:  # a second request waits for the first's search instead of repeating it
        stale = {k for k, (at, hit) in _found.items() if hit is None and now - at > _RETRY}
        due = {i["name"]: i["created"] for i in todo
               if keys[i["name"]] not in _found or keys[i["name"]] in stale}
        if due and now - _reconciled[0] > _RETRY:  # a running session's entry can be days old
            agent_history.reconcile()
            _reconciled[0] = now
        hits = agent_history.writers(_MENTION, due) if due else {}
        for n in due:
            _found[keys[n]] = (now, hits.get(n))
    for i in todo:
        i["session"] = _found[keys[i["name"]]][1]


def made_in(pane: dict, created: float) -> bool:
    """Whether the pane was already open when the folder appeared (unknown counts as yes)."""
    stat = tmux.proc_stat(pane["birth"]) if pane.get("birth") else []
    return len(stat) < 20 or tmux.started_at(stat[19]) <= created + 60


def _author(i: dict, tool: str | None, acct: dict) -> None:
    """The agent, its account and that account's chip colour, and `who` reading them as
    "claude · Work"; the account left out when it only repeats the agent ("Codex")."""
    i["tool"], i["account"], i["color"] = tool, acct.get("name"), acct.get("color")
    account = i["account"]
    if account and account.lower() != (tool or "").lower():
        i["who"] = f"{tool} · {account}" if tool else account
    else:
        i["who"] = tool or "unknown"


def progress(pane: dict | None, named: str | None, created: float) -> str:
    """How a pending folder's page is coming along (a STATES key), from the pane `named` in
    its meta.json, `pane` while that is still open: working while it runs or waits on its
    tools or subagents; quiet while it idles, which in the pane of a session whose subagent
    builds the page is not the end; stopped once it is gone or quiet for STALL. With no
    pane named, working until STALL after `created`."""
    if named and not pane:
        return "stopped"
    if pane and (pane.get("activity") in ("running", "compacting") or pane.get("agents")
                 or pane.get("waiting_on") == "external"):
        return "working"
    if time.time() - max(created, (pane or {}).get("state_since") or 0) > STALL:
        return "stopped"
    return "quiet" if pane else "working"


def resolve(listed: list[dict], states: list[dict], profiles: list[dict],
            pane_of=lambda _entry: None) -> None:
    """Who made each item: `who` (agent and account), `where` (session:window, or the
    agent session's title), `pane` when that pane is still open, and `state` (progress),
    which only a pending one shows."""
    panes = {s.get("pane_id"): s for s in states}
    for i in listed:  # a pane id from before a tmux restart names some other pane now
        if (pane := panes.get(i["pane"])) and not made_in(pane, i["created"]):
            pane = None
        i["state"] = progress(pane, i["pane"], i["created"])  # read while one is pending
        i["pane"] = pane and i["pane"]
    account = {p: a for a in profiles for p in a.get("panes", [])}
    todo = [i for i in listed if i["pane"] not in panes]
    _writers(todo)
    ids = sorted({i["session"]["session_id"] for i in todo if i["session"]})
    running = agent_history.get_many(ids) or {} if ids else {}
    for i in listed:
        s = i.pop("session", None) or {}
        i["pane"] = pane_of(running.get(s.get("session_id")) or {}) or i["pane"]  # still open
        if (pane := panes.get(i["pane"])) is not None:
            _author(i, pane.get("tool"), account.get(i["pane"]) or {})
            i["where"] = f"{pane.get('session')}:{pane.get('window_name')}"
            continue
        source = s.get("source") or ""
        acct = next((a for a in profiles if source.startswith(a.get("dir", "") + "/")), {})
        i["pane"] = None
        _author(i, s.get("harness"), acct)
        i["where"] = s.get("title") or ""


def page(root: Path, states: list[dict], profiles: list[dict], style: Path,
         pane_of=lambda _entry: None, sidecars=lambda _listed: None, docs: str = DOCS) -> str:
    """The gallery, with `style` the app's stylesheet, `pane_of` the pane a running
    agent-history entry is in (live.running_pane), `sidecars` what queues the missing
    thumbnails and descriptions (scratch_sidecars.queue) and `docs` its documentation."""
    listed = items(root)
    sidecars([i for i in listed if not i["pending"]])  # nothing to shoot or describe yet
    resolve(listed, states, profiles, pane_of)
    out = _PAGE.read_text().replace("/*tokens*/", scratch_markdown.tokens(style)).replace(
        "DOCS_URL", docs)
    if not listed:  # what scratch is, and how agents come to publish here
        out = out.replace('<section id="empty" hidden>', '<section id="empty">').replace(
            '<input type="search"', '<input hidden type="search"').replace(
            '<span class="seg"', '<span hidden class="seg"')
    return out.replace("<!--cards-->", "".join(_card(i) for i in listed))


def _chip(i: dict, e: dict) -> str:
    """The agent, then its account as the app's own chip (accounts.js badge); a tap on
    either filters the cards to it."""
    tool = (f'<button type="button" class="who tool">{e["tool"]}</button>' if "tool" in e
            else '<span class="tool">unknown</span>')
    if not i.get("account") or i["account"].lower() == (i.get("tool") or "").lower():
        return tool
    return (f'{tool}<button type="button" class="who acct-badge" '
            f'style="--a:var(--acct-{e.get("color", "")})"><i class="acct-dot"></i>'
            f'<span>{e["account"]}</span></button>')


def _where(i: dict, e: dict) -> str:
    """Where it was made, linking that pane while it is open (`e`: i's fields escaped)."""
    return (f'<a href="/m/#pane={quote(i["pane"], safe="")}">{e["where"]}</a>' if i["pane"]
            else e["where"])


def _card(i: dict) -> str:
    e = {k: escape(str(v)) for k, v in i.items() if v is not None}
    wait = STATES[i["state"]][0] if i["pending"] else (
        i.get("shot") == "pending" and "Making a preview…")
    # A pending folder's thumbnail is an earlier page's: its state shows instead, and once
    # the page lands and is shot, the poll swaps in the new thumbnail.
    poll = "" if i["thumb"] else f' data-pending="{quote(i["name"])}/thumb.webp"'
    thumb = (f'<img src="{quote(i["name"])}/thumb.webp" alt="" loading="lazy">'
             if i["thumb"] and not i["pending"]
             else f'<span class="{i["state"] if i["pending"] else ""}"{poll}>{wait}</span>'
             if wait else "No preview")
    words = " ".join([i["title"], i["description"], i["who"], i["where"]]).lower()
    return (f'<article data-q="{escape(words)}">'
            f'<a class="thumb" href="{e["href"]}" tabindex="-1" aria-hidden="true">{thumb}</a>'
            f'<h2><a href="{e["href"]}">{e["title"]}</a></h2>'
            + (f'<p class="desc">{e["description"]}</p>' if i["description"] else "")
            + f'<p class="by">{_chip(i, e)}'
            f'<span class="where">{_where(i, e)}</span></p>'
            f'<p class="muted"><time data-t="{i["created"]:.0f}"></time>'
            + (f' · updated <time data-t="{i["updated"]:.0f}"></time>'
               if i["updated"] - i["created"] > 60 else "") + "</p></article>")


def placeholder(root: Path, name: str, states: list[dict], profiles: list[dict], style: Path,
                pane_of=lambda _entry: None) -> str | None:
    """What a pending folder's URL shows until its page lands: what it will be, who is
    making it and what that pane is doing now, reloading itself (the sandbox allows no
    fetch) every 5s, every 30s after ten minutes, and every minute once stopped. None
    for any other folder."""
    i = read(root, root / name)
    if not (i and i["pending"]):
        return None
    resolve([i], states, profiles, pane_of)
    e = {k: escape(str(v)) for k, v in i.items() if v is not None}
    headline = next((s.get("headline") for s in states if s.get("pane_id") == i["pane"]), "")
    where = _where(i, e)
    age = int(time.time() - i["created"]) // 60
    fill = {"tokens": scratch_markdown.tokens(style), "title": e["title"], "state": i["state"],
            "say": STATES[i["state"]][1], "who": e["who"], "where": where and f" · {where}",
            "desc": f'<p>{e["description"]}</p>' if i["description"] else "",
            "now": f'<p class="now">Now: {escape(str(headline))}</p>' if headline else "",
            "ago": f"{age}m" if age < 60 else f"{age // 60}h {age % 60}m",
            "every": 60000 if i["state"] == "stopped" else 5000 if age < 10 else 30000}
    return re.sub(r"\{(\w+)\}", lambda m: str(fill[m[1]]), _PENDING.read_text())
