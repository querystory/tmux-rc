"""OTLP telemetry: one log record per LLM parse, for real-world model benchmarking.

Every classify call emits a record (model, latency, TTFT, tokens, cost, activity, …)
to the qsi-automation otel-receiver (../qsi-automation/otel-receiver — Cloud Run, OTLP/
gRPC, Bearer auth) which flattens it to JSONL in GCS → QueryStory. That lets us switch
TMUXRC_GEMINI_MODEL during real work and compare latency/cost/accuracy in QS instead of
running synthetic benchmarks.

Privacy is client-side and fail-closed by policy:
  - DEFAULT: numeric metrics + structural fields + a HASH of the pane text. No pane
    text, no model output. (The receiver keeps whatever we send once opted in — so the
    gate lives HERE, not on the server.)
  - TMUXRC_QSDEBUG=1: additionally attach raw pane text and the model's output JSON, for
    cross-model accuracy diffing.
The receiver REDACTS everything unless `otel_opt_in=true` is a resource attribute, so we
always set it — otherwise even the metrics land zeroed.

Wholly optional: with no OTEL endpoint configured, every function here is a no-op, so the
daemon runs identically off-network. Telemetry must never break a parse.

Config (env):
  OTEL_EXPORTER_OTLP_ENDPOINT   receiver URL (gRPC). Unset ⇒ telemetry disabled.
  OTEL_EXPORTER_OTLP_HEADERS    e.g. "authorization=Bearer <token>"
  TMUXRC_QSDEBUG=1              attach raw pane text + output JSON (default: off)
Get endpoint/token:
  gcloud run services describe otel-receiver --region us-central1 --project qsi-automation --format 'value(status.url)'
  gcloud secrets versions access latest --secret=otel-receiver-token --project=qsi-automation
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import time
from functools import cache

logger = logging.getLogger(__name__)

# Scope name tags our records so QueryStory can filter them apart from Claude Code's
# telemetry sharing the same receiver/table.
_SCOPE = "tmux-rc.classify"
# Live-view rounds get their OWN scope so QueryStory can filter them apart from parse
# benchmarks: a live round has no model/latency/tokens, and folding it into emit_parse
# would poison every parse aggregate with null-model rows. See docs/design/live-telemetry.md.
_LIVE_SCOPE = "tmux-rc.live"
# Browser-side failures (mic denial, ws onclose, poll catch, uncaught exceptions) reported
# by the client — own scope so "how often does Live Mode fail to get the mic, on what
# platforms" doesn't fold into parse/live aggregates. See server._api_client_error.
_CLIENT_SCOPE = "tmux-rc.client"
_QSDEBUG = os.environ.get("TMUXRC_QSDEBUG") == "1"


@cache
def _provider():
    """Build the OTLP logs pipeline once, or return None if telemetry is disabled.

    Disabled (⇒ None) when no OTLP endpoint is configured, or if the OTel SDK isn't
    importable — either way the emit_* functions become no-ops. Cached so we build one
    provider (one exporter, one batch processor) per process; per-scope loggers hang
    off it (see _logger)."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    try:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource

        # otel_opt_in=true is REQUIRED or the receiver strips all attributes + zeroes
        # metrics (privacy-by-default is server-enforced). service.name identifies us.
        resource = Resource.create(
            {
                "service.name": "tmux-rc",
                "otel_opt_in": "true",
                "host.name": socket.gethostname(),
            }
        )
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        return provider
    except Exception:  # noqa: BLE001 - telemetry setup must never break the daemon
        logger.warning("OTLP telemetry setup failed; disabling", exc_info=True)
        return None


@cache
def _logger(scope: str = _SCOPE):
    """The OTel logger for `scope`, or None if telemetry is disabled. Cached per scope
    so live rounds (tmux-rc.live) tag apart from parse benchmarks (tmux-rc.classify)
    for QueryStory filtering; all scopes share the one provider. Get the logger from OUR
    provider instance rather than the process-global (set_logger_provider) so we can't
    clobber another component's OTel logging, and avoid the 'provider already set' warning."""
    p = _provider()
    return p.get_logger(scope) if p is not None else None


def _emit_record(body: str, attrs: dict, scope: str = _SCOPE) -> None:
    """Emit one OTLP log record with these attributes under `scope`. No-op if telemetry
    is disabled; any failure is swallowed. The single place that touches the SDK — every
    emit_* goes through here so the record shape stays consistent."""
    lg = _logger(scope)
    if lg is None:
        return
    try:
        from opentelemetry._logs import LogRecord, SeverityNumber

        now = time.time_ns()
        lg.emit(
            LogRecord(
                timestamp=now,
                observed_timestamp=now,
                severity_number=SeverityNumber.INFO,
                body=body,
                attributes=attrs,
            )
        )
    except Exception:  # noqa: BLE001 - never let telemetry break the daemon
        logger.debug("telemetry emit failed", exc_info=True)


def emit_action(
    *,
    action: str,
    pane_uid: str,
    actor: str,
    detail: str | None,
    keys: str | None,
    outcome: str = "ok",
) -> None:
    """Audit record for a state-CHANGING request (send-keys / select / image paste), so
    "what is making changes to my terminals, and who?" is answerable from telemetry.
    `actor` is the IAP-authenticated email the tunnel relay forwards (X-Tunnel-User,
    honored only from loopback — see server._audit's trust model) or 'local:<ip>' for
    direct requests. `outcome` distinguishes completed actions from refused/failed
    attempts. Key content attaches only under TMUXRC_QSDEBUG — stricter than pane_text
    in spirit: keys can carry no-echo secrets that pane capture never sees. The
    action/actor/pane/outcome skeleton is always sent."""
    attrs = {"event": action, "pane_uid": pane_uid, "actor": actor, "outcome": outcome}
    if detail:
        attrs["detail"] = detail[:200]
    if _QSDEBUG and keys is not None:
        attrs["keys"] = keys[:500]
    _emit_record("tmux-rc action", attrs)


def emit_pane_event(*, event: str, pane_uid: str, label: str, tool: str | None) -> None:
    """Emit a pane-lifecycle record (event='pane_created' | 'pane_removed'). Lets a query
    reconstruct which panes existed when — "active now" = a pane_uid with a created and no
    later removed — instead of inferring liveness from "a parse exists" (which never
    expires when a pane closes). `pane_uid` is the same stable id stamped on every parse,
    so lifecycle and parse records join cleanly. Structural only (no pane content)."""
    attrs = {"event": event, "pane_uid": pane_uid, "pane_label": label}
    if tool:
        attrs["tool"] = tool
    _emit_record("tmux-rc pane", attrs)


def emit_parse(
    *,
    model: str,
    provider: str,
    pane_uid: str,
    pane_label: str,
    pane_text: str,
    output: dict | None,
    latency: float,
    ttft: float | None,
    in_tokens: int,
    out_tokens: int,
    cost: float,
    activity: str | None,
    changed: bool,
    error: str | None = None,
    kind: str = "parse",
) -> None:
    """Emit one benchmark record for a classify call. No-op if telemetry is disabled.

    Sends numeric metrics + stable pane identity (pane_uid/pane_label) + a hash of the
    pane text always; raw pane text + `output` JSON only under TMUXRC_QSDEBUG. `changed`
    distinguishes a content-change parse from a heartbeat re-parse. Best-effort."""
    try:
        # tps is generation-phase throughput (out_tokens / generation time), NOT
        # out_tokens/latency — total latency includes prefill + TTFT and would understate
        # it (per docs/hint.md). Needs ttft to isolate generation; when ttft is absent
        # (the non-streaming Vertex path) tps stays None rather than reporting a wrong
        # number.
        gen_s = (latency - ttft) if (ttft is not None and latency > ttft) else None
        tps = (out_tokens / gen_s) if (gen_s and out_tokens) else None
        attrs = {
            "model": model,
            "provider": provider,
            # Stable pane identity on EVERY record (not just QSDEBUG): lets a query group
            # by pane and join to the lifecycle events, so "currently active panes" works
            # on default metrics without the screen-scraped output_json.session hack.
            "pane_uid": pane_uid,
            "pane_label": pane_label,
            "latency_s": round(latency, 4),
            "changed": changed,
            # Hash lets us group repeated parses of the same screen without sending text.
            "input_sha256": hashlib.sha256(pane_text.encode()).hexdigest(),
            # "parse" = live screen classification; "bootstrap" = the one-time deep
            # scrollback digest (big in_tokens — exclude from parse-latency benchmarks).
            "kind": kind,
        }
        # On a failed parse, OMIT token/cost so they read as NULL/absent (per hint.md:
        # "exclude error IS NOT NULL" for success-path metrics) rather than skewing
        # aggregates with zeros.
        if error is None:
            attrs["in_tokens"] = in_tokens
            attrs["out_tokens"] = out_tokens
            attrs["cost_usd"] = round(cost, 6)
        else:
            attrs["error"] = error[:200]
        if ttft is not None:
            attrs["ttft_s"] = round(ttft, 4)
        if tps is not None:
            attrs["tps"] = round(tps, 1)
        if activity:
            attrs["activity"] = activity
        if _QSDEBUG:  # accuracy-diff mode: attach the actual content
            attrs["pane_text"] = pane_text
            if output is not None:
                attrs["output_json"] = json.dumps(output, ensure_ascii=False)
    except Exception:  # noqa: BLE001 - never let telemetry break a parse
        logger.debug("emit_parse attr build failed", exc_info=True)
        return
    _emit_record("tmux-rc parse", attrs)


def emit_live(
    *,
    session: str | None,
    pane_uid: str,
    pane_label: str,
    tool: str | None,
    hold_s: float,
    changed: bool,
    raw_bytes: int | None = None,
    actor: str | None = None,
) -> None:
    """One record per completed live-view long-poll round (docs/design/live-telemetry.md).

    A round is one iteration of the /live handler: it holds ~25s, capturing every 250ms,
    and ends either on a screen CHANGE (changed=True, a frame was sent — raw_bytes set)
    or on the idle HOLD timeout (changed=False, nothing sent — raw_bytes omitted so byte
    sums stay honest). Because the client re-holds instantly, a continuous viewer is a
    chain of back-to-back rounds, so summing hold_s per (session, pane) is an
    undercount-only floor for watch-time — the billing signal.

    `session` is the client's per-page-load UUID (the summable spine, anonymous — not
    identity). When it's absent (a caller that didn't send one) the round is left
    UN-ATTRIBUTABLE — no session key at all — so rollups EXCLUDE it rather than mis-sum
    it: collapsing every session-less viewer under one shared id would fold unrelated
    viewers' watch-time together and corrupt the per-session billing signal. The
    invariant is that two different viewers never share a session id, and the null case
    honors it by attributing to none. `actor` is the loopback-trusted tunnel email when
    present (the account key for per-user rollups), recorded only when the caller already
    vetted the trust model. NEVER carries frame text — this is about cost and time, not
    content (privacy is fail-closed, same as emit_parse). Own scope (tmux-rc.live) so
    parse aggregates stay clean. Best-effort: attr-build failure is swallowed, never
    breaking the live path."""
    try:
        attrs = {
            "pane_uid": pane_uid,
            "pane_label": pane_label,
            "hold_s": round(hold_s, 3),
            "changed": changed,
        }
        # session is a client-supplied query param — cap it like actor/detail so a
        # crafted value can't inflate OTLP payloads / downstream storage (a real
        # per-page-load UUID is ~36 chars; 64 leaves headroom). Absent ⇒ omit the key so
        # the round is un-attributable, never merged under a shared placeholder.
        if session:
            attrs["session"] = session[:64]
        if tool:
            attrs["tool"] = tool
        # Bytes only on a change round (an idle round sent nothing). sent_bytes is the
        # gzipped size the middleware logs — we record raw here and let that log line
        # carry ground-truth sent bytes (see design open-questions), so no double gzip
        # in the hot loop.
        if changed and raw_bytes is not None:
            attrs["raw_bytes"] = raw_bytes
        if actor:
            attrs["actor"] = actor[:200]
    except Exception:  # noqa: BLE001 - never let telemetry break the live path
        logger.debug("emit_live attr build failed", exc_info=True)
        return
    _emit_record("tmux-rc live", attrs, _LIVE_SCOPE)


def emit_live_turn(
    *,
    session: str,
    actor: str | None,
    model: str,
    in_tokens: int,
    out_tokens: int,
    audio_in_tokens: int,
    audio_out_tokens: int,
    cost: float,
    turns: int,
    duration_s: float,
    final: bool,
    transcript: str | None = None,
) -> None:
    """One record per Live Mode voice turn (and a final summary on session end), the
    voice-session analogue of emit_parse.

    Live Mode's cost is dominated by audio tokens, which the flash-lite parser never
    sees — so it gets numeric token/cost metrics of its OWN under the live scope, keyed
    by the client's `session` UUID (the summable spine, same anonymous key emit_live
    uses) so a query can sum a whole session's spend and join it to the live-round
    watch-time. Tokens are split text/audio because native-audio bills them at very
    different rates. `final` marks the end-of-session summary row (cumulative totals),
    distinct from the per-turn rows.

    Privacy mirrors emit_parse exactly: numbers + structure always; the actual
    conversation `transcript` (what was said and typed) attaches ONLY under
    TMUXRC_QSDEBUG — voice content is at least as sensitive as pane text. Best-effort."""
    try:
        attrs = {
            "kind": "live_turn",
            "model": model,
            "provider": "vertex",
            "session": session[:64],
            "turns": turns,
            "duration_s": round(duration_s, 3),
            "final": final,
            "in_tokens": in_tokens,
            "out_tokens": out_tokens,
            "audio_in_tokens": audio_in_tokens,
            "audio_out_tokens": audio_out_tokens,
            "cost_usd": round(cost, 6),
        }
        if actor:
            attrs["actor"] = actor[:200]
        if _QSDEBUG and transcript:
            attrs["transcript"] = transcript[:8000]
    except Exception:  # noqa: BLE001 - never let telemetry break the live path
        logger.debug("emit_live_turn attr build failed", exc_info=True)
        return
    _emit_record("tmux-rc live", attrs, _LIVE_SCOPE)


def emit_client_error(
    *,
    kind: str,
    name: str | None,
    endpoint: str | None,
    ua_class: str | None,
    session: str | None,
    actor: str | None,
    message: str | None,
) -> None:
    """One record per browser-side failure the client reports (docs/design or issue #57).

    `kind` is the reporting site ('mic', 'ws', 'poll', 'onerror', 'unhandledrejection');
    `name` the error's class (NotAllowedError, TypeError, …); `endpoint` the URL/path it
    failed against; `ua_class` a coarse platform bucket. These STRUCTURAL fields plus the
    anonymous `session` and loopback-trusted `actor` are always sent — enough to answer
    "how often, on what platform, for whom" without any content.

    Privacy mirrors emit_parse: the free-text `message` (which can echo URLs, pane text,
    or user input) attaches ONLY under TMUXRC_QSDEBUG. Best-effort — a report must never
    break the request that carries it."""
    try:
        attrs = {"kind": kind}
        for k, v in (("name", name), ("endpoint", endpoint), ("ua_class", ua_class)):
            if v:
                attrs[k] = v[:200]
        if session:
            attrs["session"] = session[:64]
        if actor:
            attrs["actor"] = actor[:200]
        if _QSDEBUG and message:
            attrs["message"] = message[:2000]
    except Exception:  # noqa: BLE001 - never let telemetry break the request
        logger.debug("emit_client_error attr build failed", exc_info=True)
        return
    _emit_record("tmux-rc client error", attrs, _CLIENT_SCOPE)
