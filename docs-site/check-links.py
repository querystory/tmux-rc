#!/usr/bin/env python3
"""Fail if any internal link in the built docs site points at a missing page.

Crawls the on-disk Hugo output (public/) — no server needed. Hugo's pretty URLs
turn page.md into a directory /section/page/, so a sibling link must be
../sibling/ not sibling/; this catches the ones that don't resolve. External
links (http[s] to other hosts), anchors, and static assets are skipped.

Run via `make docs-check`, which builds first. Exit 1 on any broken link.
"""

import html.parser
import pathlib
import sys
import urllib.parse

PUBLIC = pathlib.Path(__file__).parent / "public"
ASSET_SUFFIXES = (".js", ".css", ".svg", ".png", ".ico", ".xml", ".json",
                  ".txt", ".woff", ".woff2", ".webmanifest")


class HrefCollector(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.hrefs.append(v)


def page_url(index_html: pathlib.Path) -> str:
    """Map public/<path>/index.html -> the /<path>/ URL it serves at."""
    rel = index_html.relative_to(PUBLIC).parent.as_posix()
    return "/" if rel == "." else f"/{rel}/"


def exists(url_path: str) -> bool:
    """A pretty URL /a/b/ exists if public/a/b/index.html exists; also allow a
    direct file (public/a/b) for non-pretty outputs."""
    p = url_path.strip("/")
    if not p:
        return (PUBLIC / "index.html").exists()
    return (PUBLIC / p / "index.html").exists() or (PUBLIC / p).is_file()


def main() -> int:
    if not PUBLIC.is_dir():
        print("public/ not found — run `make docs` first", file=sys.stderr)
        return 2

    broken = []
    for index_html in PUBLIC.rglob("index.html"):
        base = page_url(index_html)
        collector = HrefCollector()
        collector.feed(index_html.read_text(encoding="utf-8"))
        for href in collector.hrefs:
            scheme = urllib.parse.urlparse(href).scheme
            if scheme in ("http", "https", "mailto", "tel"):
                continue
            if href.startswith("#"):
                continue
            path = urllib.parse.urlparse(urllib.parse.urljoin(base, href)).path
            if path.endswith(ASSET_SUFFIXES):
                continue
            if not exists(path):
                broken.append((base, href, path))

    if broken:
        print(f"✗ {len(broken)} broken internal link(s):")
        for src, href, path in sorted(broken):
            print(f"  {src}  →  {href}  (resolves to {path}, missing)")
        return 1
    print("✓ no broken internal links")
    return 0


if __name__ == "__main__":
    sys.exit(main())
