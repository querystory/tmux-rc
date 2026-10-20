---
weight: 4
title: "Scratch gallery"
---

# Scratch gallery

Agents publish previews to scratch: HTML pages, Markdown documents, mocks, reports,
charts, images, and built sites. Each one is a folder in the directory `TMUXRC_SCRATCH_DIR` names, served at
`/scratch/<folder>/`. The gallery at `/scratch/` lists every folder, most recently changed
first. A card shows a thumbnail, the title, one line of description, when the folder was
made and changed, and who made it. Tap the title to open the page. Tap where it was made to
jump to that pane while it is still open. Type in the filter box, or tap an agent's chip,
to narrow the list. Grid and List switch between cards across the full width and compact
rows with a small thumbnail. The app shows the gallery as Scratch, in the wide sidebar
and in a phone's ☰ menu. There a card opens full screen over the app. Back, Escape or a
swipe from the left edge returns to the gallery, and "Open in browser" opens the page in
a tab of its own. Settings links to the gallery's own page under More.

## Setting it up

The gallery needs two things: a scratch directory the daemon serves, and agents that know
about it.

1. Serve the directory: set `TMUXRC_SCRATCH_DIR` in the daemon's `.env` and restart it,
   as in [Previewing work](previews.md#quick-start).
2. Tell agents where it is. With `TMUXRC_SCRATCH_ADVERTISE=1`, and `TMUXRC_SCRATCH_URL`
   set to your tmux-rc URL plus `/scratch`, the daemon exports `TMUXRC_SCRATCH_DIR`,
   `TMUXRC_SCRATCH_URL` and `TMUXRC_SCRATCH_LOCAL_URL` to new tmux panes
   ([details](previews.md#telling-agents-about-it)).
3. Tell agents what to do with it: paste the snippet below into their global
   instructions, such as `AGENTS.md` or `CLAUDE.md`
   ([one file for every agent](previews.md#one-instruction-file-for-every-agent)).

## The agent snippet

    If TMUXRC_SCRATCH_DIR is set, you can show the user a preview (an HTML page, a
    Markdown document, a built static site, a report, images, a PDF). Write it into a new
    subfolder as index.md (Markdown, with Mermaid diagrams, admonitions, task lists and
    tables) or index.html, for example $TMUXRC_SCRATCH_DIR/<folder>/index.md, and give the user
    $TMUXRC_SCRATCH_URL/<folder>/ (or the folder's path if that variable is unset). The
    public URL sits behind the user's login, so to check the page yourself request
    $TMUXRC_SCRATCH_LOCAL_URL/<folder>/ instead; a 200 means it is served. Everything in
    that directory is visible to everyone who can reach tmux-rc: never put secrets or
    private data there. Pages are sandboxed: their scripts cannot fetch, use XHR or
    WebSockets, or submit forms, so inline any data a page needs. Beside the page, write
    meta.json: {"title", "description", "pane": "$TMUX_PANE", "created": ISO 8601 time}.

## Markdown pages

A `.md` file opens as a rendered page, and a folder whose page is `index.md` is served at
`/scratch/<folder>/` (an `index.html` beside it wins). The Markdown is the same dialect as
an MkDocs Material docs site, plus GitHub's callouts:

- tables, task lists (`- [x]`), footnotes, definition lists, and heading anchors;
- fenced code, highlighted on the server, and ```` ```mermaid ```` fences, drawn as
  diagrams in the page's light or dark theme;
- admonitions, `!!! note "Title"`, collapsible ones with `???`, and GitHub's
  `> [!NOTE]`, `[!TIP]`, `[!IMPORTANT]`, `[!WARNING]` and `[!CAUTION]`;
- raw HTML, such as `<details>`, with `markdown="1"` to render Markdown inside it.

Links and images stay as written, so a relative link to another `.md` opens that page
rendered. The toolbar copies the Markdown, or the formatted page for pasting into a mail
or a doc, and Source (`?raw`) shows the file itself. The page is sandboxed like any other
scratch page. Nothing needs setting up: the renderer ships with the daemon, and
mermaid.js is served from the app's own files. The
[design note](../design/scratch-markdown.md) explains the choices.

## What a card shows

A folder describes itself in an optional `meta.json` beside its page: `title`,
`description`, `pane` (the agent's `$TMUX_PANE`) and `created` (ISO 8601). Without one,
the title is the page's own `<title>` (a Markdown page's first heading), else the folder's
name, and the times are its files'
oldest and newest. A malformed `meta.json` counts as absent.

tmux-rc fills in the rest in the background, so the gallery never waits for it:

- **Thumbnails.** If Playwright's headless Chromium is installed (`make screenshots`
  installs it, or run `npx playwright install chromium-headless-shell`;
  `TMUXRC_THUMB_BROWSER` names another binary), each page is shot into a `thumb.webp`
  beside it. It is shot again after the page changes. Until then the card says the preview
  is being made, or that there is none.
- **Descriptions.** With the parse model configured, a folder without a description gets
  one sentence, written into its `meta.json` and marked `"generated": ["description"]`.
  A description already there is never replaced.
- **Who made it.** While the pane named in `meta.json` is open, the card shows its
  session and window, its agent and its account, and links to it. Otherwise tmux-rc asks
  [agent-history](https://github.com/querystory/tmux-rc/tree/main/agent-history) which
  session was active when the folder appeared and mentioned its path. If several were,
  it picks the most recently started one, since a long-running session that only passed
  the link on started earlier. It refreshes the index first, because a session that is
  still running can have an index entry days out of date. With no answer, the
  card says "unknown". That search reads transcripts, so the first visit after a restart
  can take a few seconds. Its answers are kept until the next restart.

Pages are shot offline. Each shot uses a throwaway browser profile and a listener that
serves scratch and nothing else, so a page can't put anything but scratch into its
thumbnail. The [design note](../design/scratch-previews.md) explains why.
