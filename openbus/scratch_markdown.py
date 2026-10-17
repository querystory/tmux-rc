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
from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor
from pygments.formatters import HtmlFormatter
from starlette.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

_PAGE = Path(__file__).resolve().parent / "scratch_markdown.html"
MERMAID = "/m/vendor/mermaid.min.js"  # the vendored build, under the app's static dir
_TOKENS = re.compile(r"^:root(?:\.light)? \{.*?^\}", re.MULTILINE | re.DOTALL)  # app colours
_CALLOUT = re.compile(r" {0,3}> ?\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\] *", re.IGNORECASE)
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


class _Callouts(Preprocessor):
    """GitHub's `> [!NOTE]` callout, rewritten as the `!!! note` admonition it mirrors.
    Fenced code is already stashed by now, so a callout quoted in one stays as written."""

    def run(self, lines):
        out, inside = [], False
        for line in lines:
            if inside and _QUOTED.match(line):
                out.append("    " + _QUOTED.sub("", line, count=1))
                continue
            if inside:
                out.append("")  # the admonition ends with the quote
            inside = bool(m := _CALLOUT.fullmatch(line))
            out += ["", f"!!! {_ADMONITION.get(m[1].lower(), m[1].lower())}"] if m else [line]
        return out


class _GitHub(Extension):
    def extendMarkdown(self, md):  # noqa: N802 - Python-Markdown's name
        md.preprocessors.register(_Callouts(md), "github_callouts", 24)  # after fences (25)


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
    """scratch's files, with a .md served as its page (`?raw` for the file itself) and a
    folder's index.md as its index when it has no index.html."""

    def __init__(self, *, web: Path, **kwargs):
        super().__init__(**kwargs)
        self.web = web

    def lookup_path(self, path):
        full, st = super().lookup_path(path)
        if st is None and Path(path).name == "index.html":
            return super().lookup_path(path.removesuffix("html") + "md")
        return full, st

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        file = Path(getattr(response, "path", ""))
        if file.suffix != ".md":
            return response
        if "raw" in parse_qs(scope["query_string"].decode(), keep_blank_values=True):
            response.headers["content-type"] = "text/plain; charset=utf-8"
            return response
        return HTMLResponse(await anyio.to_thread.run_sync(page, file, self.web))
