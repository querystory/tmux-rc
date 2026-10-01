from concurrent.futures import Future

from openbus.pr_titles import PRTitles, fetch_pr


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
    pool.future.set_result({"title": "Track PRs per session", "state": "OPEN"})
    assert cache.enrich([pr]) == [{**pr, "title": "Track PRs per session"}]
    assert len(pool.calls) == 1


def test_title_lookup_failure_falls_back(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("gh unavailable")

    monkeypatch.setattr("openbus.pr_titles.subprocess.Popen", fail)
    assert fetch_pr("querystory/tmux-rc", 245) is None


def test_title_refresh_retains_stale_value_and_backs_off(monkeypatch):
    cache = PRTitles()
    cache.close()
    key = ("querystory/tmux-rc", 245)
    cache._cache[key] = (100, {"title": "Existing title", "state": "OPEN"})
    future = Future()
    future.set_result(None)
    cache._pending[key] = future
    monkeypatch.setattr("openbus.pr_titles.time.monotonic", lambda: 100)
    assert cache.enrich([{"repo": key[0], "number": key[1]}])[0]["title"] == "Existing title"
    assert cache._cache[key][0] == 160 and cache._cache[key][1]["state"] == "OPEN"
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


def test_shutdown_between_closed_check_and_submit_is_safe():
    cache = PRTitles()
    cache.close()

    class ClosingPool:
        def submit(self, *_args):
            cache._closed = True
            raise RuntimeError("cannot schedule new futures after shutdown")

    cache._pool = ClosingPool()
    cache._closed = False
    pr = {"repo": "querystory/tmux-rc", "number": 245}
    assert cache.enrich([pr]) == [pr]


def test_shutdown_terminates_a_running_lookup(monkeypatch):
    import subprocess
    import sys
    from threading import Event

    started = Event()
    processes = []
    popen = subprocess.Popen

    def slow_lookup(_args, **kwargs):
        process = popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr("openbus.pr_titles.subprocess.Popen", slow_lookup)
    cache = PRTitles()
    try:
        cache.enrich([{"repo": "querystory/tmux-rc", "number": 245}])
        assert started.wait(2)
        future = next(iter(cache._pending.values()))
        cache.close()
        assert future.result(timeout=2) is None
        assert processes[0].poll() is not None
    finally:
        cache.close()


def _gh(monkeypatch, out):
    import subprocess

    class Done:
        returncode = 0

        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def communicate(self, timeout=None):
            return out, ""

    monkeypatch.setattr(subprocess, "Popen", Done)


def test_fetch_reads_title_and_state(monkeypatch):
    _gh(monkeypatch, '{"title": " T ", "state": "MERGED"}')
    assert fetch_pr("o/r", 1) == {"title": "T", "state": "MERGED"}


def test_merged_and_closed_prs_are_dropped_open_kept_failure_keeps_state():
    cache = PRTitles()
    cache.close()
    prs = [{"repo": "o/r", "number": n} for n in (1, 2, 3, 4)]
    for n, state in ((1, "MERGED"), (2, "CLOSED"), (3, "OPEN")):
        cache._cache[("o/r", n)] = (float("inf"), {"title": f"t{n}", "state": state})
    assert [p["number"] for p in cache.enrich(prs)] == [3, 4]
    # a failed refresh falls back to the last known info, so a merged PR stays retired
    future = Future()
    future.set_result(None)
    cache._pending[("o/r", 1)] = future
    cache._cache[("o/r", 1)] = (0, {"title": "t1", "state": "MERGED"})
    assert [p["number"] for p in cache.enrich(prs)] == [3, 4]
