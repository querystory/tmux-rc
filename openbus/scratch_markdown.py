"""Markdown in scratch: a .md file under /scratch/ opens as a page, rendered on the server
with the extensions our internal docs site (MkDocs Material) uses, so a doc reads the same
in both, and GitHub's `> [!NOTE]` callouts work too (docs/design/scratch-markdown.md).

Raw HTML passes through: the page is sandboxed like every scratch page, where an agent
could publish any HTML and script anyway. Nothing that reads other files (snippets) is
on, since the renderer runs in the daemon."""

import re
from functools import lru_cache
from html import escape, unescape
from pathlib import Path
from urllib.parse import parse_qs

import anyio
import markdown
from markdown.blockprocessors import BlockProcessor
from markdown.extensions import Extension
from pygments.formatters import HtmlFormatter
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

_PAGE = Path(__file__).resolve().parent / "scratch_markdown.html"
MERMAID = "/m/vendor/mermaid.min.js"  # the vendored build, under the app's static dir
# The app's colours, and its account chip so the gallery's cards wear the same one.
_TOKENS = re.compile(r"^(?::root(?:\.light)? \{.*?^\}|\.acct-(?:badge|dot)\b[^\n]*)",
                     re.MULTILINE | re.DOTALL)
_CALLOUT = re.compile(r"^ {0,3}> ?\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\] *$",
                      re.IGNORECASE | re.MULTILINE)
_QUOTED = re.compile(r" {0,3}> ?")
# GitHub's CAUTION is red, Material's caution orange: it becomes a danger titled Caution.
_ADMONITION = {"caution": 'danger "Caution"'}
# Highlighting is done here, so the page carries Pygments' classes for both themes, each
# scoped to its own: a token one style colours and the other leaves alone must not leak.
_PYGMENTS = (HtmlFormatter(style="github-dark").get_style_defs(":root:not(.light) .highlight")
             + "\n"
             + HtmlFormatter(style="default").get_style_defs(".light .highlight"))


def tokens(style: Path) -> str:
    """The app's colour tokens from its stylesheet, for a scratch page to inline: sandboxed,
    it cannot count on a credentialed stylesheet load behind the front door."""
    return "\n".join(_TOKENS.findall(style.read_text()))


class _Callouts(BlockProcessor):
    """GitHub's `> [!NOTE]` callout, handed back to the parser as the `!!! note` admonition
    it mirrors. A block processor, so it sees what Markdown parses (inside a markdown="1"
    container too) and never code or literal HTML, which are stashed by then."""

    def test(self, _parent, block):
        return bool(_CALLOUT.search(block))

    def run(self, _parent, blocks):
        block = blocks.pop(0)
        m = _CALLOUT.search(block)
        lines = block[m.end():].split("\n")[1:]
        n = next((i for i, line in enumerate(lines) if not _QUOTED.match(line)), len(lines))
        body = "".join("\n    " + _QUOTED.sub("", line, count=1) for line in lines[:n])
        kind = m[1].lower()
        admonition = f"!!! {_ADMONITION.get(kind, kind)}{body}"
        blocks[:0] = [b for b in (block[:m.start()].rstrip(), admonition, "\n".join(lines[n:]))
                      if b.strip()]


class _GitHub(Extension):
    def extendMarkdown(self, md):  # noqa: N802 - Python-Markdown's name
        md.parser.blockprocessors.register(_Callouts(md.parser), "github_callouts", 21)


def _mermaid(source, *_args, **_kwargs):
    """A ```mermaid fence, as the <pre class="mermaid"> mermaid.js draws in the browser."""
    return f'<pre class="mermaid">{escape(source)}</pre>'


_EXTENSIONS = ["tables", "attr_list", "md_in_html", "footnotes", "def_list", "sane_lists",
               "toc", "admonition", "pymdownx.details", "pymdownx.superfences",
               "pymdownx.highlight", "pymdownx.tasklist", _GitHub()]
_CONFIG = {"toc": {"permalink": "¶"},
           "pymdownx.superfences": {"custom_fences": [
               {"name": "mermaid", "class": "mermaid", "format": _mermaid}]}}


@lru_cache(maxsize=64)
def _render(path: str, _mtime: float, _size: int) -> tuple[str, str, str | None]:
    md = markdown.Markdown(extensions=_EXTENSIONS, extension_configs=_CONFIG)
    source = Path(path).read_text(errors="replace")
    return source, md.convert(source), unescape(md.toc_tokens[0]["name"]) if md.toc_tokens else None


def render(path: Path) -> tuple[str, str, str | None]:
    """The file's Markdown, its HTML and its first heading (None without one), kept until
    the file changes."""
    st = path.stat()
    return _render(str(path), st.st_mtime, st.st_size)


def page(path: Path, web: Path) -> str:
    """The whole page for `path`: its HTML in a readable column on the app's colours (from
    `web`, the app's static dir), the Markdown kept for copying, and mermaid.js loaded
    only when there is a diagram to draw."""
    source, body, title = render(path)
    script = f'<script src="{MERMAID}"></script>' if 'class="mermaid"' in body else ""
    return (_PAGE.read_text().replace("/*tokens*/", tokens(web / "m" / "style.css"))
            .replace("/*pygments*/", _PYGMENTS).replace("<!--title-->", escape(title or path.stem))
            .replace("<!--source-->", escape(source))
            .replace("<!--mermaid-->", script).replace("<!--body-->", body))


class Files(StaticFiles):
    """scratch's files, with a .md served as its page (`?raw` for the file itself), a
    folder's index.md as its index when it has no index.html, and, while it has neither,
    what `pending` makes of the folder's name (scratch.placeholder), as 202 Accepted:
    an agent checking for a 200 is not told its page is there. Anything hidden (a .git,
    an .env) is not part of the page, as in the gallery, and is not served."""

    def __init__(self, *, web: Path, pending=lambda _name: None, **kwargs):
        super().__init__(**kwargs)
        self.web, self.pending = web, pending

    def lookup_path(self, path):
        if any(part.startswith(".") for part in Path(path).parts):
            return "", None
        full, st = super().lookup_path(path)
        if st is None and Path(path).name == "index.html":
            return super().lookup_path(path.removesuffix("html") + "md")
        return full, st

    async def get_response(self, path, scope):
        try:
            response = await super().get_response(path, scope)
        except HTTPException as e:
            wait = e.status_code == 404 and "/" not in path and await anyio.to_thread.run_sync(
                self.pending, path)
            if not wait:
                raise
            return HTMLResponse(wait, status_code=202)
        file = Path(getattr(response, "path", ""))
        if file.suffix != ".md":
            return response
        if "raw" in parse_qs(scope["query_string"].decode(), keep_blank_values=True):
            response.headers["content-type"] = "text/plain; charset=utf-8"
            return response
        return HTMLResponse(await anyio.to_thread.run_sync(page, file, self.web))
