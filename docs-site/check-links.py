#!/usr/bin/env python3
"""Fail on broken internal links or blank pages in the built docs site.

Crawls the on-disk Hugo output (serve/, what the daemon serves) — no server needed.
Two checks: (1) every internal link resolves to a real built page; (2) no content
page has an empty <main> (the shadowed-_index.md failure). External links, anchors,
static assets, and daemon-served paths outside the site prefix are skipped.

Run via `make docs-check`, which builds first. Exit 1 on any failure.
"""

import html.parser
import pathlib
import sys
import urllib.parse

# The daemon-served build (make docs -> serve/), not Hugo's dev-server public/.
PUBLIC = pathlib.Path(__file__).parent / "serve"
ASSET_SUFFIXES = (".js", ".css", ".svg", ".png", ".ico", ".xml", ".json",
                  ".txt", ".woff", ".woff2", ".webmanifest")


class PageParser(html.parser.HTMLParser):
    """Collect <a href> targets, the page's canonical path (which carries the baseURL
    prefix, e.g. /docs/design/foo/ — so the checker works whether the site was built at
    / or /docs/), and the rendered text inside <main> (to catch blank pages: a section
    whose _index.md got shadowed builds to just an <h1> with no body)."""

    # HTML void elements never emit an end tag, so they must NOT push onto the
    # open-tag stack — otherwise the stack never unwinds and <main> looks unclosed,
    # leaking footer/nav text into body_text (a page with a blank <main> but any
    # <input>/<img> would then score as non-blank — defeating the whole check).
    VOID = frozenset((
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    ))

    def __init__(self):
        super().__init__()
        self.hrefs = []
        self.canonical = None
        self.body_text = []
        self._depth = 0  # open (non-void) tags nested inside <main>, incl. <main>

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a" and d.get("href"):
            self.hrefs.append(d["href"])
        elif tag == "link" and d.get("rel") == "canonical" and d.get("href"):
            self.canonical = d["href"]
        if (tag == "main" or self._depth) and tag not in self.VOID:
            self._depth += 1

    def handle_endtag(self, tag):
        if self._depth and tag not in self.VOID:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth:
            self.body_text.append(data)

    def main_text(self) -> str:
        """All rendered text inside <main>. A shadowed section page is *only* its
        auto-generated <h1> (a handful of chars), which falls under MIN_BODY — so we
        don't need to strip the heading to tell blank from real content."""
        return "".join(self.body_text).strip()


def file_path_for(url_path: str, prefix: str) -> pathlib.Path | None:
    """Map a served URL path back to the serve/ file that answers it, stripping the
    baseURL prefix (e.g. /docs). Returns the index.html / direct file, or None if the
    URL isn't under the prefix at all."""
    p = url_path
    if prefix and p.startswith(prefix):
        p = p[len(prefix):]
    p = p.strip("/")
    if not p:
        return PUBLIC / "index.html"
    for cand in (PUBLIC / p / "index.html", PUBLIC / p):
        if cand.is_file():
            return cand
    return None


def main() -> int:
    if not PUBLIC.is_dir():
        print("serve/ not found — run `make docs` first", file=sys.stderr)
        return 2

    # Hextra auto-generates these list pages with little/no body text — legitimately
    # sparse, so they're exempt from the blank-page check.
    TAXONOMY = ("tags", "categories")
    MIN_BODY = 100  # chars of <main> text below which a content page is "blank"

    broken = []
    blank = []
    for index_html in PUBLIC.rglob("index.html"):
        parser = PageParser()
        parser.feed(index_html.read_text(encoding="utf-8"))
        # The canonical link is the page's own served URL, incl. any baseURL prefix.
        # Resolve relative hrefs against it, and derive the prefix to strip on lookup.
        base = parser.canonical or "/"
        rel = index_html.relative_to(PUBLIC).parent.as_posix()
        file_url = "/" if rel == "." else f"/{rel}/"
        # The prefix is what canonical carries beyond the file's own path — canonical
        # /docs/design/x/ minus file_url /design/x/ = /docs. For the root page file_url
        # is "/", so the prefix is the whole canonical minus its trailing slash.
        if file_url == "/":
            prefix = base.rstrip("/")
        elif base.endswith(file_url):
            prefix = base[: -len(file_url)]
        else:
            prefix = ""
        for href in parser.hrefs:
            scheme = urllib.parse.urlparse(href).scheme
            if scheme in ("http", "https", "mailto", "tel"):
                continue
            if href.startswith("#"):
                continue
            path = urllib.parse.urlparse(urllib.parse.urljoin(base, href)).path
            if path.endswith(ASSET_SUFFIXES):
                continue
            # A root-relative link that lands outside the site prefix (e.g. /apidocs
            # when the site is at /docs/) is served by the daemon, not the Hugo build —
            # not ours to validate.
            if prefix and not path.startswith(prefix + "/") and path != prefix:
                continue
            if file_path_for(path, prefix) is None:
                broken.append((base, href, path))

        served = urllib.parse.urlparse(base).path
        last_seg = served.rstrip("/").rsplit("/", 1)[-1]
        if last_seg not in TAXONOMY and len(parser.main_text()) < MIN_BODY:
            blank.append(served)

    if blank:
        print(f"✗ {len(blank)} blank page(s) (empty <main> — shadowed _index.md?):")
        for path in sorted(blank):
            print(f"  {path}")

    if broken:
        print(f"✗ {len(broken)} broken internal link(s):")
        for src, href, path in sorted(broken):
            print(f"  {src}  →  {href}  (resolves to {path}, missing)")

    if broken or blank:
        return 1
    print("✓ no broken links, no blank pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
