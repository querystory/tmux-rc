import json
import queue
import threading
from pathlib import Path

import pytest
from py_vapid import Vapid
from pydantic import ValidationError

from openbus import push


class Watcher:
    def __init__(self):
        self.states = []
        self.births = {"%1": "123"}
        self.reparsed = []
        self.stale = False
        self.parse_valid = True
        self.input_generations = {}

    def is_stale(self):
        return self.stale

    def pane_birth(self, pane_id):
        return self.births.get(pane_id)

    def pane_parse_valid(self, pane_id):
        return self.parse_valid

    def pane_input_generation(self, pane_id):
        return self.input_generations.get(pane_id, 0)

    def request_reparse(self, pane_id):
        self.reparsed.append(pane_id)

    def invalidate_input_actions(self, pane_id):
        self.input_generations[pane_id] = self.pane_input_generation(pane_id) + 1

    def note_input(self, pane_id):
        self.invalidate_input_actions(pane_id)
        self.request_reparse(pane_id)


class Sender:
    def __init__(self, available=True):
        self.payloads = []
        self.available = available

    def send(self, payload):
        if not self.available:
            return False
        self.payloads.append(payload)
        return True

    def close(self):
        pass


def waiting(question=None):
    state = {
        "pane_id": "%1", "label": "Build", "activity": "waiting",
        "waiting_on": "user", "headline": "Approval needed",
    }
    if question is not None:
        state["question"] = question
    return state


def manager(tmp_path, watcher, clock):
    sender = Sender()
    value = push.PushManager(
        watcher, push.PushStore(tmp_path / "push.json"), clock=lambda: clock[0], sender=sender,
    )
    return value, sender


def subscription(endpoint="https://web.push.apple.com/Q1/example"):
    return {"endpoint": endpoint, "keys": {
        "p256dh": push._b64(b"\x04" + b"p" * 64),
        "auth": push._b64(b"a" * 16),
    }}


def test_store_is_owner_only_atomic_and_upserts(tmp_path: Path):
    store = push.PushStore(tmp_path / "state" / "push.json")
    private, public = store.keys()
    assert public
    assert Vapid.from_string(private).public_key is not None
    assert store.path.stat().st_mode & 0o777 == 0o600
    subscription = {"endpoint": "https://push.example/one", "keys": {"p256dh": "p", "auth": "a"}}
    store.upsert(subscription)
    store.upsert(subscription)
    assert store.subscriptions() == [subscription]
    assert json.loads(store.path.read_text())["subscriptions"] == [subscription]


def test_store_prunes_malformed_persisted_subscriptions(tmp_path: Path):
    store = push.PushStore(tmp_path / "push.json")
    private, public = store.keys()
    valid = subscription()
    store.path.write_text(json.dumps({
        "private_key": private, "public_key": public,
        "subscriptions": [valid, {"endpoint": "https://127.0.0.1/internal", "keys": {}}],
    }))
    reloaded = push.PushStore(store.path)
    assert reloaded.subscriptions() == [valid]
    assert json.loads(store.path.read_text())["subscriptions"] == [valid]


def test_sender_rejects_work_after_shutdown():
    class Store:
        @staticmethod
        def subscriptions():
            return [{"endpoint": "https://web.push.apple.com/example"}]

    sender = push.PushSender(Store())
    sender.close()
    assert not sender.send({"title": "late"})


def test_sender_does_not_evict_already_accepted_work_when_full():
    class Store:
        @staticmethod
        def subscriptions():
            return [subscription()]

    sender = push.PushSender.__new__(push.PushSender)
    sender.store = Store()
    sender._queue = queue.Queue(maxsize=1)
    sender._state_lock = threading.Lock()
    sender._closed = False
    first = {"title": "first"}
    assert sender.send(first)
    assert not sender.send({"title": "second"})
    assert sender._queue.get_nowait() == first


def test_sender_delivers_to_devices_in_parallel(monkeypatch):
    subscriptions = [
        subscription("https://web.push.apple.com/Q1/one"),
        subscription("https://web.push.apple.com/Q1/two"),
    ]

    class Store:
        @staticmethod
        def subscriptions():
            return subscriptions

        @staticmethod
        def keys():
            return push._new_keys()[0], "unused"

        @staticmethod
        def remove(_endpoint):
            return False

    barrier = threading.Barrier(2)
    delivered = []
    done = threading.Event()

    def webpush(**kwargs):
        barrier.wait(timeout=2)
        delivered.append(kwargs["subscription_info"]["endpoint"])
        if len(delivered) == 2:
            done.set()

    monkeypatch.setattr(push, "webpush", webpush)
    sender = push.PushSender(Store())
    assert sender.send({"title": "parallel"})
    assert done.wait(3)
    sender.close()
    assert sorted(delivered) == sorted(item["endpoint"] for item in subscriptions)


@pytest.mark.parametrize("override", [None, "mailto:operator@example.com"])
def test_sender_vapid_contact_default_and_override(monkeypatch, override):
    monkeypatch.delenv("TMUXRC_PUSH_SUBJECT", raising=False)
    if override is not None:
        monkeypatch.setenv("TMUXRC_PUSH_SUBJECT", override)
    sent = []
    monkeypatch.setattr(push, "webpush", lambda **kwargs: sent.append(kwargs))
    sender = object.__new__(push.PushSender)
    sender._deliver(subscription(), {"title": "Question"}, "test-key")
    assert len(sent) == 1
    assert sent[0]["vapid_claims"]["sub"] == (override or "mailto:tmux-rc@openbus.io")


def test_option_mapping_matches_card_semantics():
    question = {"answer_style": "menu", "options": ["Yes", "No"]}
    assert push.option_keys(question, 0) == "1"
    assert push.option_keys(question, 1) == "2"
    assert push.option_keys({"answer_style": "menu", "options": ["Retry", "Abort"]}, 1) == "2"
    question = {"answer_style": "menu", "options": ["Alpha", "Other", "Beta"]}
    assert push.renderable_options(question) == [(0, "Alpha"), (2, "Beta")]
    assert push.option_keys(question, 2) == "3"
    question = {"answer_style": "text", "options": ["Ship it"]}
    assert push.option_keys(question, 0) == "Ship it"
    with pytest.raises(ValueError, match="app"):
        push.option_keys({"answer_style": "cursor", "options": ["row"]}, 0)
    question = {"answer_style": "text", "options": ["  ", "Ship it"]}
    assert push.renderable_options(question) == [(1, "Ship it")]
    with pytest.raises(ValueError, match="available"):
        push.option_keys(question, 0)


def test_answer_schema_accepts_large_valid_option_indices():
    from openbus.server import PushAnswerBody

    assert PushAnswerBody(nonce="n" * 24, option_index=101).option_index == 101


def test_subscription_schema_bounds_and_forbids_extra_key_material():
    from openbus.server import PushSubscriptionBody

    value = subscription()
    value["expirationTime"] = None
    parsed = PushSubscriptionBody(**value)
    assert parsed.endpoint == value["endpoint"] and parsed.expiration_time is None
    value["keys"]["junk"] = "x" * 10_000
    with pytest.raises(ValidationError):
        PushSubscriptionBody(**value)
    value = subscription()
    value["endpoint"] = "x" * 4097
    with pytest.raises(ValidationError):
        PushSubscriptionBody(**value)


def test_contract_includes_nonrendered_options_that_change_menu_mapping():
    pane = waiting({"prompt": "Proceed?", "answer_style": "menu", "options": ["Yes", "No"]})
    before = push.contract(pane, "123")[0]
    pane["question"]["options"].append("Other")
    with_option = push.contract(pane, "123")[0]
    assert with_option != before
    pane["question"]["context"] = "Bash command — rm -rf build"
    assert push.contract(pane, "123")[0] != with_option
    with_option = push.contract(pane, "123")[0]
    pane["question"]["ask"] = "The agent wants to delete build/. Continue?"
    assert push.contract(pane, "123")[0] != with_option
    with_option = push.contract(pane, "123")[0]
    pane["activity"] = "running"
    assert push.contract(pane, "123")[0] != with_option


def test_subscription_only_accepts_known_push_relays(tmp_path):
    watcher = Watcher()
    service, _ = manager(tmp_path, watcher, [100.0])
    value = subscription("https://127.0.0.1/internal")
    with pytest.raises(ValueError, match="invalid"):
        service.subscribe(value)
    value["endpoint"] = "https://web.push.apple.com/Q1/example"
    service.subscribe(value)
    assert service.store.subscriptions() == [value]
    value["keys"]["auth"] = "not-base64!"
    with pytest.raises(ValueError, match="invalid"):
        service.subscribe(value)


def test_subscription_store_is_bounded(tmp_path):
    watcher = Watcher()
    service, _ = manager(tmp_path, watcher, [100.0])
    for index in range(push.MAX_SUBSCRIPTIONS + 3):
        service.subscribe(subscription(f"https://web.push.apple.com/Q1/{index}"))
    saved = service.store.subscriptions()
    assert len(saved) == push.MAX_SUBSCRIPTIONS
    assert saved[0]["endpoint"].endswith("/3")


def test_wait_settles_then_notifies_once_and_can_notify_after_clear(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting({"prompt": "Proceed?", "answer_style": "menu", "context": "ls",
                               "options": ["Yes", "No"], "ask": "List the files?"})]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)

    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    service.evaluate()
    assert len(sender.payloads) == 1
    assert sender.payloads[0]["body"] == "List the files?"
    assert sender.payloads[0]["url"] == "/m#pane=%251&from=push"
    assert [a["title"] for a in sender.payloads[0]["actions"]] == ["Yes", "No"]

    watcher.states = []
    service.evaluate()
    # No widget rows: nothing tells this ask from the next identical one, so no actions.
    watcher.states = [waiting({"prompt": "Proceed?", "answer_style": "menu",
                               "options": ["Yes", "No"]})]
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    assert len(sender.payloads) == 2
    assert sender.payloads[1]["actions"] == [] and sender.payloads[1]["nonce"] is None


def test_visible_browser_and_active_tmux_do_not_suppress_push(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting()]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: True)
    service.note_presence("phone", visible=True)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    assert len(sender.payloads) == 1
    # Activity must not bypass duplicate prevention either.
    service.evaluate()
    assert len(sender.payloads) == 1


def test_presence_leases_are_bounded(tmp_path):
    watcher = Watcher()
    service, _ = manager(tmp_path, watcher, [100.0])
    for index in range(push.MAX_PRESENCE_LEASES + 20):
        service.note_presence(f"client-{index}", visible=True)
    assert len(service._presence) == push.MAX_PRESENCE_LEASES
    assert "client-0" not in service._presence
    assert f"client-{push.MAX_PRESENCE_LEASES + 19}" in service._presence


def test_input_generation_updates_are_atomic():
    from openbus.watcher import Watcher as RealWatcher

    watcher = RealWatcher(None, use_llm=False)
    def increment():
        for _ in range(500):
            watcher.invalidate_input_actions("%1")

    workers = [threading.Thread(target=increment) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert watcher.pane_input_generation("%1") == 4000


def test_stale_watcher_suppresses_until_live_state_resumes(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting()]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    watcher.stale = True
    service.evaluate()
    assert sender.payloads == []
    watcher.stale = False
    service.evaluate()
    assert len(sender.payloads) == 1


def test_failed_pane_parse_suppresses_retained_question(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting()]
    watcher.parse_valid = False
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    assert sender.payloads == []
    watcher.parse_valid = True
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    assert len(sender.payloads) == 1


def test_missing_waiting_on_defaults_to_an_actionable_user_wait(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    pane = waiting()
    pane.pop("waiting_on")
    watcher.states = [pane]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    assert len(sender.payloads) == 1
    assert "compose" not in sender.payloads[0]["url"]


def test_free_text_question_deep_links_to_the_composer(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting({"prompt": "What should I do?", "answer_style": "text"})]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    assert sender.payloads[0]["url"].endswith("&compose=1")


def test_cursor_question_never_focuses_the_text_composer(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting({"prompt": "Choose a row", "answer_style": "cursor",
                               "options": []})]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    assert "compose" not in sender.payloads[0]["url"]


def test_a_wait_is_not_marked_notified_before_any_device_is_subscribed(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting()]
    sender = Sender(available=False)
    service = push.PushManager(
        watcher, push.PushStore(tmp_path / "push.json"),
        clock=lambda: clock[0], sender=sender,
    )
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    assert sender.payloads == []

    sender.available = True
    service.evaluate()
    assert len(sender.payloads) == 1


def test_expired_rate_buckets_are_pruned(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    service, _ = manager(tmp_path, watcher, clock)
    service._rates[("%old", "1")].append(clock[0])
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    clock[0] += push.RATE_WINDOW_SECONDS
    service.evaluate()
    assert service._rates == {}


def test_action_nonce_is_one_shot_and_bound_to_live_contract(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting({"prompt": "Proceed?", "answer_style": "menu", "context": "ls",
                               "options": ["Yes", "No"]})]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    sent = []
    def send(pane, keys, **kwargs):
        kwargs.pop("guard")()
        sent.append((pane, keys, kwargs))

    monkeypatch.setattr(push.tmux, "send_keys", send)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    nonce = sender.payloads[0]["nonce"]

    assert service.answer(nonce, 0) == ("%1", "1")
    # No Enter: the menu commits on "1", so an Enter would answer whatever comes next.
    assert sent == [("%1", "1", {
        "enter": False, "literal": True, "expected_pid": "123",
    })]
    assert watcher.reparsed == ["%1"]
    with pytest.raises(ValueError, match="already used"):
        service.answer(nonce, 0)


def test_action_rejects_stale_watcher_state_and_consumes_nonce(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting({"prompt": "Proceed?", "answer_style": "menu", "context": "ls",
                               "options": ["Yes", "No"]})]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    nonce = sender.payloads[0]["nonce"]
    watcher.stale = True

    with pytest.raises(ValueError, match="temporarily unavailable"):
        service.answer(nonce, 0)
    with pytest.raises(ValueError, match="already used"):
        service.answer(nonce, 0)


def test_action_rejects_a_retained_question_after_parse_failure(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting({"prompt": "Proceed?", "answer_style": "menu", "context": "ls",
                               "options": ["Yes", "No"]})]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    nonce = sender.payloads[0]["nonce"]
    watcher.parse_valid = False

    with pytest.raises(ValueError, match="temporarily unavailable"):
        service.answer(nonce, 0)
    with pytest.raises(ValueError, match="already used"):
        service.answer(nonce, 0)


def test_other_pane_input_invalidates_an_outstanding_action(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting({"prompt": "Proceed?", "answer_style": "menu", "context": "ls",
                               "options": ["Yes", "No"]})]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    nonce = sender.payloads[0]["nonce"]
    watcher.note_input("%1")

    with pytest.raises(ValueError, match="newer input"):
        service.answer(nonce, 0)
    with pytest.raises(ValueError, match="already used"):
        service.answer(nonce, 0)


def test_concurrent_valid_nonces_cannot_both_submit(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting({"prompt": "Proceed?", "answer_style": "menu", "context": "ls",
                               "options": ["Yes", "No"]})]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    nonce = sender.payloads[0]["nonce"]
    duplicate = "duplicate-notification-nonce"
    service._nonces[duplicate] = dict(service._nonces[nonce])
    send_lock = threading.Lock()
    sent = []

    def send(_pane, keys, **kwargs):
        with send_lock:
            kwargs["guard"]()
            sent.append(keys)

    monkeypatch.setattr(push.tmux, "send_keys", send)
    results = []

    def answer(value):
        try:
            results.append(service.answer(value, 0))
        except ValueError as error:
            results.append(str(error))

    workers = [threading.Thread(target=answer, args=(value,))
               for value in (nonce, duplicate)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sent == ["1"]
    assert len([result for result in results if result == ("%1", "1")]) == 1
    assert any("newer input" in str(result) for result in results)


def test_action_rejects_a_reordered_question_and_consumes_nonce(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting({"prompt": "Choose", "answer_style": "text",
                               "options": ["Alpha", "Beta"]})]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    monkeypatch.setattr(push.tmux, "pane_pid", lambda _pane: "123")
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    nonce = sender.payloads[0]["nonce"]
    watcher.states[0]["question"]["options"].reverse()

    with pytest.raises(ValueError, match="changed"):
        service.answer(nonce, 0)
    with pytest.raises(ValueError, match="already used"):
        service.answer(nonce, 0)
