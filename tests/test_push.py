import json
from pathlib import Path

import pytest
from py_vapid import Vapid

from openbus import push


class Watcher:
    def __init__(self):
        self.states = []
        self.births = {"%1": "123"}
        self.reparsed = []
        self.stale = False

    def is_stale(self):
        return self.stale

    def pane_birth(self, pane_id):
        return self.births.get(pane_id)

    def request_reparse(self, pane_id):
        self.reparsed.append(pane_id)


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


def test_sender_rejects_work_after_shutdown():
    class Store:
        @staticmethod
        def subscriptions():
            return [{"endpoint": "https://web.push.apple.com/example"}]

    sender = push.PushSender(Store())
    sender.close()
    assert not sender.send({"title": "late"})


def test_option_mapping_matches_card_semantics():
    question = {"answer_style": "menu", "options": ["Yes", "No"]}
    assert push.option_keys(question, 0) == "y"
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


def test_contract_includes_nonrendered_options_that_change_menu_mapping():
    pane = waiting({"prompt": "Proceed?", "answer_style": "menu", "options": ["Yes", "No"]})
    before = push.contract(pane, "123")[0]
    pane["question"]["options"].append("Other")
    assert push.contract(pane, "123")[0] != before


def test_subscription_only_accepts_known_push_relays(tmp_path):
    watcher = Watcher()
    service, _ = manager(tmp_path, watcher, [100.0])
    subscription = {"endpoint": "https://127.0.0.1/internal",
                    "keys": {"p256dh": "p", "auth": "a"}}
    with pytest.raises(ValueError, match="invalid"):
        service.subscribe(subscription)
    subscription["endpoint"] = "https://web.push.apple.com/Q1/example"
    service.subscribe(subscription)
    assert service.store.subscriptions() == [subscription]


def test_wait_settles_then_notifies_once_and_can_notify_after_clear(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting({"prompt": "Proceed?", "answer_style": "menu",
                               "options": ["Yes", "No"]})]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)

    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    service.evaluate()
    assert len(sender.payloads) == 1
    assert sender.payloads[0]["url"] == "/m#pane=%251&from=push"
    assert [a["title"] for a in sender.payloads[0]["actions"]] == ["Yes", "No"]

    watcher.states = []
    service.evaluate()
    watcher.states = [waiting({"prompt": "Proceed?", "answer_style": "menu",
                               "options": ["Yes", "No"]})]
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    assert len(sender.payloads) == 2


def test_visible_presence_suppresses_until_the_lease_is_gone(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting()]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    service.note_presence("phone", visible=True)
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    assert sender.payloads == []
    service.note_presence("phone", visible=False)
    service.evaluate()
    assert len(sender.payloads) == 1


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
    watcher.states = [waiting({"prompt": "Proceed?", "answer_style": "menu",
                               "options": ["Yes", "No"]})]
    service, sender = manager(tmp_path, watcher, clock)
    monkeypatch.setattr(push.tmux, "client_active_within", lambda _seconds: False)
    sent = []
    monkeypatch.setattr(
        push.tmux, "send_keys", lambda pane, keys, **kw: sent.append((pane, keys, kw))
    )
    service.evaluate()
    clock[0] += push.SETTLE_SECONDS
    service.evaluate()
    nonce = sender.payloads[0]["nonce"]

    assert service.answer(nonce, 0) == ("%1", "y")
    assert sent == [("%1", "y", {
        "enter": True, "literal": True, "expected_pid": "123",
    })]
    assert watcher.reparsed == ["%1"]
    with pytest.raises(ValueError, match="already used"):
        service.answer(nonce, 0)


def test_action_rejects_stale_watcher_state_and_consumes_nonce(tmp_path, monkeypatch):
    clock = [100.0]
    watcher = Watcher()
    watcher.states = [waiting({"prompt": "Proceed?", "answer_style": "menu",
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
