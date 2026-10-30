"""The shared Vertex client is built once even when parses race for it (issue #525).

No network: google.genai.Client is replaced by a stub that is slow to construct, so
concurrent first calls overlap the way a startup burst of parses does."""

import threading
import time

import pytest
from google import genai

from openbus import llm


def test_concurrent_first_calls_share_one_client(monkeypatch):
    built = []

    class SlowClient:
        def __init__(self, **_):
            time.sleep(0.05)  # the window in which every other thread also misses the cache
            built.append(self)

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-project")
    monkeypatch.setattr(genai, "Client", SlowClient)
    llm._build_client.cache_clear()
    try:
        got = []
        threads = [threading.Thread(target=lambda: got.append(llm._client())) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        llm._build_client.cache_clear()
    assert len(built) == 1, "each extra client is dropped and closed under its caller"
    assert {id(c) for c in got} == {id(built[0])}


def test_project_picks_vertex_else_api_key(monkeypatch):
    """Vertex whenever GOOGLE_CLOUD_PROJECT is set; otherwise GEMINI_API_KEY (AI Studio), with
    the same timeout. Neither: the error names both."""
    monkeypatch.setattr(genai, "Client", lambda **kw: kw)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    cases = [("example-project", {"vertexai": True, "project": "example-project"}),
             (None, {"api_key": "k"})]
    try:
        for project, want in cases:
            if project:
                monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project)
            else:
                monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
            llm._build_client.cache_clear()
            got = llm._build_client()
            assert want.items() <= got.items() and got["http_options"].timeout == llm._TIMEOUT_MS
            assert ("api_key" in got) != ("vertexai" in got)
        monkeypatch.delenv("GEMINI_API_KEY")
        llm._build_client.cache_clear()
        with pytest.raises(RuntimeError, match=r"GOOGLE_CLOUD_PROJECT.*GEMINI_API_KEY"):
            llm._build_client()
    finally:
        llm._build_client.cache_clear()
