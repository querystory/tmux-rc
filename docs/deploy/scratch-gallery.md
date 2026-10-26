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
    $TMUXRC_SCRATCH_LOCAL_URL/<folder>/ instead; exactly 200 means it is served (a 202 is a
    page still in progress). Everything in
    that directory is visible to everyone who can reach tmux-rc: never put secrets or
    private data there. Pages are sandboxed: their scripts cannot fetch, use XHR or
    WebSockets, or submit forms, so inline any data a page needs. Write the folder's
    meta.json ({"title", "description", "pane": "$TMUX_PANE", "created": ISO 8601 time})
    first, before the page: until index.html or index.md lands, the link shows that the
    page is in progress, so it is safe to hand out early, and the user is notified when it
    is ready.

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

## A link handed out before its page

Agents often hand out a link before the page behind it exists ("a subagent is building it,
it will be at `/scratch/x/`"). A folder whose `meta.json` came first counts as **in
progress**, and it is done once its `index.html` or `index.md` lands. A site may write
its assets ahead of its index: while files keep landing (within the last minute) it is
still in progress, and a folder of loose files older than that, or with any page
(`.html`, `.md`) other than its index, is a finished publish, whose bare URL 404s as before. There
is no status field: "page present means done" can't be forgotten by an agent that
crashes, and it survives a daemon restart, since the state is read from the folder itself.

- **The link** answers **202** with a placeholder: the title and description, who is making
  it (linked to the pane), that pane's current headline, a spinner and how long ago it
  started. It reloads itself, every 5 seconds and every 30 after ten minutes, so it turns
  into the page once that lands. A 202 passes `curl -f`, `raise_for_status()` and
  `fetch().ok`, so the snippet says exactly 200; the placeholder also sends `Retry-After` and
  `X-Scratch-Pending: 1`, and its `<title>` reads "In progress: …", not the page's own.
- **Its state** comes from the pane named in `meta.json`. While that pane runs, waits on a
  tool or has subagents out, the page is in progress. While it idles, the placeholder says
  so but keeps waiting, since a background subagent can build the page while its
  session's prompt looks idle. Once the pane is closed, or idle for 30 minutes, it says
  the agent stopped without publishing (without a pane, 30 minutes after `created`).
- **The gallery** and the drawer's Recent show the folder with a spinner while its agent
  works (once stopped, the gallery says so). Once the page lands, its thumbnail is queued and the spinner
  turns into it (it stops asking after about 18 minutes); while one spins, the gallery also
  reloads every 30 seconds (not while a filter is being typed), since the thumbnail never
  comes without a browser to shoot it.
- **When the page lands** (the daemon looks every ten seconds), every subscribed device gets one push, `Ready: <title>`, whose
  tap opens the page in the app's scratch viewer. It shares its tag with the pane's
  Needs-you notification, and for two minutes after it that pane's plain "turn ended"
  Needs-you is skipped (any question still notifies), so one finish buzzes once: a
  Needs-you already shown is replaced silently, and a question with buttons keeps its
  notification, the Ready arriving beside it. A page published in one go was never promised and does not notify.

An orchestrator that hands a subagent's link out writes `meta.json` itself, with its own
`$TMUX_PANE`: in Claude Code and Codex the subagent runs in that pane. A worker in another
pane rewrites it with its own pane when it starts.

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
  beside it. It is shot again after the page changes. Until then the card says "Making a preview…",
  or that there is none.
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
  card says "unknown". The session found is written into the folder's `meta.json` as
  `"agent_session"`, marked generated, so each folder is searched for only once.

Pages are shot offline. Each shot uses a throwaway browser profile and a listener that
serves scratch and nothing else, so a page can't put anything but scratch into its
thumbnail. The [design note](../design/scratch-previews.md) explains why.
