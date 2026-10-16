"""The scratch gallery's sidecars (openbus/scratch_sidecars.py): when a thumbnail is due,
what a description may overwrite, and that a shot never runs in the user's browser
profile."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from openbus import llm, scratch
from openbus import scratch_sidecars as S


def _page(root, name="p", at=1000, page="<title>T</title><p>Hello</p>"):
    folder = root / name
    folder.mkdir()
    (folder / "index.html").write_text(page)
    os.utime(folder / "index.html", (at, at))
    return folder


def test_sidecars_neither_date_the_folder_nor_stand_in_for_its_page(tmp_path):
    folder = _page(tmp_path)
    (folder / "meta.json").write_text('{"description": "Said"}')
    (folder / ".thumb.webp.tmp").write_text("half written")
    Image.new("RGB", (4, 4)).save(folder / "thumb.webp")
    os.utime(folder / "thumb.webp", (900, 900))
    [item] = scratch.items(tmp_path)
    assert item["updated"] == item["created"] == 1000 and item["page"].name == "index.html"
    assert item["thumb"] == 900 and S.stale(item)  # older than the page
    os.utime(folder / "thumb.webp", (2000, 2000))
    assert not S.stale(scratch.items(tmp_path)[0])
    (folder / "thumb.webp").unlink()
    (folder / "thumb.webp").symlink_to(tmp_path / "elsewhere.webp")
    assert S.stale(scratch.items(tmp_path)[0])  # a thumbnail out of scratch is none
    meta_only = tmp_path / "m"
    meta_only.mkdir()
    (meta_only / "meta.json").write_text("{}")
    assert [i["name"] for i in scratch.items(tmp_path)] == ["p"]


def test_only_pages_a_browser_can_draw_get_a_thumbnail(tmp_path):
    folder = tmp_path / "pdf"
    folder.mkdir()
    (folder / "report.pdf").write_text("%PDF")
    assert not S.stale(scratch.items(tmp_path)[0])


@pytest.fixture
def model(monkeypatch):
    asked = []
    monkeypatch.setattr(llm, "one_sentence", lambda prompt: asked.append(prompt) or "A chart.")
    return asked


def test_a_description_is_written_once_and_marked(tmp_path, model):
    folder = _page(tmp_path, page="<title>T</title><script>secret()</script><p>Hello</p>")
    (folder / "meta.json").write_text('{"pane": "%3", "created": 5}')
    assert S.describe(folder / "meta.json", folder / "index.html", "T") == "A chart."
    assert "Hello" in model[0] and "secret" not in model[0]
    assert json.loads((folder / "meta.json").read_text()) == {
        "pane": "%3", "created": 5, "description": "A chart.", "generated": ["description"]}
    assert scratch.items(tmp_path)[0]["description"] == "A chart."


@pytest.mark.parametrize("meta", ['{"description": "Agent wrote this"}', "{broken", "[1]"])
def test_never_over_what_the_agent_wrote(tmp_path, model, meta):
    folder = _page(tmp_path)
    (folder / "meta.json").write_text(meta)
    assert S.describe(folder / "meta.json", folder / "index.html", "T") is None
    assert (folder / "meta.json").read_text() == meta


def test_never_through_a_symlinked_meta(tmp_path, model):
    folder, outside = _page(tmp_path), tmp_path / "outside.json"
    outside.write_text("{}")
    (folder / "meta.json").symlink_to(outside)
    assert S.describe(folder / "meta.json", folder / "index.html", "T") is None
    assert outside.read_text() == "{}"


def test_a_shot_runs_the_bundled_shell_in_a_throwaway_profile(tmp_path, monkeypatch):
    shell = tmp_path / "ms-playwright/chromium_headless_shell-9/chrome-linux/chrome-headless-shell"
    shell.parent.mkdir(parents=True)
    shell.write_text("")
    shell.chmod(0o755)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "ms-playwright"))
    monkeypatch.delenv("TMUXRC_THUMB_BROWSER", raising=False)
    monkeypatch.setattr(S, "urlopen", lambda url, timeout: open(os.devnull))  # noqa: SIM115
    runs = []

    def run(argv, **_):
        runs.append(argv)
        profile = Path(next(a for a in argv if a.startswith("--user-data-dir="))[16:])
        assert profile.is_dir() and not list(profile.iterdir())  # fresh and empty
        Image.new("RGB", (1280, 800), "red").save(next(a for a in argv
                                                       if a.startswith("--screenshot="))[13:])

    monkeypatch.setattr(S.subprocess, "run", run)
    out = tmp_path / "thumb.webp"
    assert S.shoot("http://127.0.0.1:1/scratch/p/", out)
    [argv] = runs
    assert argv[0] == str(shell) and "--headless" in argv
    profile = next(a for a in argv if a.startswith("--user-data-dir="))[16:]
    assert Path(profile).parent == Path(tempfile.gettempdir()) and not Path(profile).exists()
    assert not any(str(Path.home()) in a for a in argv)
    with Image.open(out) as im:
        assert (im.format, im.size) == ("WEBP", S.THUMB)


def test_no_bundled_shell_means_no_shot(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))  # however Chrome is on PATH
    monkeypatch.delenv("TMUXRC_THUMB_BROWSER", raising=False)
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: pytest.fail("ran a browser"))
    assert S.browser() is None and not S.shoot("http://127.0.0.1:1/", tmp_path / "t.webp")


def test_each_folder_version_is_worked_once(tmp_path, monkeypatch):
    done = []
    monkeypatch.setattr(S, "shoot", lambda url, out: done.append(("shot", url)))
    monkeypatch.setattr(S, "describe", lambda meta, page, title: done.append(("said", title)))
    monkeypatch.setattr(S, "_tried", set())
    _page(tmp_path)
    for _ in range(2):
        S.queue(tmp_path, "http://d/scratch", scratch.items(tmp_path))
        while (worker := S._worker) is not None:
            worker.join()
    assert done == [("shot", "http://d/scratch/p/"), ("said", "T")]
    S.queue(tmp_path, "http://d/scratch", scratch.items(tmp_path), describe=False)
    assert S._worker is None  # nothing new to do


def test_a_description_missed_while_the_model_is_out_is_asked_again(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_tried", set())
    monkeypatch.setattr(S, "shoot", lambda url, out: None)
    monkeypatch.setattr(S, "describe", lambda *_: None)
    monkeypatch.setattr(llm, "backing_off", lambda: True)
    _page(tmp_path)
    S.queue(tmp_path, "http://d/scratch", scratch.items(tmp_path))
    while (worker := S._worker) is not None:
        worker.join()
    assert S._tried == set()
