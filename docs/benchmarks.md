# LLM Benchmarks

Ad-hoc measurements of the hot-path classifier (`daemon/classify.py`'s `PARSER_PROMPT`)
across models/providers. The question that started this: *can a faster-inference provider
(Cerebras/Groq) beat Gemini Flash Lite's latency?*

**Short answer: no — not for this workload.** The premise was that specialty-silicon
providers stream output 10–20× faster, so a parse would be 3–4× quicker. Measured live,
that's wrong here, and *why* it's wrong is the useful part.

## Why provider speed doesn't move our latency

Our latency is dominated by **input prefill + time-to-first-token**, not output
generation:

- The request is ~2,900 input tokens — a large static parser prompt plus a couple of
  prior frames for continuity. That prefill, plus a ~0.5–0.8s TTFT that every provider
  pays, sets the floor.
- Flash Lite emits only ~220 output tokens. There's almost no generation time for a
  faster tokens/sec to save.

So a provider can win the part of the request that barely costs us (output streaming) and
still not beat us on the part that does (prefill + TTFT). The fast-silicon models make it
worse for themselves: the ones Cerebras serves today are **reasoning models** that emit
3–5× more tokens (hidden reasoning), spending their TPS advantage and landing back at
parity.

## Measured (2026-07, live)

Same sample (`research/samples/working.txt`, a working Claude Code pane), same
`PARSER_PROMPT`, `temperature=0`, JSON mode. Non-streaming total latency unless noted.

| Model | Provider | Median total | Out tokens | Result |
|---|---|---:|---:|---|
| gemini-3.1-flash-lite | Vertex (global) | **1.52s** (min 1.18s) | 221 | ✅ correct; TTFT ~0.7s |
| gpt-oss-120b | Cerebras | 1.32s | 1065 | ✅ correct; reasoning-heavy; 429 on 2nd call |
| zai-glm-4.7 | Cerebras | 1.61s | 699 | ✅ best JSON quality; slightly slower than Flash |
| gemma-4-31b | Cerebras | 1.87s | 3 | ❌ returned `{}` — likely a `response_format` quirk, not a proven accuracy failure |

Notes:
- **Cerebras free-tier rate limits bite immediately.** 5 req/min and 30k tokens/min —
  and our input is ~2,900 tokens, so ~10 calls/min is the token ceiling. The second
  gpt-oss call already 429'd. Fine for a spot check, unusable for multi-pane real use
  without a paid tier.
- **gemma-4-31b `{}`** is unverified — Gemma may need a different JSON invocation. Don't
  read it as "Gemma is bad at the task" without a retry.
- Token counts are provider-reported (`usage`) for Cerebras/Vertex; the old
  `research/README.md` numbers used a different prompt and aren't comparable.

## The real latency levers

If latency matters, the evidence says attack the **input**, not the provider:

- Trim `PRIOR_FRAMES` (`daemon/watcher.py`) — fewer continuity frames = less prefill.
- Shrink the static `PARSER_PROMPT`. It's the bulk of the 2,900 tokens.
- Output is already small; little to gain there.

A genuinely faster path would be a *non-reasoning* small model on fast silicon, but
Cerebras's current menu is reasoning-heavy (gpt-oss, glm) or returned empty (gemma).

## Methodology / reproducing

These were one-off `python` probes, not a committed harness (we deliberately dropped the
synthetic-harness idea — see `docs/design/`). Real-world comparison is done by switching
the daemon's model live and reading per-call OTLP telemetry in QueryStory (metrics by
default; pane text + output JSON under `TMUXRC_QSDEBUG=1`). Cerebras is OpenAI-compatible
(`base_url=https://api.cerebras.ai/v1`); Flash Lite runs via Vertex (`project=qs-backend-dev`,
`location=global`). Both used the live `PARSER_PROMPT` and a saved sample.
