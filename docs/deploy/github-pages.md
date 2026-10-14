---
title: Project website on GitHub Pages
---

The public website is [querystory.github.io/tmux-rc](https://querystory.github.io/tmux-rc/).
It uses the same Markdown in `docs/` and the same Hugo/Hextra configuration in `docs-site/`
as the daemon's local documentation. The dashboard is served separately by your daemon.

## Publication

The `Website & docs` GitHub Actions workflow builds relevant pull requests and checks
for broken links and blank pages. Changes to the docs, site configuration, or build
workflow on `main` publish automatically through GitHub Pages. It can also be run manually.
GitHub Pages uses the **GitHub Actions** publishing source in repository settings.

Hugo and the theme are pinned, and the workflow verifies the Hugo download checksum.
The deployment uses GitHub's temporary workflow credentials; no hosting secret is needed.

## Local builds

```bash
# The daemon's /docs/ build:
make docs-check

# The public project site, including its /tmux-rc/ prefix:
make docs-check DOCS_BASE_URL=https://querystory.github.io/tmux-rc/ DOCS_DESTINATION=public

# Author with hot reload:
make docs-dev
```

Install Hugo Extended and Go before building. The public output goes to
`docs-site/public/`; the daemon's build goes to `docs-site/serve/`.

Keep screenshots fictional: use the demo fleet as described in the repository's
working conventions. Documentation edits remain ordinary reviewed source changes.
