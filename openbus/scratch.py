"""GET /scratch/: a gallery of what agents published to the scratch dir, newest first,
each card saying who made it and linking to that pane while it is still open
(docs/deploy/previews.md).

Who made a folder comes from its meta.json "pane" while that pane lives. Otherwise the
agent transcripts are searched for the folder's path, once per folder: a found author
never changes, so its session id goes into the folder's meta.json (marked generated, like
a description) and is never searched for again; a miss is retried after a while, since
agent-history indexes a session only some time after it starts."""

import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime
from html import escape, unescape
from itertools import islice
from pathlib import Path
from urllib.parse import quote

from . import agent_history, scratch_markdown, tmux

logger = logging.getLogger(__name__)
_TITLE = re.compile(r"<title[^>]*>([^<]*)", re.IGNORECASE)
# How an agent names the folder: the served URL, the usual dir, or one of the variables.
# The whole path component, so "mock" never takes "mock+draft"'s author, up to what
# ends a path in prose, markdown, JSON or a URL.
_MENTION = r"(scratch|SCRATCH_[A-Z_]+)/[^]/\"'\\<>()[{}`,;:*|?#[:space:]]+"
_RETRY = 600
_LOCK = threading.Lock()
_META = threading.Lock()  # one meta.json read-merge-replace at a time (authors, descriptions)
_missed: dict[tuple[str, int], float] = {}  # when each (name, inode) was last not found
_found: dict[tuple[str, int], str] = {}  # for a refusing meta.json, or a listing older than it
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
    try:  # a plain entry of the folder needs no resolving (most calls: hot in the gallery)
        return ((path.parent == root and path.name not in ("", ".", "..")
                 and not path.is_symlink()) or path.resolve().is_relative_to(root.resolve()))
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


def _files(root: Path, top: Path) -> dict[str, list[tuple[float, str]]]:
    """{name: [(mtime, path)]} of every file in the folders of scratch under `top` (scratch
    or one folder), never descending into a hidden directory (.git) nor following a
    directory link, and a file link only to a file still in its folder. One `find` for
    them all: walked in Python, each stat waited on the GIL behind the daemon's threads,
    ten times slower. Its exit status is not checked: a file deleted mid-walk fails it,
    and what it listed is still right."""
    out: dict[str, list[tuple[float, str]]] = defaultdict(list)
    try:
        listed = subprocess.run(
            ["find", str(top), "-mindepth", "1", "-name", ".*", "-prune", "-o", "(",
             "-type", "f", "-o", "-type", "l", ")", "-printf", r"%y %T@ %p\0"],
            capture_output=True, check=False).stdout
    except OSError as e:  # no find: an empty gallery, not a 500
        logger.warning("scratch: cannot list %s: %s", top, e)
        listed = b""
    start = len(str(root)) + 1  # strings, not Paths: thousands of them, under the GIL too
    for row in listed.split(b"\0")[:-1]:
        kind, at, path = os.fsdecode(row).split(" ", 2)
        name, sub, _ = path[start:].partition("/")
        if not sub:  # a loose file of scratch, or a folder link, which find never enters
            continue
        if kind == "f" or (inside(Path(path), root / name) and os.path.isfile(path)):
            out[name].append((float(at), path))
    return out


def _item(root: Path, folder: Path, files: list[tuple[float, str]]) -> dict | None:
    """One card's data; None for a folder with nothing to open. Its sidecars and anything
    hidden (a sidecar half-written, a .git) are not the page and do not date it. A folder
    whose meta.json came first is `pending` until its index lands: its page is being made
    (a site's assets may land ahead of its index, for a minute; loose files older than
    that are the publish)."""
    sidecars = {str(folder / name) for name in SIDECARS}  # its own, not a page's images/thumb.webp
    files = [(at, f) for at, f in files if f not in sidecars]
    meta, thumb, names = _meta(folder), folder / "thumb.webp", {f for _, f in files}
    if not (files or meta):
        return None
    files = files or [((folder / "meta.json").stat().st_mtime, None)]
    # The folder's own page, served at folder/: index.html, or an index.md (rendered).
    index = next((i for i in (folder / "index.html", folder / "index.md") if str(i) in names),
                 None)
    page = index or (max(files)[1] and Path(max(files)[1]))
    pending = not index and bool(meta) and not any(  # a page not at its index: published
        f.endswith((".html", ".htm", ".md")) for f in names) and (
        not page or time.time() - max(files)[0] < 60)
    # The page is read: checked again, as it may have become a link out since find listed it.
    title = meta.get("title") or (None if not page or inside(page, folder) else folder.name)
    if not title and page and page.suffix == ".md":
        title = scratch_markdown.render(page)[2]
    elif not title and page and page.suffix in (".html", ".htm"):
        with page.open(errors="replace") as f:
            m = _TITLE.search(f.read(65536))
        title = m and unescape(m.group(1)).strip()
    return {"name": folder.name, "title": title or folder.name,
            "description": meta.get("description", ""), "pane": meta.get("pane"),
            "session_id": meta.get("agent_session"), "folder": folder,
            "href": quote(page.relative_to(root).as_posix() if page != index and not pending
                          else folder.name + "/"),
            "created": _when(meta.get("created")) or min(files)[0], "updated": max(files)[0],
            "inode": folder.stat().st_ino,  # the folder until replaced, however rewritten
            "page": page, "pending": pending, "thumb": inside(thumb, folder)
            and thumb.is_file() and thumb.stat().st_mtime}


def read(root: Path, folder: Path, files: list | None = None) -> dict | None:
    """A folder's card, None for one hidden, a symlink, or gone (deleted or rebuilt mid-scan,
    which leaves it out, not the whole gallery)."""
    try:
        if not (folder.name.startswith(".") or folder.is_symlink()) and folder.is_dir():
            return _item(root, folder, _files(root, folder)[folder.name]
                         if files is None else files)
    except OSError:
        pass
    return None


def items(root: Path) -> list[dict]:
    """Every folder of scratch, most recently updated first."""
    files = _files(root, root)
    return sorted(filter(None, (read(root, f, files[f.name]) for f in root.iterdir())),
                  key=lambda i: -i["updated"])


def opened(root: Path, visits: list[tuple[str, float]], n: int,
           states: list[dict] = ()) -> list[dict]:
    """The first `n` folders of these visits, (name, when), still in scratch: the title and
    link the gallery gives each, whether its page is still being made (not stopped), and
    when it was opened."""
    found = list(islice(((item, at) for name, at in visits
                         if (item := read(root, root / name))), n))
    _progress([i for i, _ in found], states)
    return [{"title": i["title"], "href": i["href"], "at": at,
             "pending": i["pending"] and i["state"] != "stopped"} for i, at in found]


def maker(root: Path, name: str, states: list[dict]) -> dict | None:
    """The pane (its state) folder `name`'s meta.json says made it, while that very pane is
    still open (never another under a recycled id): where the scratch viewer's composer sends."""
    if not (item := read(root, root / name)):
        return None
    return _progress([item], states).get(item["pane"])


def about(root: Path, name: str) -> str | None:
    """What a message sent from the scratch viewer opens with, so its agent knows the page:
    one line naming the folder. None for no folder of scratch."""
    if not read(root, root / name):
        return None
    return f"Re scratch {''.join(c for c in name if c.isprintable())}: "


def _writers(todo: list[dict]) -> None:
    """Fill in each item's missing `session_id` from the transcripts, saving what is found."""
    now, keys = time.time(), {i["name"]: (i["name"], i["inode"]) for i in todo}
    with _LOCK:  # a second request waits for the first's search instead of repeating it
        for i in todo:
            i["session_id"] = i["session_id"] or _found.get(keys[i["name"]])
        due = {i["name"]: i["created"] for i in todo if not i["session_id"]
               and now - _missed.get(keys[i["name"]], 0) > _RETRY}
        if due and now - _reconciled[0] > _RETRY:  # a running session's entry can be days old
            agent_history.reconcile()
            _reconciled[0] = now
        hits = agent_history.writers(_MENTION, due) if due else {}
        for i in todo:
            if hit := hits.get(i["name"]):
                i["session_id"] = hit["session_id"]
                _found[keys[i["name"]]] = i["session_id"]
                annotate(i["folder"] / "meta.json", i["folder"].parent, "agent_session",
                         i["session_id"],
                         i["inode"])  # not into a folder rebuilt during the search
            elif i["name"] in due:
                _missed[keys[i["name"]]] = now


def replace(path: Path, root: Path, write, *, exclusive: bool = False) -> None:
    """Write `path` whole or not at all, never through a symlink planted at its name, and
    only while its folder is still really in scratch: it may have been swapped for a
    symlink out since it was listed. `exclusive` never over a file there by then
    (FileExistsError): one an agent wrote meanwhile is kept."""
    if not inside(path.parent, root):
        raise PermissionError(f"{path.parent} is no longer in scratch")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    write(tmp)
    if not exclusive:
        tmp.replace(path)
        return
    try:
        os.link(tmp, path)
    finally:
        tmp.unlink()


def annotate(meta: Path, root: Path, field: str, value: str, inode: int = 0) -> bool:
    """Add a field tmux-rc worked out to meta.json, listed in its "generated". Never over
    one already there, nor into a meta.json that is unreadable or a symlink: those are the
    agent's to fix. With `inode`, only while its folder is still that one."""
    try:
        with _META:
            if inode and meta.parent.stat().st_ino != inode:
                return False
            fresh = not os.path.lexists(meta)
            current = {} if fresh else None if meta.is_symlink() else json.loads(
                meta.read_text())
            if not isinstance(current, dict) or str(current.get(field) or "").strip():
                return False
            made = current.get("generated")
            made = [g for g in made if isinstance(g, str)] if isinstance(made, list) else []
            data = {**current, field: value, "generated": [*dict.fromkeys([*made, field])]}
            text = json.dumps(data, indent=1) + "\n"
            replace(meta, root, lambda tmp: tmp.write_text(text), exclusive=fresh)
    except (OSError, ValueError):
        return False
    return True


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


def _progress(listed: list[dict], states: list[dict]) -> dict:
    """Each item's `state` (progress, read while one is pending), and its `pane` while that
    pane is still open. Returns the panes by id."""
    panes = {s.get("pane_id"): s for s in states}
    for i in listed:  # a pane id from before a tmux restart names some other pane now
        if (pane := panes.get(i["pane"])) and not made_in(pane, i["created"]):
            pane = None
        i["state"] = progress(pane, i["pane"], i["created"])
        i["pane"] = pane and i["pane"]
    return panes


def resolve(listed: list[dict], states: list[dict], profiles: list[dict],
            pane_of=lambda _entry: None) -> None:
    """Who made each item: `who` (agent and account), `where` (session:window, or the
    agent session's title), `pane` when that pane is still open, and `state` (progress),
    which only a pending one shows."""
    panes = _progress(listed, states)
    account = {p: a for a in profiles for p in a.get("panes", [])}
    todo = [i for i in listed if i["pane"] not in panes]
    _writers(todo)
    ids = sorted({i["session_id"] for i in todo if i["session_id"]})
    running = agent_history.get_many(ids) or {} if ids else {}
    open_in = {sid: pane_of(e) for sid, e in running.items()}  # asks tmux: once a session
    if missing := [sid for sid in ids if sid not in running]:  # no binary, or a subagent
        running = {**agent_history.indexed(missing), **running}  # the index still says who
    for i in listed:
        s = running.get(i["session_id"]) or {} if i["pane"] not in panes else {}
        i["pane"] = open_in.get(s.get("session_id")) or i["pane"]  # still open
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
    stopped = i["pending"] and i.get("state") == "stopped"  # nothing coming to poll for
    poll = "" if i["thumb"] or stopped else (
        f' data-pending="{quote(i["name"])}/thumb.webp"')
    thumb = (f'<img src="{quote(i["name"])}/thumb.webp" alt="" loading="lazy">'
             if i["thumb"] and not i["pending"]
             else f'<span class="{i["state"] if i["pending"] else ""}"{poll}>{wait}</span>'
             if wait else "No preview")
    words = " ".join([i["title"], i["description"], i["who"], i["where"]]).lower()
    return (f'<article data-q="{escape(words)}">'
            f'<a class="thumb" href="{e["href"]}" tabindex="-1" aria-hidden="true">{thumb}</a>'
            f'<h2><a href="{e["href"]}"'
            + (f' aria-label="{e["title"]}: {wait}"' if i["pending"] else "")  # the thumb's
            + f'>{e["title"]}</a></h2>'
            + (f'<p class="desc">{e["description"]}</p>' if i["description"] else "")
            + f'<p class="by">{_chip(i, e)}'
            f'<span class="where">{_where(i, e)}</span></p>'
            f'<p class="muted"><time data-t="{i["created"]:.0f}"></time>'
            + (f' · updated <time data-t="{i["updated"]:.0f}"></time>'
               if i["updated"] - i["created"] > 60 else "") + "</p></article>")


def placeholder(root: Path, name: str, who) -> str | None:
    """What a pending folder's URL shows until its page lands: what it will be, who is
    making it and what that pane is doing now, reloading itself (the sandbox allows no
    fetch) every 5s, every 30s after ten minutes, and every minute once stopped. None
    for any other folder. `who()` gives the panes, their accounts, the stylesheet and,
    optionally, pane_of (as page's); it scans every account, so only a pending folder asks."""
    i = read(root, root / name)
    if not (i and i["pending"]):
        return None
    states, profiles, style, *pane_of = who()
    resolve([i], states, profiles, *pane_of)
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
