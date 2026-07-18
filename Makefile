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

# Build the docs site (docs/ -> docs-site/public/). Needs the extended Hugo build.
docs:
	cd docs-site && hugo --gc --minify

# Docs authoring server with hot reload on http://localhost:17194
docs-dev:
	cd docs-site && hugo server --port 17194

# Build, then fail if any internal doc link points at a missing page.
docs-check: docs
	uv run python docs-site/check-links.py

docs-clean:
	rm -rf docs-site/public docs-site/resources
