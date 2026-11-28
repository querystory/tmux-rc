"""Push delivery for panes that are blocked on the user.

The watcher remains the source of truth.  This module only routes stable, actionable
waits to subscribed devices and validates notification action replies against the
still-live answer contract before typing anything into tmux. What to send is decided
once (PushManager); how it reaches a device is its subscription's transport: Web Push
for a browser, APNs for the native iOS shell.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import itertools
import json
import logging
import os
import queue
import secrets
import string
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from pywebpush import WebPushException, webpush

from . import scratch, tmux
from .classify import is_approval

logger = logging.getLogger(__name__)

SETTLE_SECONDS = 5.0
RATE_WINDOW_SECONDS = 15 * 60.0
RATE_MAX = 3
NONCE_SECONDS = 10 * 60.0
MAX_SUBSCRIPTIONS = 16
READY_QUIET_SECONDS = 120.0  # after "Ready", a pane's Needs-you without answers says nothing new
LANDED_FRESH_SECONDS = 60.0  # a promised page that landed between two scans: still news
DEFAULT_PUSH_HOSTS = (
    "web.push.apple.com",
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    ".notify.windows.com",
)
APNS_HOSTS = {"development": "api.sandbox.push.apple.com", "production": "api.push.apple.com"}
APNS_TOKEN_SECONDS = 30 * 60.0  # Apple refuses one over an hour old, or renewed within 20 min
CONFIG_DIR = Path.home() / ".config" / "tmux-rc"  # provider credentials, outside every checkout


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


def _ident(subscription: dict) -> tuple:
    """What names one device's subscription: its transport, and that transport's address."""
    return subscription.get("kind", "web"), subscription.get("endpoint") or subscription.get(
        "token")


def _valid_subscription(subscription) -> bool:
    if not isinstance(subscription, dict):
        return False
    if subscription.get("kind") == "apns":  # a device token: hex, 32 bytes today
        token = subscription.get("token")
        return (isinstance(token, str) and 64 <= len(token) <= 200
                and set(token) <= set(string.hexdigits) and subscription.get("env") in APNS_HOSTS)
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


def _declarative(payload: dict, subscription: dict) -> dict:
    """The payload as Declarative Web Push for an Apple relay's device whose app origin is known.
    An iOS home-screen app often never runs the worker's notificationclick for a tap (WebKit bug
    268797), so a push that leaves its address to sw.js opens the app on its last screen; a
    declarative one is opened at `navigate` by iOS itself (18.4+; older iOS hands it to sw.js
    like any push). WebKit takes only an absolute `navigate`, hence the stored origin. Safari
    shows no actions, so answering from the notification loses nothing there. A chat tap stays
    the worker's: a navigation would reload away the chat that lives in the open page."""
    origin = subscription.get("origin")
    apple = urlparse(subscription["endpoint"]).hostname == "web.push.apple.com"
    if not origin or not apple or payload.get("chat"):
        return payload
    return {"web_push": 8030, "notification": {
        "title": payload["title"], "body": payload["body"], "tag": payload["tag"],
        "navigate": origin + payload["url"]}}


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
    """Owner-only, atomic persistence for VAPID keys and device credentials: a browser's
    Web Push subscription as the browser gave it, a native app's as its kind and token."""

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
            data = {"private_key": private, "public_key": public, "subscriptions": [],
                    "turns": {}}
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
        if not isinstance(data.get("turns"), dict):  # a store from before turns were kept
            data["turns"] = {}
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
            return copy.deepcopy(self._load()["subscriptions"])

    def upsert(self, subscription: dict) -> None:
        with self._lock:
            data = self._load()
            subscriptions = [
                item for item in data["subscriptions"] if _ident(item) != _ident(subscription)
            ] + [subscription]
            data["subscriptions"] = subscriptions[-MAX_SUBSCRIPTIONS:]
            self._save()

    def remove(self, subscription: dict) -> bool:
        with self._lock:
            data = self._load()
            old = data["subscriptions"]
            data["subscriptions"] = [item for item in old if _ident(item) != _ident(subscription)]
            changed = len(old) != len(data["subscriptions"])
            if changed:
                self._save()
            return changed

    def told(self, pane: str, turn: str | None) -> bool:
        """Was the user already told about this pane (id and birth) at this turn?"""
        with self._lock:
            return turn is not None and self._load()["turns"].get(pane) == turn

    def tell(self, pane: str, turn: str | None) -> None:
        """Remember `turn` as the pane's last one the user was told about, or that ended
        without an ask. Kept across restarts: the same turn read again is not news."""
        with self._lock:
            turns = self._load()["turns"]
            if turn is not None and turns.get(pane) != turn:
                turns[pane] = turn
                self._save()

    def keep(self, panes: set[str]) -> None:
        """Forget the turns of panes gone (not in `panes`): what bounds the store."""
        with self._lock:
            data = self._load()
            if not set(data["turns"]) <= panes:
                data["turns"] = {k: v for k, v in data["turns"].items() if k in panes}
                self._save()

    def revoke_all(self) -> int:
        with self._lock:
            data = self._load()
            count = len(data["subscriptions"])
            data["subscriptions"] = []
            if count:
                self._save()
            return count


class GoneError(Exception):
    """The push service says this device's subscription is dead: forget it."""


def _web(store: PushStore):
    def deliver(subscription: dict, payload: dict) -> None:
        try:
            webpush(
                subscription_info=subscription,
                data=json.dumps(_declarative(payload, subscription), separators=(",", ":")),
                vapid_private_key=store.keys()[0],
                # pywebpush mutates this dict with endpoint-specific aud/exp claims.
                vapid_claims={"sub": os.environ.get(
                    "TMUXRC_PUSH_SUBJECT", "mailto:tmux-rc@openbus.io")},
                timeout=(3, 7),
                ttl=600,
                headers={"Urgency": "high"},
            )
        except WebPushException as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) in {404, 410}:
                raise GoneError from exc
            raise
    return deliver


def _apns(config: Path, transport: httpx.BaseTransport | None = None):
    """Delivery to the native iOS shell, straight to Apple; None unless its credentials are
    configured: the app's signing key at `config`/apns.p8 and, in the environment (from
    openai.env, like every provider key), TMUXRC_APNS_KEY_ID, TMUXRC_APNS_TEAM_ID and
    TMUXRC_APNS_TOPIC (the app's bundle id). `transport` stands in for Apple in tests."""
    key_id, team, topic = (os.environ.get(f"TMUXRC_APNS_{name}")
                           for name in ("KEY_ID", "TEAM_ID", "TOPIC"))
    try:
        key = serialization.load_pem_private_key((config / "apns.p8").read_bytes(), None)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:  # unreadable, malformed, or encrypted:
        # a broken key must not take the daemon down with it
        logger.warning("apns.p8 is not a usable private key (%s); native push is off",
                       type(exc).__name__)
        return None
    if not (key_id and team and topic):
        logger.warning("apns.p8 needs TMUXRC_APNS_KEY_ID, _TEAM_ID and _TOPIC; native push is off")
        return None
    client = httpx.Client(http2=True, transport=transport, timeout=httpx.Timeout(7, connect=3))
    lock = threading.Lock()
    minted = [float("-inf"), ""]

    def bearer() -> str:
        with lock:
            if time.time() - minted[0] > APNS_TOKEN_SECONDS:
                signed = ".".join(_b64(json.dumps(part).encode()) for part in (
                    {"alg": "ES256", "kid": key_id}, {"iss": team, "iat": int(time.time())}))
                # JWS wants the bare r and s, not the DER the signer returns.
                pair = decode_dss_signature(key.sign(signed.encode(), ec.ECDSA(hashes.SHA256())))
                signature = _b64(b"".join(n.to_bytes(32, "big") for n in pair))
                minted[:] = time.time(), f"{signed}.{signature}"
            return minted[1]

    def deliver(subscription: dict, payload: dict) -> None:
        # Answer buttons are a category the shell registers; their titles and the nonce ride
        # along as custom keys, like the address a tap opens (`url`).
        aps = {"alert": {"title": payload["title"], "body": payload["body"]}, "sound": "default"}
        if payload.get("actions"):
            aps |= {"category": "answer", "mutable-content": 1}
        response = client.post(
            f"https://{APNS_HOSTS[subscription['env']]}/3/device/{subscription['token']}",
            json={"aps": aps} | {k: v for k, v in payload.items()
                                 if k not in ("title", "body") and v},
            headers={"authorization": f"bearer {bearer()}", "apns-topic": topic,
                     "apns-push-type": "alert", "apns-priority": "10",
                     "apns-expiration": str(int(time.time()) + 600),
                     # Like a Web Push tag: a pane's newer ask replaces its older one. Hashed
                     # because Apple caps the id at 64 bytes.
                     "apns-collapse-id": hashlib.sha256(payload["tag"].encode()).hexdigest()})
        if response.status_code == 200:
            return
        reason = response.json().get("reason") if response.content else None
        if response.status_code == 410 or reason in ("BadDeviceToken", "Unregistered"):
            raise GoneError
        raise RuntimeError(f"APNs {response.status_code} {reason}")
    return deliver


def _transports(store: PushStore, config: Path = CONFIG_DIR) -> dict:
    """How a payload reaches a device, by its subscription's kind. Only the transports this
    daemon is configured for: a kind missing here is refused at subscribe and skipped at send."""
    return {"web": _web(store)} | ({"apns": apns} if (apns := _apns(config)) else {})


class PushSender:
    """Small, lossy worker queue: push relay trouble must never stall the watcher."""

    def __init__(self, store: PushStore, transports: dict | None = None):
        self.store = store
        self.transports = _transports(store) if transports is None else transports
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
            subscriptions = self.store.subscriptions()
            if not subscriptions:
                continue
            # One slow relay/device must not serially delay every other subscribed device.
            with ThreadPoolExecutor(
                max_workers=len(subscriptions), thread_name_prefix="tmux-rc-push"
            ) as pool:
                futures = [pool.submit(self._deliver, subscription, payload)
                           for subscription in subscriptions]
                # _deliver contains and logs device-specific errors; never log an endpoint.
                sent = sum(future.result() for future in futures)
            if sent:
                logger.info("push sent to %d of %d devices: %s %r", sent, len(subscriptions),
                            payload.get("tag"), payload.get("title"))

    def _deliver(self, subscription: dict, payload: dict) -> bool:
        kind = _ident(subscription)[0]
        try:
            if kind not in self.transports:  # its credentials were taken away since
                return False
            self.transports[kind](subscription, payload)
            return True
        except GoneError:
            self.store.remove(subscription)
        except Exception as exc:  # noqa: BLE001 - whatever a relay does, the worker lives
            # Intentionally no retry: an answer prompt delayed by a relay
            # outage is stale, and draining old alerts later is worse.
            logger.warning("%s push delivery failed: %s", kind, exc)
        return False


def renderable_options(question: dict) -> list[tuple[int, str]]:
    options = question.get("options")
    if not isinstance(options, list):
        return []
    out = []
    for index, option in enumerate(options):
        # The row the model marks as opening the agent's own free-text field is left to
        # the composer, which is always there.
        if not isinstance(option, str) or not option.strip() or index == question.get("free_text"):
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
        # Likewise "Do 1 now?" over a different row 1: the body shows the rows it names.
        "items": question.get("items") if question else None,
        "subject": question.get("subject") if question else None,
        # The body is the restatement: one that arrives late (a retried call) re-notifies.
        "ask": question.get("ask") if question else None,
    }
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return digest, question


def _pane_key(pane: dict) -> str:
    """A pane across restarts: ids are recycled, so with its birth."""
    return f"{pane['pane_id']}:{pane.get('birth')}"


def held_question(watcher, pane_id: str, fingerprint: str) -> tuple[dict, str]:
    """The pane's held question and birth, only while it is still the one `fingerprint`
    (contract's digest) names: an answer tapped on one ask must never land on the next."""
    if watcher.is_stale() or not watcher.pane_parse_valid(pane_id):
        raise ValueError("pane state is temporarily unavailable")
    pane = next((dict(s) for s in watcher.states if s.get("pane_id") == pane_id), None)
    birth = watcher.pane_birth(pane_id)
    if pane is None or contract(pane, birth)[0] != fingerprint:
        raise ValueError("the pending question has changed")
    if not birth:
        raise ValueError("the pane has changed")
    question = pane.get("question")
    if not isinstance(question, dict):
        raise ValueError("the pending question has changed")  # noqa: TRY004
    return question, birth


def claim_question(watcher, pane_id: str, fingerprint: str, generation: int, frame: str) -> None:
    """Run under the pane's send lock, just before an answer's keys go out: refuse unless
    the pane still holds the question the answer was offered on (held_question), has taken
    no input since (the generation) and still shows the frame that question was parsed
    from (keyboard input never reaches the generation), then consume the generation, so
    only the first of two answers to one question can pass.

    Residual risk, accepted: the send lock orders only tmux-rc's own senders, and tmux
    has no compare-and-send, so a key typed at an attached client in the milliseconds
    between this capture and send-keys can still answer the question first (send_keys
    runs this guard last for that reason). Closing that gap needs the agent itself to
    check a prompt identity, which no supported agent offers."""
    if watcher.pane_input_generation(pane_id) != generation:
        raise ValueError("the pane received newer input")
    question, _ = held_question(watcher, pane_id, fingerprint)
    # The frame hashes the normalized screen plus the question's own rows verbatim, so
    # neither animation nor "sleep 10s" becoming "sleep 20s" fools it.
    try:
        text = tmux.capture_pane(pane_id, mark_dim=True)
    except subprocess.CalledProcessError as e:  # closed or timed out: nothing was sent
        raise ValueError("the pane has changed") from e
    if watcher.frame_fp(text, question) != frame:
        raise ValueError("the pending question has changed")
    watcher.consume_answer(pane_id, watcher.frame_fp(text), frame)


@contextmanager
def pane_input(watcher, pane_id: str, *, invalidate: bool = True):
    """Wrap one input attempt on a pane. Before it, invalidate push actions and app menu
    tokens ahead of competing for the pane's send lock. After it, unless a claim refused
    it, invalidate them again before that lock is released: a parse that read the
    generation while the keys were still going out captured the old screen, and no
    other sender may claim its token in between. Whatever the outcome, force a reparse:
    input changes the screen, so an answered question clears from its card within a
    capture, and only a fresh parse reissues a token. Never reparse before delivery:
    that parse could stamp the new generation on the old screen. `pane_id` must be
    canonical: the watcher matches its forced set against pane.id, and the send lock is
    keyed by it."""
    bump = getattr(watcher, "invalidate_input_actions", None)
    if invalidate and bump is not None:
        tmux.before_send(pane_id, lambda: bump(pane_id))
    try:
        with tmux._pane_lock(pane_id):  # noqa: SLF001 - reentrant; the delivery retakes it
            try:
                yield
            except ValueError:  # a refused claim (claim_question): no key went out
                bump = None
                raise
            finally:
                if bump is not None:
                    bump(pane_id)
    finally:
        if watcher is not None:
            watcher.request_reparse(pane_id)


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


PUSH_BODY_MAX = 400


def _mtime(path: Path) -> int | None:
    try:
        return path.stat(follow_symlinks=False).st_mtime_ns
    except OSError:
        return None


def _push_text(value, limit: int) -> str:
    """Bound encrypted payload size while keeping the full question in the deep link."""
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class PushManager:
    def __init__(self, watcher, store: PushStore | None = None, *, clock=time.monotonic,
                 sender=None, scratch: Path | None = None, shoot=lambda _item: None):
        self.watcher = watcher
        self.store = store or PushStore()
        self.sender = sender or PushSender(self.store)
        self.clock = clock
        self._stable: dict[str, tuple[str, float, str | None]] = {}  # fp, since, turn
        self._rates: dict[tuple[str, str | None], deque[float]] = defaultdict(deque)
        self._nonces: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._task: asyncio.Task | None = None
        self.scratch = scratch  # the scratch dir whose pending pages notify as they land
        self.shoot = shoot  # what queues a landed page's thumbnail (scratch_sidecars.queue)
        self._mtimes: dict[str, tuple] | None = None
        self._waiting: dict[tuple[str, int], str] = {}
        self._marked: dict[tuple[str, int], float] = {}  # when an update's mark was made
        self._done: set[tuple[str, int]] = set()
        self._ready: dict[tuple, float] = {}  # (pane, birth) -> when its "Ready" went out
        # The last pages that landed, as their push said it, for every open app (/api/state):
        # its notice and its Recent.
        self.news: deque[dict] = deque(maxlen=10)

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

    def subscribe(self, subscription: dict, origin: str | None = None) -> None:
        """`origin`: the app's, as the subscribing page sent it, for a declarative push's
        absolute address (_declarative); the page re-subscribes on every boot (push.js)."""
        kind = _ident(subscription)[0]
        if kind not in self.sender.transports:
            raise ValueError(f"{kind} push is not configured on this daemon")
        if not _valid_subscription(subscription):
            raise ValueError("invalid push subscription")
        if kind != "web":
            self.store.upsert({key: subscription[key] for key in ("kind", "token", "env")})
            return
        endpoint = subscription["endpoint"]
        keys = subscription["keys"]
        self.store.upsert({"endpoint": endpoint, "keys": {
            "p256dh": keys["p256dh"], "auth": keys["auth"],
        }} | ({"origin": origin} if origin and origin.startswith("https://") else {}))

    def chat(self, card: str, text: str) -> bool:
        """The chat assistant's card waits on a user who isn't looking (live._nudge). The tap
        brings back the chat holding that card id in the open page (sw.js); approving stays
        in the app. False when it was not queued (a full queue): the caller tries again."""
        return self.sender.send({"title": "Chat needs you", "body": _push_text(text, 400),
                          "tag": "chat", "url": "/m", "chat": card, "actions": []})

    def answer(self, nonce: str, option_index: int) -> tuple[str, str]:
        now = self.clock()
        with self._lock:
            issued = self._nonces.pop(nonce, None)
        if not issued or issued["expires"] < now:
            raise ValueError("notification answer expired or was already used")
        if option_index not in issued["indices"]:
            raise ValueError("option was not offered by this notification")
        if (self.watcher.pane_input_generation(issued["pane_id"])
                != issued["input_generation"]):
            raise ValueError("the pane received newer input")
        question, birth = held_question(self.watcher, issued["pane_id"], issued["fingerprint"])
        keys = option_keys(question, option_index)
        with pane_input(self.watcher, issued["pane_id"], invalidate=False):  # claim bumps
            # A menu commits on its shortcut; an Enter would confirm the NEXT menu's default.
            tmux.send_keys(
                issued["pane_id"], keys, enter=question.get("answer_style") != "menu",
                literal=True, expected_pid=birth, guard=lambda: claim_question(
                    self.watcher, issued["pane_id"], issued["fingerprint"],
                    issued["input_generation"], issued["frame"]),
            )
        return issued["pane_id"], keys

    async def _loop(self) -> None:
        for tick in itertools.count():
            await asyncio.sleep(1)
            # Needs-you each second; scratch, which stats every folder, every ten.
            for step in (self.evaluate, self.landed):
                if step == self.landed and tick % 10:
                    break
                try:
                    # Keep notification evaluation off the server event loop.
                    await asyncio.to_thread(step)
                except Exception:
                    logger.warning("push %s failed", step.__name__, exc_info=True)

    def landed(self) -> None:
        """Each tick, read again the scratch folders whose entries, or meta.json, changed
        (all of them at startup); one that was pending (only its meta.json, its page being
        made) and has its page now sends "Ready" to every device. So does a page whose
        meta.json says it was `created` under a minute ago: one made faster than a scan was
        never seen pending, but was promised all the same (a meta.json tmux-rc generated, or
        one half-written, says no such thing). A page published in one go was never promised
        and does not notify; nor does any folder already there at startup that was not
        pending. One whose meta.json said `updating` and no longer does sends "Updated", if its
        page was written since that mark: a meta.json changed alone is no update."""
        now = self.clock()
        if not self.scratch:
            return
        seen, self._mtimes = self._mtimes, {  # a meta.json finished in place: the folder's alone
            e.name: (e.stat(follow_symlinks=False).st_mtime_ns, _mtime(Path(e.path, "meta.json")))
            for e in os.scandir(self.scratch)}
        # Removed: forgotten.
        self._waiting = {k: v for k, v in self._waiting.items() if k[0] in self._mtimes}
        self._done = {k for k in self._done if k[0] in self._mtimes}
        self._marked = {k: v for k, v in self._marked.items() if k in self._waiting}
        # And an updating one each time: a fresh asset reads as pending for a minute, a change
        # in time alone that no mtime shows.
        waiting = {name for (name, _), verb in self._waiting.items() if verb == "Updated"}
        for name in [n for n, at in self._mtimes.items()
                     if (seen or {}).get(n) != at or n in waiting]:
            item = scratch.read(self.scratch, self.scratch / name)
            key = item and (name, item["inode"])
            if item:  # a folder replaced under its name: the old one's wait is over
                self._waiting = {k: v for k, v in self._waiting.items()
                                 if k[0] != name or k == key}
            if seen is None and item and not item["pending"]:
                self._done.add(key)  # there at startup: old news, however it changes later
            fresh = (item and key not in self._done and item["stamped"]
                     and time.time() - item["stamped"] < LANDED_FRESH_SECONDS)
            verb = self._waiting.get(key) or (fresh and "Ready")
            if item and (item["pending"] or item["revising"]):
                # a revision that drops its index for a moment is still an update
                self._waiting[key] = "Updated" if item["revising"] else self._waiting.get(
                    key, "Ready")
                if item["revising"]:
                    self._marked.setdefault(key, item["named"])
            elif verb == "Updated" and item["read"] and item["updated"] <= self._marked.get(key, 0):
                self._waiting.pop(key)  # its mark came and went, its page untouched: no news
            elif verb and item["read"] and (verb == "Updated" or item["href"].endswith("/")):
                if message := self._ready_push(item, now, verb):
                    # the app's cursor: a ms clock, strictly increasing however the clock steps
                    at = max(int(time.time() * 1000), self.news[-1]["at"] + 1 if self.news else 0)
                    self.news.append({**message, "at": at})
                    self.watcher.nudge()
                    self.shoot(item)  # the gallery's spinner polls for it
                    self._waiting.pop(key, None)
                    self._done.add(key)  # a later rewrite of its meta.json is not news
                else:  # a full queue: the next scan tries again, however old it is by then
                    self._waiting[key] = verb
                    self._mtimes.pop(name)

    def _ready_push(self, item: dict, now: float, verb: str = "Ready") -> dict | None:
        """Tell every device a pending page has landed, under a tag of its own: a page is news
        of its own, so it never silently replaces a Needs-you. A plain Needs-you right after
        it is skipped (evaluate), so one finish still buzzes once. An "Updated" page names no
        pane: its reviser need not be the pane that made it. The message once sent, or with
        no device to send it to; None for a full queue."""
        pane = verb == "Ready" and next((s for s in self.watcher.states
                                         if s.get("pane_id") == item["pane"]
                                         and scratch.made_in(s, item["created"])), None)
        body = item["description"] or (pane and f"{pane.get('tool')} finished it in "
                                       f"{scratch.where(pane)}")
        message = {
            "title": _push_text(f"{verb}: {item['title']}", 100),
            "body": _push_text(body or f"Its page is {verb.lower()}.", PUSH_BODY_MAX),
            "tag": f"scratch:{item['name']}",
            "url": "/m#" + urlencode({"view": "scratch", "open": f"/scratch/{item['href']}"}),
            "actions": []}
        sent = self.sender.send(message)
        if sent and pane:
            self._ready[(item["pane"], pane.get("birth"))] = now
        return message if sent or not self.store.subscriptions() else None

    def evaluate(self) -> None:
        if self._stopping.is_set() or self.watcher.is_stale():
            return
        now = self.clock()
        booted = self.watcher.booted()  # before the copy: then it lists every pane
        panes = [dict(s) for s in self.watcher.states]
        active: set[tuple[str, str]] = set()
        candidates = []
        for pane in panes:
            pane_id = pane.get("pane_id")
            if (not isinstance(pane_id, str)
                    or not self.watcher.pane_parse_valid(pane_id)):
                continue
            if (pane.get("activity") != "waiting"
                    or pane.get("waiting_on") == "external"):
                if pane.get("activity") in ("idle", "waiting"):  # it ended asking nothing
                    self.store.tell(_pane_key(pane), pane.get("turn"))
                continue
            # The snapshot's own birth: held_question checks it against the live one, so
            # a recycled pane id can never inherit this question.
            fp, question = contract(pane, pane.get("birth"))
            active.add((pane_id, fp))
            previous = self._stable.get(pane_id)
            if previous is None or (previous[0], previous[2]) != (fp, pane.get("turn")):
                self._stable[pane_id] = (fp, now, pane.get("turn"))
                continue
            if now - previous[1] >= SETTLE_SECONDS:
                candidates.append((pane, fp, question))

        if booted:  # the rest are gone
            self.store.keep({_pane_key(p) for p in panes if isinstance(p.get("pane_id"), str)})
        self._ready = {k: at for k, at in self._ready.items() if now - at < READY_QUIET_SECONDS}
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
            for nonce, issued in list(self._nonces.items()):
                if (issued["expires"] < now
                        or (issued["pane_id"], issued["fingerprint"]) not in active):
                    self._nonces.pop(nonce, None)

        for pane, fp, question in candidates:
            pane_id = pane["pane_id"]
            # Evidence, not wording: a turn already told about (before a restart, too) is
            # not news however its card reads now (re-read after a resize, by a new parser),
            # and a new one is, however alike.
            if self.store.told(_pane_key(pane), turn := pane.get("turn")):
                continue
            rate = self._rates[(pane_id, self.watcher.pane_birth(pane_id))]
            if len(rate) >= RATE_MAX:
                continue
            offered = renderable_options(question)[:2] if question else []
            # A widget's plain restatement ("Do you want to proceed?" alone says
            # nothing; classify._restate), else the prompt itself, then the list rows
            # it names ("Do 1 now?" alone says nothing either), led by what is being decided
            # when that still fits: the rows must, the subject is a bonus.
            items = (question or {}).get("items") or []
            # str(): raw model fields are unvalidated, and one bad headline must not stall
            # every pane's notifications.
            lines = [(question or {}).get("ask") or (question or {}).get(
                "prompt") or pane.get("headline") or "Needs your attention", *items]
            if (subject := (question or {}).get("subject")) and (
                    len("\n".join(map(str, lines))) + len(str(subject)) < PUSH_BODY_MAX):
                lines.insert(1, subject)
            body = "\n".join(map(str, lines))
            # A menu with no widget rows (one taller than classify reads, or none) can't
            # tell one "Do you want to proceed?" from the next, so its nonce could approve
            # a later command; an approval not yet restated (a failed call, retried) would
            # be approved blind. Nor is one whose named rows the body cannot fit. Either
            # way the notification only opens the app.
            style = (question or {}).get("answer_style")
            if (style == "cursor" or (style == "menu" and not question.get("context"))
                    or (style == "menu" and is_approval(question) and not question.get("ask"))
                    or (items and len(body) > PUSH_BODY_MAX)
                    or not self.watcher.pane_birth(pane_id)):
                offered = []
            if not question and (pane_id, pane.get("birth")) in self._ready:
                self.store.tell(_pane_key(pane), turn)  # a plain turn ended: "Ready" was the news
                continue
            nonce = secrets.token_urlsafe(24) if offered else None
            if nonce:
                with self._lock:
                    self._nonces[nonce] = {
                        "pane_id": pane_id, "fingerprint": fp,
                        "indices": {index for index, _ in offered},
                        # Both from the published snapshot the question came from, as
                        # /api/state takes them: a live generation would vouch for a
                        # stale frame whose successor's parse has not landed yet.
                        "input_generation": pane.get("input_generation", 0),
                        "frame": pane.get("frame", ""),
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
                "body": _push_text(body, PUSH_BODY_MAX),
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
            self.store.tell(_pane_key(pane), turn)
            rate.append(now)
