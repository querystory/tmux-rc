"""The scratch gallery's sidecars (openbus/scratch_sidecars.py): when a thumbnail is due,
what a description may overwrite, and that a shot never runs in the user's browser
profile."""

import json
import os
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

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
    (folder / ".git").mkdir()
    (folder / ".git" / "HEAD").write_text("ref")
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
    _page(tmp_path, ".staging")  # hidden at the root too
    (folder / "img").mkdir()  # a page's own asset that happens to share a sidecar's name
    (folder / "img" / "thumb.webp").write_text("asset")
    os.utime(folder / "img" / "thumb.webp", (3000, 3000))
    assert scratch.items(tmp_path)[0]["updated"] == 3000
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
    assert S.describe(folder / "meta.json", folder / "index.html", "T", tmp_path) == "A chart."
    assert "Hello" in model[0] and "secret" not in model[0]
    assert json.loads((folder / "meta.json").read_text()) == {
        "pane": "%3", "created": 5, "description": "A chart.", "generated": ["description"]}
    assert scratch.items(tmp_path)[0]["description"] == "A chart."


@pytest.mark.parametrize("meta", ['{"description": "Agent wrote this"}', "{broken", "[1]"])
def test_never_over_what_the_agent_wrote(tmp_path, model, meta):
    folder = _page(tmp_path)
    (folder / "meta.json").write_text(meta)
    assert S.describe(folder / "meta.json", folder / "index.html", "T", tmp_path) is None
    assert (folder / "meta.json").read_text() == meta


def test_never_through_a_symlinked_meta(tmp_path, model):
    folder, outside = _page(tmp_path), tmp_path / "outside.json"
    outside.write_text("{}")
    (folder / "meta.json").symlink_to(outside)
    assert S.describe(folder / "meta.json", folder / "index.html", "T", tmp_path) is None
    assert outside.read_text() == "{}"


def test_nothing_is_read_or_written_once_a_folder_points_out_of_scratch(tmp_path, model):
    root, outside = tmp_path / "scratch", _page(tmp_path, "outside")
    root.mkdir()
    (root / "p").symlink_to(outside)  # swapped after it was listed
    meta = root / "p" / "meta.json"
    assert S.describe(meta, root / "p" / "index.html", "T", root) is None and not model
    with pytest.raises(PermissionError):
        S._replace(meta, root, lambda tmp: tmp.write_text("{}"))
    assert not (outside / "meta.json").exists()


def test_the_renderer_is_served_scratch_and_nothing_else(tmp_path):
    root = tmp_path / "scratch"
    _page(root.parent, "scratch", page="<p>page</p>")
    (tmp_path / "secret.txt").write_text("outside")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")
    for name in ("leak", "leak2"):
        (root / name).mkdir()
    (root / "leak" / "index.html").symlink_to(tmp_path / "secret.txt")
    (root / "leak2" / "index.htm").symlink_to(tmp_path / "secret.txt")
    with S.serving(root) as base:
        assert base.endswith("/scratch")  # a page's /scratch/... links resolve as served
        with urlopen(f"{base}/index.html") as page:
            assert page.read() == b"<p>page</p>"
            assert page.headers["Content-Security-Policy"] == scratch.SANDBOX
        host = base.removesuffix("/scratch")
        for url in (host + "/index.html", host + "/api/digest", base + "/link.txt",
                    base + "/../secret.txt", base + "/%2e%2e/secret.txt", base + "/leak/",
                    base + "/leak2/"):
            with pytest.raises(HTTPError):
                urlopen(url)


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
    assert S.shoot("http://127.0.0.1:1/scratch/p/", out, tmp_path, 1234)
    [argv] = runs
    assert argv[0] == str(shell) and "--headless" in argv
    assert "--proxy-bypass-list=<-loopback>;127.0.0.1:1" in argv  # nothing else reachable
    profile = next(a for a in argv if a.startswith("--user-data-dir="))[16:]
    assert Path(profile).parent == Path(tempfile.gettempdir()) and not Path(profile).exists()
    assert not any(str(Path.home()) in a for a in argv)
    with Image.open(out) as im:
        assert (im.format, im.size) == ("WEBP", S.THUMB)
    assert out.stat().st_mtime == 1234  # the version it shows, not when it was saved


def test_no_bundled_shell_means_no_shot(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))  # however Chrome is on PATH
    monkeypatch.delenv("TMUXRC_THUMB_BROWSER", raising=False)
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: pytest.fail("ran a browser"))
    assert S.browser() is None and not S.shoot("http://127.0.0.1:1/", tmp_path / "t.webp", tmp_path)


def test_each_folder_version_is_worked_once(tmp_path, monkeypatch):
    done = []
    monkeypatch.setattr(S, "shoot", lambda url, out, root, at: done.append(("shot", url)))
    monkeypatch.setattr(S, "describe", lambda meta, page, title, root: done.append(("said", title)))
    monkeypatch.setattr(S, "_tried", set())
    _page(tmp_path)
    for _ in range(2):
        S.queue(tmp_path, scratch.items(tmp_path))
        while (worker := S._worker) is not None:
            worker.join()
    assert [what for what, _ in done] == ["shot", "said"] and done[0][1].endswith("/p/")
    S.queue(tmp_path, scratch.items(tmp_path), describe=False)
    assert S._worker is None  # nothing new to do


def test_a_failed_shot_still_gets_a_description(tmp_path, monkeypatch):
    said = []
    monkeypatch.setattr(S, "_tried", set())
    monkeypatch.setattr(S, "shoot", lambda *_: 1 / 0)
    monkeypatch.setattr(S, "describe", lambda *a: said.append(a[2]) or "Said.")
    _page(tmp_path)
    S.queue(tmp_path, scratch.items(tmp_path))
    while (worker := S._worker) is not None:
        worker.join()
    assert said == ["T"]


def test_a_description_missed_while_the_model_is_out_is_asked_again(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_tried", set())
    monkeypatch.setattr(S, "shoot", lambda *_: None)
    monkeypatch.setattr(S, "describe", lambda *_: None)
    monkeypatch.setattr(llm, "backing_off", lambda: True)
    _page(tmp_path)
    S.queue(tmp_path, scratch.items(tmp_path))
    while (worker := S._worker) is not None:
        worker.join()
    assert S._tried == set()
