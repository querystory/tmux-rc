from concurrent.futures import Future

from openbus.pr_titles import PRTitles, fetch_title


def test_title_cache_nonblocking_deduplicates_and_reuses():
    cache = PRTitles()
    cache.close()

    class Pool:
        def __init__(self):
            self.calls = []
            self.future = Future()

        def submit(self, *args):
            self.calls.append(args)
            return self.future

    pool = Pool()
    cache._pool = pool
    cache._closed = False
    pr = {"repo": "querystory/tmux-rc", "number": 245}
    assert cache.enrich([pr]) == [pr]
    assert cache.enrich([pr]) == [pr]
    assert len(pool.calls) == 1
    pool.future.set_result("Track PRs per session")
    assert cache.enrich([pr]) == [{**pr, "title": "Track PRs per session"}]
    assert len(pool.calls) == 1


def test_title_lookup_failure_falls_back(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("gh unavailable")

    monkeypatch.setattr("openbus.pr_titles.subprocess.run", fail)
    assert fetch_title("querystory/tmux-rc", 245) is None


def test_title_refresh_retains_stale_value_and_backs_off(monkeypatch):
    cache = PRTitles()
    cache.close()
    key = ("querystory/tmux-rc", 245)
    cache._cache[key] = (100, "Existing title")
    future = Future()
    future.set_result(None)
    cache._pending[key] = future
    monkeypatch.setattr("openbus.pr_titles.time.monotonic", lambda: 100)
    assert cache.enrich([{"repo": key[0], "number": key[1]}])[0]["title"] == "Existing title"
    assert cache._cache[key] == (160, "Existing title")
    assert cache._pending == {}


def test_title_shutdown_handles_cancelled_lookups():
    cache = PRTitles()
    cache.close()
    future = Future()
    future.cancel()
    cache._pending[("querystory/tmux-rc", 245)] = future
    pr = {"repo": "querystory/tmux-rc", "number": 245}
    assert cache.enrich([pr]) == [pr]
    assert cache._pending == {}


def test_title_change_publishes_idle_pane():
    from openbus.watcher import Watcher

    pane = {"pane_id": "%1", "activity": "idle", "prs": [
        {"repo": "querystory/tmux-rc", "number": 245},
    ]}
    before = Watcher._deck_fp([pane])
    pane["prs"][0]["title"] = "Track PRs per session"
    assert Watcher._deck_fp([pane]) != before
