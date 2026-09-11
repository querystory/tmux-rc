import pytest


@pytest.fixture(autouse=True)
def _vertex_project(monkeypatch):
    """Vertex entries are offered only when a project is set. Supply one so the suite does
    not depend on the checkout's .env (a worktree has none) for what it offers."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
