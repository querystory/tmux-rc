"""Markdown in scratch (openbus/scratch_markdown.py): what it renders, how /scratch/ and the
thumbnailer serve it, and what the gallery makes of a .md page."""

import os
import re
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from openbus import llm, scratch
from openbus import scratch_markdown as M
from openbus import scratch_sidecars as S
from openbus.server import WEB_DIR

DOC = """# Launch *plan* & notes

| Step | Owner |
|------|-------|
| Draft | Ada |

```mermaid
flowchart LR
  A --> B
```

!!! warning "Mind the gap"
    Between trains.

> [!NOTE]
> Callouts work, **with Markdown**.
>
> Over two paragraphs.

```text
> [!TIP]
> quoted in code, left alone
```

- [x] Written
- [ ] Reviewed

<details><summary>More</summary>Raw HTML stays.</details>

<pre>
> [!WARNING]
> literal, left alone
</pre>

<div markdown="1">
> [!IMPORTANT]
> Inside a container.
</div>

Para before
> [!CAUTION]
> Hot.

See [the next page](next.md).
"""


def _doc(root, name="notes.md", text=DOC):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_the_docs_sites_markdown_and_githubs_callouts_render(tmp_path):
    source, html, title = M.render(_doc(tmp_path))
    assert source == DOC and title == "Launch plan & notes"
    assert "<th>Step</th>" in html and "<td>Ada</td>" in html
    assert '<pre class="mermaid">flowchart LR\n  A --&gt; B</pre>' in html
    assert '<div class="admonition warning">\n<p class="admonition-title">Mind the gap' in html
    assert '<div class="admonition note">\n<p class="admonition-title">Note</p>' in html
    assert "<p>Callouts work, <strong>with Markdown</strong>.</p>\n<p>Over two" in html
    assert "[!TIP]" in html and "admonition tip" not in html  # code is code
    assert '<input type="checkbox" disabled checked/> Written' in html
    assert "<details><summary>More</summary>Raw HTML stays.</details>" in html
    assert "<pre>\n> [!WARNING]\n> literal, left alone\n</pre>" in html
    assert '<div class="admonition important">' in html and "<p>Inside a container.</p>" in html
    assert ('<p>Para before</p>\n<div class="admonition danger">\n'
            '<p class="admonition-title">Caution') in html
    assert '<a href="next.md">the next page</a>' in html  # the next rendered page, as written
    assert 'class="headerlink"' in html


def test_a_list_straight_under_text_starts_as_on_github(tmp_path):
    text = ("**Where.** the garden shed:\n- rakes\n- spades\n  still a spade\n\n"
            "Steps:\n1. water\n2. wait\n\n"
            "- tight\nlazy line\n- still tight\n## Next\nThen:\n- plums\n\n"
            "The harvest of\n2026. was a good year\n- apples\n\n"
            "```text\nfenced\n- left alone\n```\n\n"
            "| Tool | Shelf |\n|------|-------|\n| hoe | top |\n")
    html = M.render(_doc(tmp_path, text=text))[1]
    assert "<p><strong>Where.</strong> the garden shed:</p>\n<ul>\n<li>rakes</li>" in html
    assert "<li>spades\n  still a spade</li>\n</ul>" in html
    assert "<p>Steps:</p>\n<ol>\n<li>water</li>\n<li>wait</li>\n</ol>" in html
    assert "<li>tight\nlazy line</li>\n<li>still tight</li>" in html
    assert "<p>Then:</p>\n<ul>\n<li>plums</li>" in html
    assert "<p>The harvest of\n2026. was a good year</p>\n<ul>\n<li>apples</li>" in html
    assert "fenced\n- left alone" in html and "<td>hoe</td>" in html


def test_a_page_loads_mermaid_only_for_a_diagram(tmp_path):
    page = M.page(_doc(tmp_path), WEB_DIR)
    assert f'<script src="{M.MERMAID}">' in page and "<title>Launch plan &amp; notes" in page
    assert "--accent:" in page and "Copy formatted" in page
    assert "<textarea hidden>" in page and "&gt; [!NOTE]" in page  # kept for Copy Markdown
    plain = M.page(_doc(tmp_path, "plain.md", "Just text."), WEB_DIR)
    assert M.MERMAID not in plain and "<title>plain</title>" in plain
    assert (WEB_DIR / M.MERMAID.removeprefix("/")).is_file()



def test_the_theme_is_set_before_the_body_and_light_unless_asked_for_dark(tmp_path):
    # Raw HTML passes through, and an unclosed <title> swallows everything after it as text.
    page = M.page(_doc(tmp_path, text='"Ready: <title>" gets its own slot.'), WEB_DIR)
    assert page.index('!matchMedia("(prefers-color-scheme: dark)")') < page.index("</head>")
    assert "prefers-color-scheme: light" not in page

def test_an_export_is_the_daemons_pages_as_static_files(tmp_path):
    src, out = tmp_path / "doc", tmp_path / "out"
    _doc(src, "index.md")
    _doc(src, "deep/more.md", "Just text.")
    _doc(src, "both.md", "# Markdown")
    (src / "both.html").write_text("html wins")
    (src / "meta.json").write_text('{"title": "Launch plan"}')
    _doc(src, ".env", "SECRET=1")
    _doc(src, ".git/index.md", "# Not a page")
    scratch.export(src, out, WEB_DIR)
    page = (out / "index.html").read_text()
    assert page == M.page(src / "index.md", WEB_DIR, app="_app/", raw="index.md")
    assert '<script src="_app/m/vendor/mermaid.min.js">' in page and 'href="index.md"' in page
    assert 'href="../_app/scratch.svg"' in (out / "deep/more.html").read_text()
    assert (out / "index.md").read_text() == DOC and (out / "both.html").read_text() == "html wins"
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "_app/m/vendor/mermaid.min.js", "_app/scratch.svg", "both.html", "both.md",
        "deep/more.html", "deep/more.md", "index.html", "index.md"]
    assert not re.search(r"(src|href)=\"/", page)  # nothing at the daemon's own paths
    with pytest.raises(SystemExit, match="not empty"):  # it would keep what the folder lost
        scratch.export(src, out, WEB_DIR)
    with pytest.raises(SystemExit, match="is not a folder"):  # a typo in its name
        scratch.export(tmp_path / "nowhere", tmp_path / "x", WEB_DIR)
    (src / "deep/more.html").symlink_to(src / ".env")  # a symlink is never exported
    (src / "settings.txt").symlink_to(src / ".env")
    scratch.export(src, tmp_path / "again", WEB_DIR)
    assert "Just text." in (tmp_path / "again/deep/more.html").read_text()
    assert not (tmp_path / "again/settings.txt").exists()
    _doc(src, "_app/logo.svg", "<svg/>")
    with pytest.raises(SystemExit, match="_app"):
        scratch.export(src, tmp_path / "third", WEB_DIR)


def test_a_rewrite_shows_on_reload(tmp_path):
    path = _doc(tmp_path, text="# One")
    assert M.render(path)[2] == "One"
    path.write_text("# Two, longer")
    assert M.render(path)[2] == "Two, longer"


def test_scratch_serves_markdown_as_its_page(tmp_path):
    _doc(tmp_path)
    _doc(tmp_path, "site/index.md", "# Site")
    _doc(tmp_path, "both/index.md", "# Markdown")
    (tmp_path / "both" / "index.html").write_text("html wins")
    files = M.Files(directory=tmp_path, html=True, web=WEB_DIR)
    c = TestClient(Starlette(routes=[Mount("/", files)]))
    page = c.get("/notes.md")
    assert page.headers["content-type"].startswith("text/html") and "<th>Step</th>" in page.text
    raw = c.get("/notes.md?raw")
    assert raw.headers["content-type"] == "text/plain; charset=utf-8" and raw.text == DOC
    assert "<h1" in c.get("/site/").text and c.get("/both/").text == "html wins"
    assert c.get("/nothing/").status_code == 404


def test_the_thumbnailer_renders_markdown_as_the_daemon_does(tmp_path):
    _doc(tmp_path / "doc", "index.md")
    (tmp_path / "doc" / "index.htm").write_text("not the page the gallery opens")
    with S.serving(tmp_path, WEB_DIR) as base:
        with urlopen(f"{base}/doc/") as page:
            assert page.read().decode() == M.page(tmp_path / "doc" / "index.md", WEB_DIR)
            assert page.headers["Content-Security-Policy"] == scratch.SANDBOX
        with urlopen(base.removesuffix("/scratch") + M.MERMAID) as script:
            assert script.read(100) == (WEB_DIR / "m/vendor/mermaid.min.js").read_bytes()[:100]


def test_the_gallery_reads_a_markdown_page(tmp_path, monkeypatch):
    _doc(tmp_path / "doc", "index.md")
    _doc(tmp_path / "doc", "older.md", "# Older")
    os.utime(tmp_path / "doc" / "older.md", (1, 1))
    _doc(tmp_path / "loose", "draft.md", "No heading here.")
    items = {i["name"]: i for i in scratch.items(tmp_path)}
    assert items["doc"]["title"] == "Launch plan & notes" and items["doc"]["href"] == "doc/"
    assert items["loose"]["title"] == "loose" and items["loose"]["href"] == "loose/draft.md"
    assert S.stale(items["doc"])  # shot like any page
    asked = []
    monkeypatch.setattr(llm, "one_sentence", lambda prompt: asked.append(prompt) or "Plans.")
    S.describe(tmp_path / "doc" / "meta.json", items["doc"]["page"], "T", tmp_path)
    assert "Mind the gap Between trains." in asked[0] and "!!!" not in asked[0]


def test_download_saves_a_folder_page_as_a_file_named_after_it(tmp_path):
    _doc(tmp_path, "notes/index.md")
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<p>site</p>")
    files = M.Files(directory=tmp_path, html=True, web=WEB_DIR)
    c = TestClient(Starlette(routes=[Mount("/", files)]))
    page = c.get("/site/?download=1")
    assert page.headers["content-disposition"] == "attachment; filename*=UTF-8''site.html"
    assert page.text == "<p>site</p>" and "content-disposition" not in c.get("/site/").headers
    md = c.get("/notes/?download=1")  # rendered, not the Markdown
    assert md.headers["content-disposition"].endswith("notes.html") and "<th>Step</th>" in md.text
    (tmp_path / ".archive" / "old").mkdir(parents=True)
    (tmp_path / ".archive" / "old" / "index.html").write_text("old")
    old = c.get("/.archive/old/?download=1").headers["content-disposition"]
    assert old.endswith("''old.html")
    (tmp_path / "site" / "report.pdf").write_bytes(b"%PDF")
    assert c.get("/site/report.pdf?download=1").headers["content-disposition"].endswith("site.pdf")
    pending = M.Files(directory=tmp_path, html=True, web=WEB_DIR, pending=lambda _n: "making")
    c = TestClient(Starlette(routes=[Mount("/", pending)]))
    assert c.get("/next/?download=1").status_code == 204  # a placeholder: the app stays put
