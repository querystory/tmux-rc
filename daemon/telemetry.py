"""OTLP telemetry: one log record per LLM parse, for real-world model benchmarking.

Every classify call emits a record (model, latency, TTFT, tokens, cost, activity, …)
to the qsi-automation otel-receiver (../qsi-automation/otel-receiver — Cloud Run, OTLP/
gRPC, Bearer auth) which flattens it to JSONL in GCS → QueryStory. That lets us switch
TMUXRC_MODEL between providers during real work and compare latency/cost/accuracy in QS
instead of running synthetic benchmarks.

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
_QSDEBUG = os.environ.get("TMUXRC_QSDEBUG") == "1"


@cache
def _logger():
    """Build the OTLP logs pipeline once, or return None if telemetry is disabled.

    Disabled (⇒ None) when no OTLP endpoint is configured, or if the OTel SDK isn't
    importable — either way emit_parse becomes a no-op. Cached so we build one provider
    per process."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    try:
        from opentelemetry._logs import get_logger, set_logger_provider
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
        set_logger_provider(provider)
        return get_logger(_SCOPE)
    except Exception:  # noqa: BLE001 - telemetry setup must never break the daemon
        logger.warning("OTLP telemetry setup failed; disabling", exc_info=True)
        return None


def emit_parse(
    *,
    model: str,
    provider: str,
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
) -> None:
    """Emit one benchmark record for a classify call. No-op if telemetry is disabled.

    Sends numeric metrics + a hash of the pane text always; raw pane text + `output`
    JSON only under TMUXRC_QSDEBUG. `changed` distinguishes a content-change parse from a
    heartbeat re-parse. Fully best-effort — any failure is swallowed."""
    lg = _logger()
    if lg is None:
        return
    try:
        from opentelemetry._logs import LogRecord, SeverityNumber

        tps = (out_tokens / latency) if latency > 0 and out_tokens else None
        attrs = {
            "model": model,
            "provider": provider,
            "latency_s": round(latency, 4),
            "in_tokens": in_tokens,
            "out_tokens": out_tokens,
            "cost_usd": round(cost, 6),
            "changed": changed,
            # Hash lets us group repeated parses of the same screen without sending text.
            "input_sha256": hashlib.sha256(pane_text.encode()).hexdigest(),
        }
        if ttft is not None:
            attrs["ttft_s"] = round(ttft, 4)
        if tps is not None:
            attrs["tps"] = round(tps, 1)
        if activity:
            attrs["activity"] = activity
        if error:
            attrs["error"] = error[:200]
        if _QSDEBUG:  # accuracy-diff mode: attach the actual content
            attrs["pane_text"] = pane_text
            if output is not None:
                attrs["output_json"] = json.dumps(output, ensure_ascii=False)

        now = time.time_ns()
        lg.emit(
            LogRecord(
                timestamp=now,
                observed_timestamp=now,
                severity_number=SeverityNumber.INFO,
                body="tmux-rc parse",
                attributes=attrs,
            )
        )
    except Exception:  # noqa: BLE001 - never let telemetry break a parse
        logger.debug("emit_parse failed", exc_info=True)
