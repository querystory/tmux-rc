.PHONY: dev run test fmt docs docs-dev docs-check docs-clean install-units

# Dev server with auto-reload on source changes. Reload restarts the process (the
# watcher's in-memory cache resets and rebuilds from tmux within a couple ticks — safe,
# since tmux is the source of truth). Requires GOOGLE_CLOUD_PROJECT for the LLM pass.
dev:
	TMUXRC_RELOAD=1 uv run python -m daemon.server

# Plain run, no reload.
run:
	uv run python -m daemon.server

# Install/refresh the systemd --user units (see docs/design/deployment.md). Idempotent:
# safe to re-run after editing a unit. enable-linger is what moves the units' start from
# "first login" to "boot", so an unattended reboot brings the phone back by itself; set
# LINGER=0 to skip it on a host with an encrypted $HOME, where nothing under it exists
# pre-login and lingering units would crash-loop until someone logs in.
LINGER ?= 1
install-units:
	install -Dm644 -t $(HOME)/.config/systemd/user deploy/systemd/tmux-rc.service \
		deploy/systemd/tmux-rc-tunnel.service deploy/systemd/tmux-rc.target
	systemctl --user daemon-reload
	systemctl --user enable --now tmux-rc.target tmux-rc.service tmux-rc-tunnel.service
	[ "$(LINGER)" = "1" ] && loginctl enable-linger $(USER) || \
		echo "LINGER=0: skipping enable-linger (units start at first login)"
	systemctl --user --no-pager status tmux-rc.service tmux-rc-tunnel.service | head -20

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

# Docs authoring server with hot reload on http://localhost:17194. Uses Hugo's default
# in-memory/public workspace at root baseURL — independent of the daemon's serve/ dir.
docs-dev:
	cd docs-site && hugo server --port 17194

# Build, then fail on broken internal links or blank pages.
docs-check: docs
	uv run python docs-site/check-links.py

docs-clean:
	rm -rf docs-site/public docs-site/serve docs-site/resources docs-site/.hugo_build.lock
