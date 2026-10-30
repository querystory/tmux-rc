"""The shared Vertex client is built once even when parses race for it (issue #525).

No network: google.genai.Client is replaced by a stub that is slow to construct, so
concurrent first calls overlap the way a startup burst of parses does."""

import threading
import time

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
