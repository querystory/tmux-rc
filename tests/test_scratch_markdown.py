"""Markdown in scratch (openbus/scratch_markdown.py): what it renders, how /scratch/ and the
thumbnailer serve it, and what the gallery makes of a .md page."""

import os
from urllib.request import urlopen

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
    assert '<a href="next.md">the next page</a>' in html  # the next rendered page, as written
    assert 'class="headerlink"' in html


def test_a_page_loads_mermaid_only_for_a_diagram(tmp_path):
    page = M.page(_doc(tmp_path), WEB_DIR)
    assert f'<script src="{M.MERMAID}">' in page and "<title>Launch plan &amp; notes" in page
    assert "--accent:" in page and "Copy formatted" in page
    assert "&gt; [!NOTE]" in page  # the Markdown, kept for "Copy Markdown"
    plain = M.page(_doc(tmp_path, "plain.md", "Just text."), WEB_DIR)
    assert M.MERMAID not in plain and "<title>plain</title>" in plain
    assert (WEB_DIR / M.MERMAID.removeprefix("/")).is_file()


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
