# Markdown in scratch

Status: implemented (`openbus/scratch_markdown.py`). How to use it:
[Scratch gallery](../deploy/scratch-gallery.md#markdown-pages).

## Why

Agents write Markdown far more readily than HTML: a plan, a review, a design note with a
diagram or two. Until now, publishing one to [scratch](scratch-previews.md) meant either
hand-wrapping it in HTML or serving the raw file, which a phone shows as plain text with
the Mermaid source where the diagram should be. So a `.md` file under `/scratch/` now opens
as a rendered page, and a folder whose page is `index.md` works like one with an
`index.html`.

## What "full-featured" means

The bar is our internal docs site, built with MkDocs Material. Counting what its pages
actually use, rather than what its configuration enables, gave the list: tables, fenced
code in a dozen languages, Mermaid (flowcharts, sequence, state, Gantt and ER diagrams,
over two hundred of them), task lists, `!!! note "Title"` admonitions and `???`
collapsible ones, raw HTML such as `<details>` with `markdown="1"` inside, and relative
links to other `.md` files and images. A document dropped into scratch from that site
should look the same as it does there.

Agents, though, write GitHub-flavoured Markdown, whose callouts are `> [!NOTE]` rather
than `!!!`. Both are supported: a GitHub callout becomes the admonition it mirrors. Where
the two disagree on colour (GitHub's CAUTION is red, Material's caution orange), the
GitHub syntax keeps GitHub's meaning.

Math, content tabs, front matter and file includes are enabled on the docs site but
unused, so they are left out until someone needs them. Footnotes, definition lists and
heading anchors came free with the same engine, so they are in.

## Options considered

**Python-Markdown with pymdown-extensions, on the server (chosen).** It is the engine the
docs site runs, with the same extensions, so `!!!`, `???`, `markdown="1"` and task lists
come out the same by construction rather than by imitation. Code is highlighted on the
server with Pygments, so a page needs no script at all unless it has a diagram. The
gallery gets real HTML to read titles and descriptions from, and the thumbnailer shoots
the same page a person sees. The dependencies are small and pure Python. The cost is a
second Markdown engine next to the chat's markdown-it, but the two have different jobs:
chat renders untrusted model output with HTML off, while scratch documents are written by
the user's own agents and published deliberately.

**markdown-it in the browser.** It is already vendored for chat, and the server would do
nothing. But the sandbox forbids `fetch`, so the server would have to inline the Markdown
into a page anyway, and `!!!` and `markdown="1"` would need custom plugins that imitate
MkDocs and drift from it. Highlighting would mean another bundle, and the gallery's
descriptions and thumbnails would depend on scripts running.

**The goldmark renderer an internal Go service already ships.** It exists and works, but
it lives in a different service in a different language, renders GitHub-flavoured
Markdown only (no highlighting, no admonitions) and drops raw HTML. Its one idea worth
keeping was taken: rewrite Mermaid fences for a locally served mermaid.js.

**Running MkDocs on each folder.** An exact match, but a whole-site build for one file,
with a heavy dependency and a build step between an agent's write and the reload. Scratch
pages are rewritten every few minutes; that is the wrong shape.

## How it works

A request for a `.md` file is rendered and returned as HTML, under the same sandbox policy
as every other `/scratch/` response. `?raw` returns the file itself as plain text, and
the page's Source button links there. The rendering is cached by path, modification time
and size, so a rewrite shows on the next reload and an unchanged file costs nothing. The
same function serves the thumbnailer's private loopback listener, so a thumbnail is of
exactly what the browser shows.

Links and images are left as written. A relative link to another `.md` lands on that
page, rendered; a relative image loads from the same folder. That is how the docs site
behaves too.

The page is a readable column on the app's own colour tokens, inlined as the gallery
inlines them, following the system's light or dark setting. Admonitions use Material's
colours by type; Pygments' styles are included for both themes, each scoped to its own,
so a token one style colours and the other leaves alone cannot leak across. Wide tables
and code scroll inside themselves, so a phone never scrolls the page sideways.

The toolbar offers Copy Markdown, Copy formatted (HTML and plain text together, for
pasting into a mail or a doc) and Source. A sandboxed page may be refused the async
clipboard, so copying falls back to a copy event, which the sandbox allows.

## Mermaid

A Mermaid fence becomes a `<pre class="mermaid">`, which mermaid.js draws in the browser,
as Material does. mermaid.js is vendored with the app's other browser libraries: pinned in
`package.json`, its npm integrity hash recorded in `web/m/vendor/versions.json`, checked
byte for byte in CI, with its own and its bundled dependencies' licenses copied beside
it. It is about 3.5 MB, so it loads only on a page that has a diagram, and it is drawn in
the theme that matches the page.

It is loaded as a classic script, not a module. A sandboxed page has an opaque origin, so
every request it makes is cross-origin; a module script is fetched with CORS and the
daemon sends no CORS headers, while a classic script is an ordinary resource load, like an
image. Headless checks confirm the page's origin is `null` and both diagrams of a sample
draw. Server-side rendering of diagrams was not considered worth it: it would need a
headless browser per render.

## Safety

The rendered page lives under the same sandbox as every scratch page: an opaque origin,
no `fetch`, no form posts. Agents can already publish arbitrary HTML and script there, so
letting raw HTML through Markdown adds no capability, and the docs site's `<details>`
blocks need it. Sanitizing would only break documents without protecting anything.

What does matter is the server side, since the renderer runs inside the daemon. Nothing
in a document is executed while rendering, and the extensions that read other files
(snippets, includes) are off, so a document cannot pull a file from outside scratch into a
page.

## Not done

The Hugo docs under `/docs/` already render their Markdown at build time and are left
alone. Version control for scratch folders is a separate question.
