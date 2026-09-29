# Live Mode evaluation scripts

The scripts that answered "which voice model, and does the whole loop actually work?" for
[Live Mode](../../docs/design/live-mode.md). None of them touch a real tmux pane: the
probe and harnesses never reach tmux code (the harnesses only inspect the model's tool
call), and the two smokes run the daemon against a fake watcher with `send_keys` mocked.
All of them call paid provider APIs, so none run in `make test`; run them by hand when the
question they answer comes up again.

Run from the repo root. Credentials come from two places, never the command line: the
repo `.env` (`GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS` for Vertex) and
`~/.config/tmux-rc/openai.env` (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`,
`AZURE_OPENAI_ENDPOINT`), which lives outside every checkout so no worktree can commit a
key. See `.env.example` for both.

## probe_vertex_live.py — which Live model IDs does Vertex serve us?

Model names in the Live catalogue come and go faster than the docs, and a wrong ID can
hang rather than fail. This connects to each candidate on Vertex in `us-central1`, sends
one text turn, and prints OK/FAIL/HANG with connect time, time to first audio, and turn
time. Run it before adding a Vertex entry to `TMUXRC_LIVE_MODELS`. Needs the Vertex
credentials from `.env`. With no arguments it tries a built-in candidate list; pass IDs
to probe your own.

    uv run python research/live-eval/probe_vertex_live.py [model ...]

## harness_gemini.py / harness_openai.py — can the model drive panes by tool call?

The same six text-turn cases against a fake four-pane snapshot: does the model call
`type_in_pane` with the right pane and keys, and call nothing when only asked a
question? Each case prints PASS/FAIL and latency to the tool call. This is what showed
every candidate reasons well enough, so the choice comes down to voice quality, which
no harness hears. Run it when a new model or provider appears. The Gemini harness takes
a model and `vertex` (default; Vertex credentials) or `apikey` (`GEMINI_API_KEY`). The
OpenAI harness reuses the Gemini cases over the Realtime WebSocket; its model defaults
to `gpt-realtime-2.1` and its backend to `openai` (`OPENAI_API_KEY`); `azure` needs
`AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_ENDPOINT`, and the model is the deployment name.

    uv run python research/live-eval/harness_gemini.py <model> [vertex|apikey]
    uv run python research/live-eval/harness_openai.py [model] [openai|azure]

## smoke_live_ws.py — does the daemon's Live loop work end to end?

The harnesses skip audio and the daemon; this does not. It synthesizes one spoken
command with OpenAI TTS, streams it as browser-shaped 16 kHz frames into the daemon's own
`/api/live-mode` WebSocket, and checks the full round trip: the ambient tmux update
draws no reply, speech leads to a typed action, the turn completes, and the meter
records a nonzero cost. It exits 1 on failure, so a shell can gate on it. Run it after
changing `openbus/live.py` or a provider adapter. The argument is a picker label
(default `GPT 2.1 mini`) that must be an offered entry in `TMUXRC_LIVE_MODELS`, read
from the environment or the repo `.env`; `OPENAI_API_KEY` is always needed for the TTS,
plus whatever the chosen entry's backend needs.

    TMUXRC_LIVE_MODELS='[...]' uv run python research/live-eval/smoke_live_ws.py "<label>"

## smoke_gpt_live.py — does the GPT-Live session work end to end?

GPT-Live owns a whole session rather than a provider connection, so it bypasses the
path `smoke_live_ws.py` covers and gets its own smoke. It speaks "In window one, type
echo hello and press enter." to a fake one-pane terminal and asserts speech, delegation,
exactly one guarded action, a spoken confirmation after it, final usage, and returned
audio, then prints the cost. Run it after changing `openbus/gpt_live.py`. Needs
`OPENAI_API_KEY` and `ffmpeg`; roughly 20 seconds of billable Live audio. An optional
path saves the model's reply as a WAV.

    uv run python research/live-eval/smoke_gpt_live.py [/tmp/live-smoke.wav]
