"""Web Push delivery for panes that are blocked on the user.

The watcher remains the source of truth.  This module only routes stable, actionable
waits to subscribed browsers and validates notification action replies against the
still-live answer contract before typing anything into tmux.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import tempfile
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from . import tmux
from .classify import is_approval

logger = logging.getLogger(__name__)

SETTLE_SECONDS = 5.0
PRESENCE_SECONDS = 40.0
RATE_WINDOW_SECONDS = 15 * 60.0
RATE_MAX = 3
NONCE_SECONDS = 10 * 60.0
MAX_PRESENCE_LEASES = 128
MAX_SUBSCRIPTIONS = 16
DEFAULT_PUSH_HOSTS = (
    "web.push.apple.com",
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    ".notify.windows.com",
)
_FREETEXT_OPTION = re.compile(
    r"^(type\b|other\b|something else|let me|custom|free.?text|write )", re.IGNORECASE
)


def _valid_key(value, size: int) -> bool:
    if not isinstance(value, str):
        return False
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        return False
    return len(raw) == size


def _allowed_hosts() -> tuple[str, ...]:
    configured = os.environ.get("TMUXRC_PUSH_ALLOWED_HOSTS", "")
    return tuple(
        item.strip().lower() for item in configured.split(",") if item.strip()
    ) or DEFAULT_PUSH_HOSTS


def _valid_subscription(subscription) -> bool:
    if not isinstance(subscription, dict):
        return False
    endpoint = subscription.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        return False
    try:
        parsed = urlparse(endpoint)
        port = parsed.port
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    trusted_host = any(
        host == rule or (rule.startswith(".") and host.endswith(rule))
        for rule in _allowed_hosts()
    )
    keys = subscription.get("keys")
    return bool(
        parsed.scheme == "https" and host and not parsed.username and not parsed.password
        and port in (None, 443) and trusted_host and not parsed.fragment
        and isinstance(keys, dict) and _valid_key(keys.get("p256dh"), 65)
        and _valid_key(keys.get("auth"), 16)
    )


def default_path() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return root / "tmux-rc" / "push.json"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _new_keys() -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    # py-vapid's string form is the 32-byte P-256 scalar, base64url encoded (not PEM).
    private = _b64(key.private_numbers().private_value.to_bytes(32, "big"))
    public = _b64(key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    ))
    return private, public


class PushStore:
    """Owner-only, atomic persistence for VAPID keys and browser credentials."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_path()
        self._lock = threading.Lock()
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            private, public = _new_keys()
            data = {"private_key": private, "public_key": public, "subscriptions": []}
            self._data = data
            self._save()
            return data
        if (not isinstance(data, dict) or not _valid_key(data.get("private_key"), 32)
                or not _valid_key(data.get("public_key"), 65)
                or not isinstance(data.get("subscriptions"), list)):
            raise ValueError("invalid push subscription store")
        os.chmod(self.path, 0o600)
        subscriptions = [item for item in data["subscriptions"] if _valid_subscription(item)]
        changed = subscriptions != data["subscriptions"]
        data["subscriptions"] = subscriptions[-MAX_SUBSCRIPTIONS:]
        changed = changed or len(subscriptions) > MAX_SUBSCRIPTIONS
        self._data = data
        if changed:
            self._save()
        return data

    def _save(self) -> None:
        assert self._data is not None
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd, name = tempfile.mkstemp(prefix=".push-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as out:
                os.fchmod(out.fileno(), 0o600)
                json.dump(self._data, out, separators=(",", ":"))
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
            raise

    def keys(self) -> tuple[str, str]:
        with self._lock:
            data = self._load()
            return data["private_key"], data["public_key"]

    def subscriptions(self) -> list[dict]:
        with self._lock:
            return [
                {"endpoint": item["endpoint"], "keys": dict(item["keys"])}
                for item in self._load()["subscriptions"]
            ]

    def upsert(self, subscription: dict) -> None:
        with self._lock:
            data = self._load()
            subscriptions = [
                item for item in data["subscriptions"]
                if item.get("endpoint") != subscription["endpoint"]
            ] + [subscription]
            data["subscriptions"] = subscriptions[-MAX_SUBSCRIPTIONS:]
            self._save()

    def remove(self, endpoint: str) -> bool:
        with self._lock:
            data = self._load()
            old = data["subscriptions"]
            data["subscriptions"] = [item for item in old if item.get("endpoint") != endpoint]
            changed = len(old) != len(data["subscriptions"])
            if changed:
                self._save()
            return changed

    def revoke_all(self) -> int:
        with self._lock:
            data = self._load()
            count = len(data["subscriptions"])
            data["subscriptions"] = []
            if count:
                self._save()
            return count


class PushSender:
    """Small, lossy worker queue: push relay trouble must never stall the watcher."""

    def __init__(self, store: PushStore):
        self.store = store
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=16)
        self._state_lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="tmux-rc-push", daemon=True)
        self._thread.start()

    def send(self, payload: dict) -> bool:
        with self._state_lock:
            if self._closed or not self.store.subscriptions():
                return False
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                # Keep already-accepted work intact. Returning False lets PushManager
                # remove this payload's nonce and retry its still-current wait later.
                logger.warning("push queue full; deferring notification")
                return False
        return True

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    return
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while (payload := self._queue.get()) is not None:
            private, _ = self.store.keys()
            subscriptions = self.store.subscriptions()
            if not subscriptions:
                continue
            # One slow relay/device must not serially delay every other subscribed device.
            with ThreadPoolExecutor(
                max_workers=len(subscriptions), thread_name_prefix="tmux-rc-webpush"
            ) as pool:
                futures = [pool.submit(self._deliver, subscription, payload, private)
                           for subscription in subscriptions]
                for future in futures:
                    future.result()  # _deliver contains and logs endpoint-specific errors

    def _deliver(self, subscription: dict, payload: dict, private: str) -> None:
        try:
            # pywebpush mutates this dict with endpoint-specific aud/exp claims,
            # so every endpoint receives the independent copy returned by the store.
            claims = {"sub": os.environ.get(
                "TMUXRC_PUSH_SUBJECT", "mailto:tmux-rc@openbus.io"
            )}
            webpush(
                subscription_info=subscription,
                data=json.dumps(payload, separators=(",", ":")),
                vapid_private_key=private,
                vapid_claims=claims,
                timeout=(3, 7),
                ttl=600,
                headers={"Urgency": "high"},
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {404, 410}:
                self.store.remove(subscription.get("endpoint", ""))
            else:
                # Intentionally no retry: an answer prompt delayed by a relay
                # outage is stale, and draining old alerts later is worse.
                logger.warning("push delivery failed: %s", exc)
        except Exception:
            logger.warning("push delivery failed", exc_info=True)


def renderable_options(question: dict) -> list[tuple[int, str]]:
    options = question.get("options")
    if not isinstance(options, list):
        return []
    out = []
    for index, option in enumerate(options):
        if (not isinstance(option, str) or not option.strip()
                or _FREETEXT_OPTION.match(option.strip())):
            continue
        out.append((index, option))
    return out


def contract(pane: dict, birth: str | None) -> tuple[str, dict | None]:
    question = pane.get("question") if isinstance(pane.get("question"), dict) else None
    value = {
        "birth": birth,
        "activity": pane.get("activity"),
        "waiting_on": pane.get("waiting_on", "user"),
        "prompt": question.get("prompt") if question else pane.get("headline"),
        # Keep the complete source array as well as the renderable indices. Menu mapping
        # depends on the original option count, including pseudo/non-string rows.
        "options": question.get("options") if question else [],
        "renderable": renderable_options(question) if question else [],
        "style": question.get("answer_style") if question else None,
        # The same "Do you want to proceed?" over a different command is a different ask:
        # a notification describing one must not approve the other.
        "context": question.get("context") if question else None,
        # The body is the restatement: one that arrives late (a retried call) re-notifies.
        "ask": question.get("ask") if question else None,
    }
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return digest, question


def option_keys(question: dict, index: int) -> str:
    options = question.get("options")
    if not isinstance(options, list) or index < 0 or index >= len(options):
        raise ValueError("option is no longer available")
    option = options[index]
    if not isinstance(option, str) or not option.strip():
        raise ValueError("option is no longer available")
    style = question.get("answer_style", "text")
    if style == "cursor":
        raise ValueError("cursor questions must be answered in the app")
    if style == "menu":  # mirrors answerBody in web/cursor-pick.js: always the row's digit
        if index >= 9:  # "10" would commit row 1 on its first key
            raise ValueError("use the keyboard for menu rows past 9")
        return str(index + 1)
    return option


def _push_text(value, limit: int) -> str:
    """Bound encrypted payload size while keeping the full question in the deep link."""
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class PushManager:
    def __init__(self, watcher, store: PushStore | None = None, *, clock=time.monotonic,
                 sender=None):
        self.watcher = watcher
        self.store = store or PushStore()
        self.sender = sender or PushSender(self.store)
        self.clock = clock
        self._presence: dict[str, float] = {}
        self._stable: dict[str, tuple[str, float]] = {}
        self._notified: set[tuple[str, str]] = set()
        self._rates: dict[tuple[str, str | None], deque[float]] = defaultdict(deque)
        self._nonces: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.sender.close()

    def note_presence(self, client: str, *, visible: bool) -> None:
        if not client:
            return
        with self._lock:
            if visible:
                client = client[:100]
                self._presence[client] = self.clock()
                while len(self._presence) > MAX_PRESENCE_LEASES:
                    oldest = min(self._presence, key=self._presence.get)
                    self._presence.pop(oldest, None)
            else:
                self._presence.pop(client[:100], None)

    def subscribe(self, subscription: dict) -> None:
        if not _valid_subscription(subscription):
            raise ValueError("invalid push subscription")
        endpoint = subscription["endpoint"]
        keys = subscription["keys"]
        self.store.upsert({"endpoint": endpoint, "keys": {
            "p256dh": keys["p256dh"], "auth": keys["auth"],
        }})

    def answer(self, nonce: str, option_index: int) -> tuple[str, str]:
        now = self.clock()
        with self._lock:
            issued = self._nonces.pop(nonce, None)
        if not issued or issued["expires"] < now:
            raise ValueError("notification answer expired or was already used")
        if option_index not in issued["indices"]:
            raise ValueError("option was not offered by this notification")
        if (self.watcher.is_stale()
                or not self.watcher.pane_parse_valid(issued["pane_id"])):
            raise ValueError("pane state is temporarily unavailable")
        if (self.watcher.pane_input_generation(issued["pane_id"])
                != issued["input_generation"]):
            raise ValueError("the pane received newer input")
        pane = next((dict(s) for s in self.watcher.states
                     if s.get("pane_id") == issued["pane_id"]), None)
        birth = self.watcher.pane_birth(issued["pane_id"])
        if pane is None or contract(pane, birth)[0] != issued["fingerprint"]:
            raise ValueError("the pending question has changed")
        if not birth:
            raise ValueError("the pane has changed")
        question = pane.get("question")
        if not isinstance(question, dict):
            raise ValueError("the pending question has changed")  # noqa: TRY004
        keys = option_keys(question, option_index)
        def guard() -> None:
            if (self.watcher.pane_input_generation(issued["pane_id"])
                    != issued["input_generation"]):
                raise ValueError("the pane received newer input")
            # Reserve the generation while the pane lock is held. Even if two distinct
            # valid nonces somehow coexist, only the first guard can pass.
            self.watcher.invalidate_input_actions(issued["pane_id"])
        # A menu commits on its shortcut; an Enter would confirm the NEXT menu's default.
        tmux.send_keys(
            issued["pane_id"], keys, enter=question.get("answer_style") != "menu", literal=True,
            expected_pid=birth, guard=guard,
        )
        self.watcher.request_reparse(issued["pane_id"])
        return issued["pane_id"], keys

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                # Keep notification evaluation off the server event loop.
                await asyncio.to_thread(self.evaluate)
            except Exception:
                logger.warning("push evaluation failed", exc_info=True)

    def evaluate(self) -> None:
        if self._stopping.is_set() or self.watcher.is_stale():
            return
        now = self.clock()
        panes = [dict(s) for s in self.watcher.states]
        active: set[tuple[str, str]] = set()
        candidates = []
        for pane in panes:
            if (pane.get("activity") != "waiting"
                    or pane.get("waiting_on") == "external"):
                continue
            pane_id = pane.get("pane_id")
            if (not isinstance(pane_id, str)
                    or not self.watcher.pane_parse_valid(pane_id)):
                continue
            fp, question = contract(pane, self.watcher.pane_birth(pane_id))
            active.add((pane_id, fp))
            previous = self._stable.get(pane_id)
            if previous is None or previous[0] != fp:
                self._stable[pane_id] = (fp, now)
                continue
            if now - previous[1] >= SETTLE_SECONDS:
                candidates.append((pane, fp, question))

        cleared = set(self._notified) - active
        self._notified.intersection_update(active)
        self._stable = {pid: value for pid, value in self._stable.items()
                        if (pid, value[0]) in active}
        # Keep only the rolling 15-minute cap. Pane ids are recycled, so the birth token
        # is part of the bucket key and expired historical panes disappear entirely.
        for key, bucket in list(self._rates.items()):
            while bucket and now - bucket[0] >= RATE_WINDOW_SECONDS:
                bucket.popleft()
            if not bucket:
                self._rates.pop(key, None)
        with self._lock:
            self._presence = {k: ts for k, ts in self._presence.items()
                              if now - ts < PRESENCE_SECONDS}
            for nonce, issued in list(self._nonces.items()):
                if issued["expires"] < now or (issued["pane_id"], issued["fingerprint"]) in cleared:
                    self._nonces.pop(nonce, None)

        for pane, fp, question in candidates:
            pane_id = pane["pane_id"]
            marker = (pane_id, fp)
            if marker in self._notified:
                continue
            rate = self._rates[(pane_id, self.watcher.pane_birth(pane_id))]
            if len(rate) >= RATE_MAX:
                continue
            offered = renderable_options(question)[:2] if question else []
            # A menu with no widget rows (one taller than classify reads, or none) can't
            # tell one "Do you want to proceed?" from the next, so its nonce could approve
            # a later command; an approval not yet restated (a failed call, retried) would
            # be approved blind. Either way the notification only opens the app.
            style = (question or {}).get("answer_style")
            if (style == "cursor" or (style == "menu" and not question.get("context"))
                    or (style == "menu" and is_approval(question) and not question.get("ask"))
                    or not self.watcher.pane_birth(pane_id)):
                offered = []
            nonce = secrets.token_urlsafe(24) if offered else None
            if nonce:
                with self._lock:
                    self._nonces[nonce] = {
                        "pane_id": pane_id, "fingerprint": fp,
                        "indices": {index for index, _ in offered},
                        "input_generation": self.watcher.pane_input_generation(pane_id),
                        "expires": now + NONCE_SECONDS,
                    }
            deep_link = {"pane": pane_id, "from": "push"}
            if (question and question.get("answer_style") != "cursor"
                    and not renderable_options(question)):
                deep_link["compose"] = "1"
            url = "/m#" + urlencode(deep_link)
            payload = {
                "title": _push_text(
                    pane.get("title") or pane.get("label") or pane_id, 100
                ),
                # A widget's plain restatement ("Do you want to proceed?" alone says
                # nothing; classify._restate), else the prompt itself.
                "body": _push_text((
                    (question or {}).get("ask")
                    or (question or {}).get("prompt")
                    or pane.get("headline")
                    or "Needs your attention"
                ), 400),
                "tag": f"block:{pane_id}",
                "url": url,
                "nonce": nonce,
                "actions": [{"action": f"answer:{index}", "title": _push_text(text, 50)}
                            for index, text in offered],
            }
            if self._stopping.is_set() or not self.sender.send(payload):
                if nonce:
                    with self._lock:
                        self._nonces.pop(nonce, None)
                continue
            self._notified.add(marker)
            rate.append(now)
