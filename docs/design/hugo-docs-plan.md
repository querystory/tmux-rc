# Hugo docs site for termiphone

Status: **proposed**. Plan for serving `docs/` as a browsable site at `/docs`, modeled on qs-app's
docs setup but stripped to what a small repo actually needs.

## Why

Today `docs/` is a flat pile of Markdown read only in the editor or on GitHub. We want it browsable —
nav, search, mermaid, cross-links that resolve — served at `/docs` off the same daemon that already
serves the PWA. The daemon proxies (really: serves the pre-built static files) for now; when the
server moves to Go the doc pipeline is *already* Go (Hugo), so nothing about docs has to move with it.

## What we're copying from qs-app, and what we're not

qs-app runs **MkDocs Material**, not Hugo — its `docs/hugo-migration-plan.md` was written and never
executed. What it *actually* does, and which parts we adopt:

**Adopt (the serving model).** qs-app builds each doc site to `backend/static/<name>/` and serves it
through an **authenticated FastAPI route** (`backend/routes/frontend.py`) that reads the pre-built HTML
off disk — `GET /docs/{path}` returns the file, or `index.html`, or the theme's `404.html`. There is no
`StaticFiles` mount for docs and no live proxy to a running Hugo/MkDocs server; production serves flat
files. That's exactly "proxy them from the py api server for now," and it's the pattern we copy.

**Adopt (single hand-maintained nav + Material-style features).** The `docs/design/mkdocs.yml` nav is a
big explicit tree, with search, mermaid, admonitions, dark/light toggle. Hextra gives us the equivalent
(sidebar auto-built from the content tree + front-matter `weight`, Pagefind search, mermaid, callouts)
with *less* hand-maintenance because the sidebar comes from the file layout instead of a 250-line `nav:`.

**Reject (the entire icon-sync pipeline).** ~70% of qs-app's Hugo plan is `Icon.tsx` → `icons.json` →
downloaded Lucide SVGs → a Hugo `icon` shortcode, so docs icons match the React app pixel-for-pixel.
termiphone has no `Icon.tsx` and no app to match. **None of that pipeline is in this plan.** Dropping it
is the single biggest simplification and the main reason this is a short doc, not a 2200-line one.

**Reject (two sites).** qs-app splits `/docs` (customer) and `/internal` (engineer). termiphone's docs
are all internal; one site at `/docs` is the whole thing. If a customer-facing split is ever needed,
it's an additive second Hugo site, not a prerequisite.

## Decisions

- **Hugo + Hextra.** Chosen over MkDocs because the server is moving to Go anyway — keeping the doc
  toolchain in Go means one language for the eventual backend. Hextra is clean, Tailwind-based, and
  gives search/mermaid/callouts out of the box.
- **Extended Hugo is required and not yet installed.** The `hugo` on this box is `v0.153.0` *standard* —
  Hextra ships SCSS, which only the **extended** build compiles. The plan installs a pinned
  `hugo_extended` from GitHub releases (not `go install` — current Hugo needs Go ≥1.23 and this box has
  1.21). This is the one real setup gate; everything else is downstream of it.
- **Hextra via Hugo Modules**, not a git submodule. Modules keep the theme out of our tree and pinned in
  `go.mod`; a submodule is more moving parts for no benefit here.
- **Content stays in `docs/`; no `content/` copy.** qs-app's plan copied Markdown into
  `docs/user/content/`. We instead point Hugo's `contentDir` at the existing `docs/` files so there's
  **one source of truth** and existing relative links (`[durable-vertex-auth.md](durable-vertex-auth.md)`)
  keep working. This is the DRY win — no migration script, no drift between two copies.
- **Search: Pagefind**, run as a post-build step over Hugo's output. Client-side, no server, matches the
  flat-file serving model.
- **Served at `/docs` behind the daemon.** Build output lands somewhere like `web/docs/` (or a sibling
  the server reads), and the daemon grows a `/docs/{path}` route mirroring qs-app's — file read, fall
  back to `index.html`/`404.html`. It slots in *before* the catch-all `/` PWA mount so `/docs` wins.

## Styling — this is a first-class goal, not polish-at-the-end

The docs should be *beautiful*, not stock-Hextra. Hextra's defaults are clean but generic (it reads as
"a Nextra clone"); shipping them unchanged would look like every other Hugo doc site. Treat visual design
as a Phase of its own, informed by termiphone's existing identity rather than invented fresh.

- **Borrow the brand that already exists.** `web/` has real assets — `icon.svg`, `tmux-logo.svg` /
  `tmux-logomark.svg`, `apple-touch-icon.png`, the model glyphs (`claude`, `gemini`, `openai`), and the
  PWA's own CSS in `web/index.html` / `web/app.js`. The docs should feel like the same product as the
  PWA: pull the PWA's color variables, font stack, and accent treatment into the Hextra theme override so
  `/docs` and `/` look like siblings, not strangers. This is the concrete version of qs-app's "match the
  app" goal — except our "app" is the terminal PWA, and the assets are sitting right there in `web/`.
- **Where the styling lives.** Hextra is themed via (a) `hugo.toml` `[params]` for primary/accent color,
  logo, favicon, font; and (b) a custom SCSS/CSS partial layered on top for anything the params don't
  reach (code-block chrome, callout borders, sidebar active state, link hover, spacing rhythm). Keep it
  in one override file, not scattered `!important`s — qs-app's plan leaned on `!important` overrides,
  which is the anti-pattern we avoid.
- **Typography carries most of the "beautiful."** Pick a deliberate pair — a characterful sans for body
  and a good mono for code (the PWA already commits to a terminal aesthetic; a monospace-forward or
  terminal-flavored treatment for headings/code would tie the site to the product's whole reason to
  exist). Set generous line-height and measure; don't accept Hextra's defaults.
- **Dark mode is the primary mode.** This is a terminal-agent tool viewed on a phone, often at night.
  Design the dark theme first and make it genuinely nice (not just inverted), then let light mode follow.
- **Load the `frontend-design` skill before writing any CSS.** It calibrates design investment and steers
  away from templated defaults — exactly the failure mode of unstyled Hextra. Do this at the top of the
  styling phase, before touching `hugo.toml [params]` or the override file.
- **Mermaid + code blocks are the hero content.** Most of these docs are architecture prose with mermaid
  diagrams and code. Style *those* well — diagram theming that matches the palette, code blocks with the
  right background/radius/copy-button — because that's what readers actually look at.

Sequencing: this becomes **Phase 2.5** (after content renders, before/with the Makefile phase) so we're
styling real pages, not lorem ipsum. It's explicitly in scope, unlike the icon pipeline.

## Open questions (decide before building, not blocking the plan)

1. **Front matter.** Hextra's sidebar reads `title`/`weight` from front matter. Our `.md` files have
   `# Heading` but no front matter. Options: (a) add minimal front matter to each file, (b) let Hextra
   derive titles from the H1/filename and accept default (alphabetical) ordering, (c) one `_index.md`
   per section setting order. Leaning (b) to start — zero edits to existing docs — and add `weight` only
   where ordering actually matters.
2. **`docs/design/` as a section vs. top-level.** With everything in one site, `docs/design/*` naturally
   becomes a "Design" section and the root `docs/*.md` (PRD, REQUIREMENTS, benchmarks…) become top-level
   pages. That's fine; just confirm the sidebar grouping reads well.
3. **Auth.** qs-app gates docs on user status. termiphone's daemon is loopback/tunnel with an audit trust
   gate — decide whether `/docs` is open to anyone who can reach the daemon (probably yes; it's your own
   phone) or gated like the API mutations.
4. **Where build output lives + `.gitignore`.** Pick the output dir and gitignore it (qs-app gitignores
   the built site; source-only in git).

## Phased build

**Phase 1 — Hugo scaffold.** Install `hugo_extended` (pinned). `hugo mod init` at repo root (or a
`docs-site/` holding only `hugo.toml` + `go.mod`, with `contentDir` pointing back at `docs/`).
`hugo mod get github.com/imfing/hextra`. Minimal `hugo.toml`: `baseURL = "/docs/"`, theme=hextra,
mermaid on, Pagefind search on, goldmark `unsafe=true` for raw HTML (some design docs embed HTML).
Verify `hugo server` renders the existing `docs/` tree.

**Phase 2 — Content fit.** Decide the front-matter question (#1). Spot-fix anything that renders wrong:
MkDocs-style admonitions if any snuck in, mermaid fences (Hextra renders ```` ```mermaid ```` natively),
relative links across the `docs/` ↔ `docs/design/` boundary. No bulk migration script — the content is
small enough to eyeball.

**Phase 2.5 — Styling (see the styling section above).** Load `frontend-design`, pull brand from `web/`,
theme Hextra via `hugo.toml [params]` + one override file, commit to dark-mode-first and a deliberate
type pair, and style mermaid/code blocks as the hero content. Done against real rendered pages.

**Phase 3 — Build + search + Makefile.** Add targets mirroring the existing Makefile's terse style:
- `docs` — `hugo --minify` then `pagefind` over the output.
- `docs-dev` — `hugo server` with hot reload on a fixed port for local authoring.
- `docs-clean` — remove build output.

Keep them as thin as the current `dev`/`run`/`test`/`fmt` targets; no emoji-heavy multi-line recipes.

**Phase 4 — Serve at `/docs`.** Add the daemon route (mirrors qs-app `frontend.py`): `GET /docs/{path}`
reads the built file, falls back to `index.html` / theme `404.html`, registered before the `/` PWA
mount. Confirm relative asset paths resolve under the `/docs/` base URL.

**Phase 5 — CI/build wiring.** If docs should ship with the deployed daemon, add the `docs` target to
whatever builds the container/image, and gitignore the build output. Hextra needs network at build time
for the module fetch (or vendor via `hugo mod vendor`) — decide which for the deploy environment.

## Cost / risk

- **One real install gate:** extended Hugo. Once pinned in setup docs + CI, it's done.
- **Network at build:** Hugo Modules fetch Hextra from GitHub. Mitigate with `hugo mod vendor` if the
  build env is offline.
- **Rollback is free:** the source `.md` files are untouched and still read fine on GitHub/in-editor.
  Deleting the Hugo scaffold reverts to today with no content loss.

## What this explicitly does NOT include

No `Icon.tsx` parsing, no `icons.json`, no Lucide SVG downloader, no icon shortcode, no `content/` copy
of the docs, no second `/internal` site, no personalization/interactive-example/versioned-docs phases
from the qs-app plan. If any of those are wanted later they're additive — none are load-bearing for
"serve `docs/` at `/docs`."
