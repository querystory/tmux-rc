"""GET /scratch/: a gallery of what agents published to the scratch dir, newest first,
each card saying who made it and linking to that pane while it is still open
(docs/deploy/previews.md).

Who made a folder comes from its meta.json "pane" while that pane lives. Otherwise the
agent transcripts are searched for the folder's path, once per folder: a found author
never changes, so it is kept for the daemon's lifetime, and a miss is retried after a
while, since agent-history indexes a session only some time after it starts."""

import json
import re
import time
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote

from . import agent_history

_TITLE = re.compile(r"<title[^>]*>([^<]*)", re.IGNORECASE)
_TOKENS = re.compile(r"^:root(?:\.light)? \{.*?^\}", re.MULTILINE | re.DOTALL)  # app colours
# How an agent names the folder: the served URL, the usual dir, or one of the variables.
_MENTION = r"(scratch|SCRATCH_[A-Z_]+)/[A-Za-z0-9_.-]+"
_RETRY = 600
_found: dict[tuple[str, float], tuple[float, dict | None]] = {}
_PAGE = Path(__file__).resolve().parent / "scratch.html"


def _inside(path: Path, root: Path) -> bool:
    """The file is really in this folder of scratch, not a symlink out of it."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _meta(folder: Path) -> dict:
    """meta.json's string fields; anything malformed counts as absent."""
    path = folder / "meta.json"
    try:
        meta = json.loads(path.read_text()) if _inside(path, folder) else {}
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


def items(root: Path) -> list[dict]:
    """Every folder of scratch, most recently updated first."""
    out = []
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir():
            continue
        files = [(f.lstat().st_mtime, f) for f in folder.rglob("*")
                 if f.is_file() and _inside(f, folder)]
        if not files:
            continue
        meta, index = _meta(folder), folder / "index.html"
        page = index if index.is_file() else max(files)[1]
        title = meta.get("title")
        if not title and page.suffix == ".html":
            with page.open(errors="replace") as f:
                m = _TITLE.search(f.read(65536))
            title = m and m.group(1).strip()
        out.append({"name": folder.name, "title": title or folder.name,
                    "description": meta.get("description", ""), "pane": meta.get("pane"),
                    "href": quote(page.relative_to(root).as_posix() if page != index
                                  else folder.name + "/"),
                    "created": _when(meta.get("created")) or min(files)[0],
                    "updated": max(files)[0]})
    return sorted(out, key=lambda i: -i["updated"])


def _writers(todo: list[dict]) -> None:
    """Fill each item's `session` from the transcripts, through the cache."""
    now = time.time()
    keys = {i["name"]: (i["name"], i["created"]) for i in todo}
    due = {n: k[1] for n, k in keys.items()
           if k not in _found or (_found[k][1] is None and now - _found[k][0] > _RETRY)}
    if due:
        hits = agent_history.writers(_MENTION, due)
        for n in due:
            _found[keys[n]] = (now, hits.get(n))
    for i in todo:
        i["session"] = _found[keys[i["name"]]][1]


def _who(tool: str | None, account: str | None) -> str:
    """"claude · Work"; the account left out when it only repeats the agent ("Codex")."""
    if account and account.lower() != (tool or "").lower():
        return f"{tool} · {account}" if tool else account
    return tool or "unknown"


def resolve(listed: list[dict], states: list[dict], profiles: list[dict]) -> None:
    """Who made each item: `who` (agent and account), `where` (session:window, or the
    agent session's title) and `pane` when that pane is still open."""
    panes = {s.get("pane_id"): s for s in states}
    account = {p: a.get("name") for a in profiles for p in a.get("panes", [])}
    todo = [i for i in listed if i["pane"] not in panes]
    _writers(todo)
    ids = sorted({i["session"]["session_id"] for i in todo if i["session"]})
    running = agent_history.get_many(ids) or {} if ids else {}
    for i in listed:
        s = i.pop("session", None) or {}
        live = (running.get(s.get("session_id")) or {}).get("running") or {}
        i["pane"] = live.get("tmux_pane") or i["pane"]  # its writer, still open in a pane
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


def page(root: Path, states: list[dict], profiles: list[dict], style: Path) -> str:
    """The gallery, with `style` the app's stylesheet."""
    listed = items(root)
    resolve(listed, states, profiles)
    # The app's colour tokens, inlined: the page is sandboxed like the rest of /scratch/,
    # so it cannot count on a credentialed stylesheet load behind the front door.
    tokens = _TOKENS.findall(style.read_text())
    cards = "".join(_card(i) for i in listed) or '<p class="muted">Nothing here yet.</p>'
    return _PAGE.read_text().replace("/*tokens*/", "\n".join(tokens)).replace("<!--cards-->", cards)


def _card(i: dict) -> str:
    e = {k: escape(str(v)) for k, v in i.items() if v is not None}
    where = (f'<a href="/m/#pane={quote(i["pane"], safe="")}">{e["where"]}</a>' if i["pane"]
             else e["where"])
    words = " ".join([i["title"], i["description"], i["who"], i["where"]]).lower()
    return (f'<article data-q="{escape(words)}">'
            f'<h2><a href="{e["href"]}">{e["title"]}</a></h2>'
            + (f'<p class="desc">{e["description"]}</p>' if i["description"] else "")
            + f'<p class="by"><button type="button" class="who">{e["who"]}</button>'
            f'<span class="where">{where}</span></p>'
            f'<p class="muted"><time data-t="{i["created"]:.0f}"></time>'
            + (f' · updated <time data-t="{i["updated"]:.0f}"></time>'
               if i["updated"] - i["created"] > 60 else "") + "</p></article>")
