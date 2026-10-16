"""The /scratch/ gallery (openbus/scratch.py): what it lists, in what order, and who it
says made each folder."""

import json
import os

import pytest

from openbus import agent_history, scratch
from openbus.server import WEB_DIR

STYLE = WEB_DIR / "m" / "style.css"


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(scratch, "_found", {})
    monkeypatch.setattr(agent_history, "get_many", lambda _ids: {})


def _folder(root, name, at, meta=None, page="<title>Its own title</title>"):
    folder = root / name
    folder.mkdir()
    (folder / "index.html").write_text(page)
    if meta is not None:
        (folder / "meta.json").write_text(meta if isinstance(meta, str) else json.dumps(meta))
    for path in folder.iterdir():
        os.utime(path, (at, at))
    return folder


def _resolved(root, states=(), profiles=()):
    listed = scratch.items(root)
    scratch.resolve(listed, list(states), list(profiles))
    return {i["name"]: i for i in listed}, [i["name"] for i in listed]


def test_newest_first_and_titles_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_history, "writers", lambda *_: {})
    _folder(tmp_path, "old", 1000, {"title": "Named", "description": "Said so"})
    _folder(tmp_path, "new", 3000, "{not json")  # malformed: as if absent
    _folder(tmp_path, "mid", 2000, ["a", "list"], page="no title here")
    _folder(tmp_path, "typed", 1500, {"title": 7, "pane": ["%1"]})  # wrong types: ignored
    (tmp_path / "loose.html").write_text("a file, not a folder")
    items, order = _resolved(tmp_path)
    assert order == ["new", "mid", "typed", "old"]
    assert [items[n]["title"] for n in order] == ["Its own title", "mid", "Its own title", "Named"]
    assert items["old"]["description"] == "Said so"
    assert items["new"]["who"] == "unknown" and items["typed"]["pane"] is None


def test_a_live_pane_names_its_window_and_account(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_history, "writers", lambda *_: pytest.fail("pane was enough"))
    _folder(tmp_path, "mock", 1000, {"pane": "%7", "created": "2026-01-02T03:04:05+00:00"})
    states = [{"pane_id": "%7", "tool": "claude", "session": "web", "window_name": "ui"}]
    items, _ = _resolved(tmp_path, states, [{"name": "Work", "dir": "/c", "panes": ["%7"]}])
    want = {"who": "claude · Work", "where": "web:ui", "pane": "%7"}
    assert items["mock"] | want == items["mock"]
    assert items["mock"]["created"] == 1767323045
    page = scratch.page(tmp_path, states, [], STYLE)
    assert 'href="/m/#pane=%257"' in page and 'href="mock/"' in page and ":root.light {" in page


def test_a_closed_pane_falls_back_to_the_transcript_once(tmp_path, monkeypatch):
    calls = []

    def writers(pattern, due):
        calls.append(due)
        return {"gone": {"session_id": "s1", "harness": "codex", "title": "Billing",
                         "source": "/c/projects/x/s1.jsonl"}}

    monkeypatch.setattr(agent_history, "writers", writers)
    running = {"s1": {"running": {"tmux_pane": "%9"}}}
    monkeypatch.setattr(agent_history, "get_many", lambda _ids: running)
    _folder(tmp_path, "gone", 1000, {"pane": "%3"})
    _folder(tmp_path, "nobody", 2000)
    items, _ = _resolved(tmp_path, [], [{"name": "Lab", "dir": "/c"}])
    assert items["gone"] | {"who": "codex · Lab", "where": "Billing", "pane": None} == items["gone"]
    assert items["nobody"]["who"] == "unknown"
    # %9 is where its writer runs now, and once the watcher has it, the card links there
    items, _ = _resolved(tmp_path, [{"pane_id": "%9", "tool": "codex", "session": "s",
                                     "window_name": "w"}])
    assert items["gone"]["pane"] == "%9"
    assert calls == [{"gone": 1000, "nobody": 2000}]  # found and missed both cached


def test_nothing_outside_scratch_is_listed_or_read(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_history, "writers", lambda *_: {})
    root, outside = tmp_path / "scratch", tmp_path / "outside"
    root.mkdir()
    _folder(tmp_path, "outside", 1000, {"title": "secret"})
    (root / "escape").symlink_to(outside)
    inner = _folder(root, "inner", 1000, page="<title><script>x</script></title>")
    (inner / "meta.json").symlink_to(outside / "meta.json")
    (inner / "leak.html").symlink_to(outside / "index.html")
    items, order = _resolved(root)
    assert order == ["inner"] and items["inner"]["title"] == "inner"  # <title> has no text
    _folder(root, "xss", 1000, {"title": "<img src=x onerror=alert(1)>"})
    page = scratch.page(root, [], [], STYLE)
    assert "secret" not in page and "<img" not in page and "&lt;img" in page


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
    for name, text in {"a": "wrote $TMUXRC_SCRATCH_DIR/mock/index.html", "b": "/scratch/mockup/",
                       "sub": '{"path":"/x/scratch/report/index.html"}',
                       "late": "/scratch/mock/ again"}.items():
        (tmp_path / f"{name}.jsonl").write_text(text)
    _entry(index, "claude", "a", tmp_path / "a.jsonl", "2026-01-01T10:00:00Z",
           "2026-01-01T11:00:00Z")
    _entry(index, "codex", "b", tmp_path / "b.jsonl", "2026-01-01T09:00:00Z",
           "2026-01-01T12:00:00Z")
    _entry(index, "claude", "sub", tmp_path / "sub.jsonl", "2026-01-01T10:30:00Z",
           "2026-01-01T10:40:00Z", parent="a")
    _entry(index, "claude", "late", tmp_path / "late.jsonl", "2026-01-02T10:00:00Z",
           "2026-01-02T11:00:00Z")
    at = 1767262200  # 2026-01-01T10:10:00Z
    found = agent_history.writers(scratch._MENTION, {"mock": at, "report": at + 1500,
                                                     "mockup": at, "missing": at})
    # "mock" is not "mockup"; a subagent's work is its parent's; the later session that
    # only mentions it is not its writer
    assert {n: e["session_id"] for n, e in found.items()} == {"mock": "a", "mockup": "b",
                                                              "report": "a"}
