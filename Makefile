.PHONY: dev run test fmt

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
