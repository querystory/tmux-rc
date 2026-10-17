"""The /scratch/ gallery (openbus/scratch.py): what it lists, in what order, and who it
says made each folder."""

import json
import os
import re
from html import unescape

import pytest

from openbus import agent_history, scratch
from openbus.server import WEB_DIR

STYLE = WEB_DIR / "m" / "style.css"


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(scratch, "_found", {})
    monkeypatch.setattr(agent_history, "get_many", lambda _ids: {})
    monkeypatch.setattr(agent_history, "reconcile", lambda: None)  # never this machine's index


def _folder(root, name, at, meta=None, page="<title>Its own title</title>"):
    folder = root / name
    folder.mkdir()
    (folder / "index.html").write_text(page)
    if meta is not None:
        (folder / "meta.json").write_text(meta if isinstance(meta, str) else json.dumps(meta))
    for path in folder.iterdir():
        os.utime(path, (at, at))
    return folder


def _resolved(root, states=(), profiles=(), pane_of=lambda _entry: None):
    listed = scratch.items(root)
    scratch.resolve(listed, list(states), list(profiles), pane_of)
    return {i["name"]: i for i in listed}, [i["name"] for i in listed]


def test_newest_first_and_titles_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_history, "writers", lambda *_: {})
    _folder(tmp_path, "old", 1000, {"title": "Named", "description": "Said so"})
    _folder(tmp_path, "new", 3000, "{not json")  # malformed: as if absent
    _folder(tmp_path, "mid", 2000, ["a", "list"], page="no title here")
    _folder(tmp_path, "amp", 500, None, page="<title>Build &amp; review</title>")
    _folder(tmp_path, "typed", 1500, {"title": 7, "pane": ["%1"]})  # wrong types: ignored
    (tmp_path / "loose.html").write_text("a file, not a folder")
    htm = tmp_path / "htm"
    htm.mkdir()
    (htm / "report.htm").write_text("<title>From an htm</title>")
    os.utime(htm / "report.htm", (100, 100))
    items, order = _resolved(tmp_path)
    assert order == ["new", "mid", "typed", "old", "amp", "htm"]
    assert items["amp"]["title"] == "Build & review" and items["htm"]["title"] == "From an htm"
    assert [items[n]["title"] for n in order[:4]] == ["Its own title", "mid", "Its own title",
                                                      "Named"]
    assert items["old"]["description"] == "Said so"
    assert items["new"]["who"] == "unknown" and items["typed"]["pane"] is None


def test_a_live_pane_names_its_window_and_account(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_history, "writers", lambda *_: pytest.fail("pane was enough"))
    _folder(tmp_path, "mock", 1000, {"pane": "%7", "created": "2026-01-02T03:04:05+00:00"})
    states = [{"pane_id": "%7", "tool": "claude", "session": "web", "window_name": "ui"}]
    profiles = [{"name": "Work", "dir": "/c", "panes": ["%7"], "color": 2}]
    items, _ = _resolved(tmp_path, states, profiles)
    want = {"who": "claude · Work", "where": "web:ui", "pane": "%7", "color": 2}
    assert items["mock"] | want == items["mock"]
    assert items["mock"]["created"] == 1767323045
    page = scratch.page(tmp_path, states, profiles, STYLE)
    assert 'href="/m/#pane=%257"' in page and 'href="mock/"' in page and ":root.light {" in page
    # The account wears the app's own chip, its rules carried over with the colours.
    assert ('<button type="button" class="who tool">claude</button><button type="button" '
            'class="who acct-badge" style="--a:var(--acct-2)"><i class="acct-dot"></i>'
            '<span>Work</span></button>') in page
    assert re.search(r"^\.acct-badge \{", page, re.MULTILINE)


def test_a_closed_pane_falls_back_to_the_transcript_once(tmp_path, monkeypatch):
    calls = []

    def writers(pattern, due):
        calls.append(due)
        return {"gone": {"session_id": "s1", "harness": "codex", "title": "Billing",
                         "source": "/c/projects/x/s1.jsonl"}}

    monkeypatch.setattr(agent_history, "writers", writers)
    running = {"s1": {"running": {"tmux_pane": "%9", "pid": 5}}}
    monkeypatch.setattr(agent_history, "get_many", lambda _ids: running)
    _folder(tmp_path, "gone", 1000, {"pane": "%3"})
    _folder(tmp_path, "nobody", 2000)
    items, _ = _resolved(tmp_path, [], [{"name": "Lab", "dir": "/c"}])
    assert items["gone"] | {"who": "codex · Lab", "where": "Billing", "pane": None} == items["gone"]
    assert items["nobody"]["who"] == "unknown"
    # %9 is where its writer runs now, and once the watcher has it, the card links there
    items, _ = _resolved(tmp_path, [{"pane_id": "%9", "tool": "codex", "session": "s",
                                     "window_name": "w"}],
                         pane_of=lambda e: e.get("running", {}).get("tmux_pane"))
    assert items["gone"]["pane"] == "%9"
    for name in ("index.html", "meta.json"):  # rewritten: created moves, the author stays
        (tmp_path / "gone" / name).write_text('{"pane": "%3"}')
    _resolved(tmp_path)
    assert calls == [{"gone": 1000, "nobody": 2000}]  # found and missed both cached


def test_a_pane_id_from_an_earlier_tmux_server_is_not_trusted(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_history, "writers", lambda *_: {})
    _folder(tmp_path, "old", 1000, {"pane": "%7"})  # this test's process began long after
    states = [{"pane_id": "%7", "tool": "claude", "session": "s", "window_name": "w",
               "birth": str(os.getpid())}]
    items, _ = _resolved(tmp_path, states)
    assert items["old"]["pane"] is None and items["old"]["who"] == "unknown"
    page = scratch.page(tmp_path, states, [], STYLE)
    assert '<p class="by"><span class="tool">unknown</span><span' in page


def test_nothing_outside_scratch_is_listed_or_read(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_history, "writers", lambda *_: {})
    root, outside = tmp_path / "scratch", tmp_path / "outside"
    root.mkdir()
    _folder(tmp_path, "outside", 1000, {"title": "OUTSIDE-TITLE"})
    (root / "escape").symlink_to(outside)
    inner = _folder(root, "inner", 1000, page="<title><script>x</script></title>")
    (inner / "meta.json").symlink_to(outside / "meta.json")
    (inner / "leak.html").symlink_to(outside / "index.html")
    loop = _folder(root, "loop", 700)
    (loop / "meta.json").symlink_to(loop / "meta.json")  # a symlink loop: as if absent
    linked = _folder(root, "linked", 900, {"description": "no title"}, page="local")
    (linked / "index.html").unlink()
    (linked / "index.html").symlink_to(outside / "index.html")
    (linked / "report.pdf").write_text("pdf")
    _folder(root, "meta-only", 800, {"title": "nothing to open"}).joinpath("index.html").unlink()
    items, order = _resolved(root)
    assert sorted(order) == ["inner", "linked", "loop"]
    assert items["inner"]["title"] == "inner"  # its <title> has no text
    assert items["linked"]["href"] == "linked/report.pdf" and items["linked"]["title"] == "linked"
    _folder(root, "xss", 1000, {"title": "<img src=x onerror=alert(1)>"})
    page = scratch.page(root, [], [], STYLE)
    assert "OUTSIDE-TITLE" not in page and "<img src=x" not in page and "&lt;img" in page


def _entry(index, harness, sid, source, started, last, parent=None):
    path = index / harness / (f"{parent}/{sid}.md" if parent else f"{sid}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = {"harness": harness, "session_id": sid, "parent_session": parent,
              "source": str(source), "title": sid, "started": started, "last_active": last}
    path.write_text("---\n" + "".join(f"{k}: {json.dumps(v)}\n" for k, v in fields.items() if v)
                    + "---\n\n# body\n")


def test_writers_finds_the_session_that_named_the_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_DIR", str(tmp_path))
    index = tmp_path / "index"
    for name, text in {"a": "wrote $TMUXRC_SCRATCH_DIR/mock/index.html", "b": "/scratch/mockup/ "
                       "/scratch/mock+draft/x /scratch/design%20v2/",
                       "sub": '{"path":"/x/scratch/report/index.html"}',
                       "late": "/scratch/mock/ again", "long": "/scratch/mock/ passed on"}.items():
        (tmp_path / f"{name}.jsonl").write_text(text)
    _entry(index, "claude", "a", tmp_path / "a.jsonl", "2026-01-01T10:00:00Z",
           "2026-01-01T11:00:00Z")
    _entry(index, "codex", "b", tmp_path / "b.jsonl", "2026-01-01T09:00:00Z",
           "2026-01-01T12:00:00Z")
    _entry(index, "claude", "mid", tmp_path / "none.jsonl", "2026-01-01T10:20:00Z",
           "2026-01-01T10:50:00Z", parent="a")
    _entry(index, "claude", "sub", tmp_path / "sub.jsonl", "2026-01-01T10:30:00Z",
           "2026-01-01T10:40:00Z", parent="mid")
    _entry(index, "claude", "long", tmp_path / "long.jsonl", "2025-12-01T10:00:00Z",
           "2026-01-03T11:00:00Z")  # open all along: it only passed the link on
    _entry(index, "claude", "late", tmp_path / "late.jsonl", "2026-01-02T10:00:00Z",
           "2026-01-02T11:00:00Z")
    day = tmp_path / "sessions" / "2026" / "01" / "01"  # a resumed, segmented Codex thread
    day.mkdir(parents=True)
    (day / "rollout-2026-01-01T09-00-00-t1_1.jsonl").write_text("/scratch/resumed/")
    (day / "rollout-2026-01-01T09-30-00-t1.jsonl").write_text("later turns")
    _entry(index, "codex", "t1", day / "rollout-2026-01-01T09-30-00-t1.jsonl",
           "2026-01-01T09:00:00Z", "2026-01-01T12:00:00Z")
    at = 1767262200  # 2026-01-01T10:10:00Z
    found = agent_history.writers(scratch._MENTION, {"mock": at, "report": at + 1500,
                                                     "mockup": at, "missing": at,
                                                     "resumed": at, "mock+draft": at,
                                                     "design v2": at})
    # "mock" is not "mockup"; a subagent's work, nested or not, is its root's; the later one that
    # only mentions it is not its writer
    assert {n: e["session_id"] for n, e in found.items()} == {"mock": "a", "mockup": "b",
                                                              "report": "a", "resumed": "t1",
                                                              "mock+draft": "b", "design v2": "b"}


def test_an_empty_gallery_says_how_agents_publish_there(tmp_path):
    page = scratch.page(tmp_path, [], [], STYLE, docs="/docs/x/")
    assert '<section id="empty">' in page and '<input hidden type="search"' in page
    assert page.count('href="/docs/x/"') == 2  # the header's About, and the empty state's
    # the snippet shown is the one documented, word for word
    doc = (WEB_DIR.parent / "docs/deploy/scratch-gallery.md").read_text()
    block = doc.split("## The agent snippet\n\n", 1)[1].split("\n\n", 1)[0]
    shown = re.search(r"<pre>(.*?)</pre>", page, re.DOTALL).group(1)
    assert unescape(shown) == " ".join(line.strip() for line in block.splitlines())  # unwrapped
    _folder(tmp_path, "one", 1000)
    assert '<section id="empty" hidden>' in scratch.page(tmp_path, [], [], STYLE)


def test_a_pending_preview_names_the_image_the_page_retries():
    from openbus.scratch import _card
    item = {"name": "a b", "href": "a%20b/", "title": "t", "description": "", "who": "",
            "where": "", "pane": None, "thumb": False, "shot": "pending",
            "created": 0, "updated": 0}
    assert 'data-pending="a%20b/thumb.webp"' in _card(item)
    assert "data-pending" not in _card({**item, "shot": "failed"})
