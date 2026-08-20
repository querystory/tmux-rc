.PHONY: dev run test fmt docs docs-dev docs-check docs-clean

# Dev server with auto-reload on source changes. Reload restarts the process (the
# watcher's in-memory cache resets and rebuilds from tmux within a couple ticks — safe,
# since tmux is the source of truth). Requires GOOGLE_CLOUD_PROJECT for the LLM pass.
dev:
	TMUXRC_RELOAD=1 uv run python -m daemon.server

# Plain run, no reload.
run:
	uv run python -m daemon.server

test:
	uv run pytest -q tests/

fmt:
	uv run --with ruff ruff check --fix daemon tests
	uv run --with ruff ruff format daemon tests

# Build the docs site the daemon serves, into docs-site/serve/ (NOT public/).
# Needs the EXTENDED Hugo build AND `go` on PATH (Hugo Modules fetches the Hextra
# theme via the Go toolchain). --baseURL /docs/ so assets emit under the /docs prefix
# the daemon mounts them at (see TMUXRC_DOCS_DIR in .env.example). Dedicated output dir
# so `docs-dev`'s Hugo server — which owns public/ and rewrites it on every edit — can
# never clobber the build the daemon is serving.
docs:
	cd docs-site && hugo --gc --minify --baseURL /docs/ --destination serve

# Docs authoring server with hot reload on http://localhost:18034. Ports here spell R,C
# (1803X) — "rc" being the half of the name that survives if the project is ever renamed
# off tmux — and the block keeps this project's ports clear of its neighbours'. Uses
# Hugo's default in-memory/public workspace at root baseURL — independent of the daemon's
# serve/ dir.
docs-dev:
	cd docs-site && hugo server --port 18034

# Build, then fail on broken internal links or blank pages.
docs-check: docs
	uv run python docs-site/check-links.py

docs-clean:
	rm -rf docs-site/public docs-site/serve docs-site/resources docs-site/.hugo_build.lock
