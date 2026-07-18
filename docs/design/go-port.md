# Porting the daemon to Go

Status: **draft / for consideration**. Research note weighing a rewrite of the
tmux-rc daemon (`daemon/*.py`) from Python to Go — the module-by-module port cost, the
tmux integration question (shell out vs control mode vs library), the Gemini/Vertex
story in Go, the sharp edges, and a phased recommendation. No code changes proposed
here; this is a decision document.

## The short version

**Port it — but not yet, and not because Python is failing.** The current daemon is
~2,270 lines across nine files, none of it exotic, and every dependency has a first-class
Go equivalent. A port is *feasible* and would take on the order of one to two focused
weeks. The honest question is not "can we" but "what does it buy, and when." The answer:
the payoff is real but almost entirely **downstream** — it lands when the hosted/tunnel
future in [cloud-architecture.md](cloud-architecture.md) gets built, because that world
is already specified as a Go relay, and a Go daemon lets the two share a protocol, a
binary-shipping story, and one language. Today, on a single-user single-host tool, Go's
advantages (static binary, goroutine concurrency) are **marginal-to-nice**, not
load-bearing. So the recommendation is a **strangler-fig, HTTP-surface-first** port timed
to the cloud work, keeping the watcher — the subtle part — in Python until last.

On the tmux question specifically: **keep shelling out.** Control mode is a real
temptation and the wrong tool here; the reasoning is below and it is the most decision-worthy
part of this doc.

## Why this is even on the table

The stated motivation is "Python is a liability." It's worth being precise about what
that means, because it changes the recommendation. Two readings:

1. **Operational liability** — the daemon runs unattended for days and Python's failure
   modes (a hung ADC refresh freezing the event loop, dependency drift, the venv/`uv`
   launch surface) have caused real incidents. This is true, but note that *every one of
   those incidents was fixed in Python* — the 20s Vertex timeout, the SA-key auth, the
   planned systemd unit. None of them were caused by the language; they were caused by
   running a service as a shell job with interactive credentials, and
   [deployment.md](deployment.md) + [durable-vertex-auth.md](durable-vertex-auth.md)
   already close them. Go would not have prevented them and does not, by itself, fix them.

2. **Strategic liability** — the *future* is Go. The cloud relay
   ([cloud-architecture.md](cloud-architecture.md)) is specified as "a single Go Cloud Run
   service," and that doc even sketches the daemon's uplink as a new module. If the relay
   is Go and the daemon is Python, the shared protocol lives in two languages, the JSON
   message types are hand-mirrored, and the "reference tunnel relay (standalone Go binary)"
   promised as the open-source artifact can't share code with the daemon it talks to. *This*
   is the liability worth acting on — and it's a reason to port the daemon **around the time
   the relay is built**, not in isolation now.

The rest of this doc assumes the strategic reading, because the operational one doesn't
justify a rewrite on its own.

## Module-by-module: what a port actually involves

The daemon is small and cleanly separated. Here is each file, what it does, and its Go
shape. Line counts are current.

### `tmux.py` (416 LOC) — the CLI wrapper

Shells out to `tmux` for `capture-pane`, `send-keys`, `list-panes`, `display-message`,
and friends, plus a self-contained ANSI/SGR/OSC-8 parser that marks dim runs (`⟪dim⟫`)
and materializes hyperlinks, plus clipboard-image plumbing (`wl-copy`/`xclip`) and a
loginctl lock-state probe.

**Go shape:** `os/exec` for the tmux and clipboard subprocess calls — a direct,
idiomatic translation. The interesting part is the ANSI/SGR state machine (`_mark_dim`,
`_materialize_links`, `_ANSI`): it's ~150 lines of careful regex + a hand-rolled parser,
and it's **load-bearing** — the dim-marking is what lets the LLM distinguish an unsent
draft from executed output. Go's `regexp` uses RE2, which lacks a couple of niceties
(no backreferences — not needed here) but is otherwise fine; the state machine ports
line-for-line. This is the module where a port is most likely to introduce a *subtle*
behavioral difference, so it needs golden-file tests against real captures. **The tmux
integration decision (below) lives here.**

### `watcher.py` (577 LOC) — the observation loop, the heart

An `asyncio` loop that ticks every 1.5s, captures each pane on a worker thread
(`asyncio.to_thread`), fingerprints it (stripping volatile timer/spinner churn so
"merely working" costs zero LLM calls), decides whether to re-parse, and maintains a
dozen per-pane in-memory dicts (snapshot ring, events log, burst history, idle summary,
tool-stickiness, bootstrap state, pane-id-recycle detection).

**Go shape:** a single goroutine driving a `time.Ticker` loop, with the per-pane state
held in structs behind a `sync.Mutex` (the HTTP handlers read `watcher.states` and the
events log concurrently — Python gets away with the GIL making list/dict reads atomic;
Go must take an explicit lock, and this is the one place the port adds real code the
Python doesn't have). The `asyncio.to_thread(self._tick)` pattern — offload blocking
work so the loop stays responsive — maps naturally: in Go the capture/LLM/subprocess
calls are *already* blocking-and-fine because they run on their own goroutine, so the
"push blocking work off the event loop" dance largely **disappears** (there is no event
loop to protect). That's a genuine simplification. The dozen per-pane maps become fields
on a `pane` struct in one `map[string]*pane`, which is cleaner than Python's parallel
dicts and their `_stores()` tuple that exists only to keep them from drifting.

This is the module with the most *domain* subtlety (the fingerprint regex, the tool
stickiness time-bounding, the event dedup guard, pane-id recycling) — not
*language* subtlety. Port it last, with the existing behavior as the oracle.

### `server.py` (434 LOC) — FastAPI surface

FastAPI app: serves the PWA static files, `/api/state`, `/api/digest`, per-pane
`/events`, `/snapshots`, `/peek`, `/send`, `/select`, `/image`, plus a no-cache
middleware, an audit-logging helper with an IAP-identity trust model, and the
image-upload/staging path.

**Go shape:** `net/http` with the standard `ServeMux` (Go 1.22+ has method+path patterns
like `POST /api/panes/{id}/send`, which covers the entire routing surface — **no router
dependency needed**; chi/echo would be gratuitous for ~9 routes). The pieces:

- **Static PWA:** `http.FileServer` + `embed.FS` (see go:embed below) — one line, and it
  lets the whole daemon ship as a single binary with the PWA baked in.
- **JSON responses:** `encoding/json`. The daemon pipes *raw LLM JSON dicts* straight
  through (`classify.py` is explicit that "the LLM is the parser AND the schema" — there
  is no typed model in the hot path). In Go that means the state is
  `map[string]any` / `json.RawMessage`, marshaled as-is. This is the one spot where Go's
  static typing fights the design: the whole point of the current pipe is that a new
  prompt field lights up in the frontend with zero backend change, and Go's instinct
  would be to define a struct. **Resist it** — keep the passthrough as `json.RawMessage`
  and the property survives. (`models.py`'s `PaneState` is already vestigial — the code
  pipes dicts, not `PaneState` — so there is nothing to translate there anyway.)
- **Multipart image upload:** `r.ParseMultipartForm` / `r.FormFile` — the direct
  analogue of `python-multipart`'s `UploadFile`. The size cap, mime check, and disk
  staging are straightforward.
- **The no-cache middleware and audit trust model** port directly; the audit
  `X-Tunnel-User`-only-from-loopback logic is just header + peer-IP checks.

FastAPI's ergonomic wins (automatic body validation from Pydantic, dependency injection)
buy little here because the surface is tiny and mostly untyped-passthrough anyway. Net: a
lateral move, slightly more boilerplate, no lost capability.

### `llm.py` (329 LOC) — the Gemini/Vertex client

Wraps `google-genai` (Vertex backend, ADC/SA-key auth), with a per-call timeout, a
shared 429 backoff, JSON-with-trailing-garbage salvage, running token/cost totals, a
JSONL metrics log, and a trace log. See the dedicated LLM section below — this is the
module with the most external-dependency risk, and the news is good.

### `classify.py` (139 LOC) — prompt assembly + passthrough

Loads the two `.txt` prompts (re-reading on mtime change so edits take effect under
reload), assembles the payload (prior frames + already-reported events + the tmux
foreground-process anchor line), calls the LLM function, and applies the *one* piece of
non-model logic kept out of the prompt ("a question/rewind means waiting").

**Go shape:** trivial string assembly. The mtime-reload dance becomes unnecessary if the
prompts are embedded (they'd be recompiled in), but keeping a dev-mode disk re-read is a
few lines. No dependency, no subtlety.

### `telemetry.py` (213 LOC) — OTLP export

Builds an OTLP-logs pipeline (gRPC) to the qsi-automation receiver, emitting one record
per parse / action / pane-lifecycle event, with a fail-closed privacy gate
(`otel_opt_in`, `TMUXRC_QSDEBUG`).

**Go shape:** the **OpenTelemetry Go SDK is first-class** — arguably more mature than
Python's (`go.opentelemetry.io/otel` + the OTLP/gRPC log exporter). The record-building
is field-mapping. One caveat: the code uses the OTel *logs* signal (not traces), which is
GA in Go but worth confirming the exporter version. Low risk, direct port.

### `render.py` (90 LOC) — ANSI-to-PNG

A small SGR parser that rasterizes a colored capture to a PNG via Pillow, for the vision
path. **Currently dead weight in the hot path** — `classify.py` states research showed
text beats image on accuracy/cost/latency, so "the image switch stays wired but off."

**Go shape:** `image`, `image/png`, and `golang.org/x/image/font` for monospace glyph
drawing — a direct Pillow analogue. But given it's *off*, the right move is **don't port
it in the first pass**; leave the image path unimplemented until it's actually turned on.

### `models.py` (70 LOC) — Pydantic models

`PaneState`, `Question`, `Rewind` etc. As noted, **these are vestigial** — the pipeline
pipes raw LLM JSON, not typed models. In Go they'd become structs *if* you wanted typed
validation, but the design deliberately doesn't. Skip them; they're documentation of the
shape at most.

### Totals

Roughly 2,270 LOC of Python → an estimated 2,500–3,000 LOC of Go (Go is more verbose:
explicit error handling, struct definitions, the mutex the GIL made free). No module is a
research project. The two that carry *domain* risk are `tmux.py` (the ANSI parser) and
`watcher.py` (the fingerprint/dedup/stickiness heuristics), and both are risky in the
"subtle behavior change" sense, not the "hard to write" sense — which is exactly what a
golden-file test corpus of real captures neutralizes.

## The tmux question (the decision that matters most)

We currently shell out to the tmux CLI: one `capture-pane` per pane per tick, plus
`send-keys` on input. The user asked specifically whether Go should keep doing that, move
to **control mode** (`tmux -CC`, the `%begin`/`%output` protocol), or use a **library**.
Verdict first, then why.

**Keep shelling out (`os/exec`). Reject control mode. Skip the libraries.**

### Shelling out is correct here, not a compromise

The architecture's founding idea is *observe the terminal, don't integrate with the
agent* — "the daemon is an observer with a keyboard." `capture-pane` is the purest
expression of that: it is **non-disruptive and stateless**, works from a wholly unrelated
process, and — critically — **a human can stay attached to the same session at the same
time.** The daemon holds no pty and no attachment. That property is load-bearing for the
whole product (you watch your agent from your phone *while sitting at the same tmux*), and
it is a direct consequence of the one-shot CLI model. Go's `os/exec` does this exactly as
Python's `subprocess` does. The per-tick fork cost is one `capture-pane` per pane at
1.5s cadence — trivial, and if anything Go's process spawn is cheaper than CPython's.

### Why control mode is the tempting wrong answer

Control mode (`tmux -CC` / `control-mode`) is a persistent protocol: you open **one**
connection to the tmux server and it **pushes** you `%output` notifications as panes
change, with `%begin`/`%end`/`%error` framing command replies. On paper this is
attractive for exactly the reasons the user intuits: no per-tick fork, and *push instead
of poll* — the server tells you when a pane changed instead of you asking 1.5 times a
second. iTerm2's tmux integration is built on it, so it's proven.

It is still the wrong fit, for four concrete reasons:

1. **Control mode is an attached client.** `tmux -CC` *attaches* to a session — it is a
   client, occupying a client slot and a "current" window, receiving output as a
   consumer. That breaks the founding non-disruptive property: the daemon stops being an
   invisible observer and becomes a participant in the session's client state (window
   sizing, `client-attached` hooks, the active-client notion). The whole design rests on
   the daemon *not* being an attached client. This alone is close to disqualifying.

2. **`%output` is a byte stream, not a screen.** Control mode pushes you raw pane
   *output bytes* as they're written — you'd have to run a **terminal emulator** over that
   stream to reconstruct the visible grid, because the watcher reasons about the *rendered
   screen* ("a pane is a fixed-width character grid; treat alignment/columns as
   meaningful," per the parser prompt). `capture-pane` hands you the emulated grid for
   free — tmux already did the emulation. Control mode would make us reimplement tmux's
   terminal emulator to get back to where `capture-pane` starts. That is a massive step
   backward for this workload.

3. **Push doesn't help — the fingerprint already solves the poll cost.** The reason
   push-vs-poll would matter is to avoid wasteful work when nothing changed. But the
   watcher *already* has that: the fingerprint strips volatile churn, so a merely-working
   agent (spinner + ticking timer) produces **zero LLM calls** despite repainting
   constantly. The expensive thing (the LLM parse) is already gated on real content
   change; the cheap thing (a `capture-pane` per tick) is negligible. Push would fire
   *more* often than the current parse cadence (every spinner repaint is `%output`),
   giving us data we deliberately throw away. It optimizes the part that was never the
   cost.

4. **Statefulness is a liability for an always-on daemon.** A persistent control-mode
   connection is one more thing to hold open, detect the death of, and re-establish —
   precisely the failure class [deployment.md](deployment.md) is trying to *remove*. The
   stateless one-shot model degrades perfectly: tmux gone → empty pane list → keep
   polling → recover automatically. That resilience is a feature we'd be trading away.

Control mode is the right tool for building a *terminal multiplexer client* (iTerm2). We
are building the opposite — an out-of-band observer that must not perturb the session.

### The Go tmux libraries: skip them

There are several — `GianlucaP106/gotmux`, `jubnzv/go-tmux`, `owenthereal/tmux`,
`wricardo/gomux`. **They are all thin `os/exec` wrappers over the same CLI we already
call**, oriented toward *session/window/pane management* (create, split, kill, rename) —
not toward the two verbs we actually use (`capture-pane -e -J -p` and `send-keys`). None
provides the thing that's actually hard in our `tmux.py`: the ANSI/SGR dim-marking and
OSC-8 materialization, which is *our* domain logic, not tmux plumbing. Adopting a library
would mean taking a dependency to save the easy 20% (`exec.Command("tmux", …)`) while
still hand-writing the hard 80%, and inheriting whatever field-format assumptions the
library baked in (our `list-panes -F` format string is tuned to tab-separate exactly the
fields we need, including `pane_pid` for id-recycle detection). Control-mode support in
these libraries, where it exists at all, is nascent and unproven for a long-running
observer. **Verdict: a ~40-line internal `tmux` package wrapping `os/exec` — the direct
translation of today's `tmux.py` — beats every option.** Zero dependency, full control of
the format strings, and the parser logic ports as-is.

## Gemini in Go: the LLM port

Good news across the board.

**The SDK exists and is the sanctioned one.** `google.golang.org/genai` (googleapis/go-genai)
is the official Go GenAI SDK and the *direct analogue* of Python's `google-genai` — same
team, same concepts. Critically, the older `cloud.google.com/go/vertexai/genai` was
**deprecated in June 2025** in favor of it, so there's no ambiguity about which to use.
It supports the Vertex backend (`genai.NewClient` with a `Backend: genai.BackendVertexAI`
config carrying project + location), `Models.GenerateContent` with text and inline-bytes
parts, `GenerateContentConfig` with `SystemInstruction`, `Temperature`, and
`ResponseMIMEType: "application/json"` — i.e. **every option `llm.py` uses today has a
1:1 counterpart.**

**Auth ports cleanly and the durable-auth work carries over untouched.** The whole point
of [durable-vertex-auth.md](durable-vertex-auth.md)'s recommendation (option A, the
long-lived SA key via `GOOGLE_APPLICATION_CREDENTIALS`) is that it's **credential
resolution, not code** — google-auth honors that env var with zero daemon code. Go's
`golang.org/x/oauth2/google` / the GenAI client's default credential chain honor
`GOOGLE_APPLICATION_CREDENTIALS` **identically**: it's Application Default Credentials, a
cross-language Google standard. So the SA-key durable-auth fix is already language-neutral
and needs *no rework* in Go. That's a real point in the port's favor — the hardest-won
operational fix transfers for free.

Rough edges, all minor:

- **The timeout-unit assertion becomes unnecessary.** `llm.py` has a defensive check that
  google-genai's `HttpOptions.timeout` is still in milliseconds (a real hazard in a
  dynamically-typed SDK where a unit flip would silently turn 20s into hours). In Go you
  pass a `time.Duration` or a `context.WithTimeout` — the type *is* the unit, so the
  entire class of bug the assertion guards against **can't occur**. A small, genuine win
  for correctness.
- **JSON-with-trailing-garbage salvage.** `_parse_json` handles flash-lite occasionally
  emitting a valid object then repeating it. Go's `json.Decoder.Decode` reads exactly one
  value and stops — so "take the first object, ignore the tail" is the *default* behavior
  of a streaming decoder, not special-case code. Another edge that gets *easier*.
- **Prompt caching** (the parser prompt is stable so it hits Gemini's context cache) is a
  server-side property of sending a stable prefix; it's unaffected by client language.
- **Streaming / TTFT.** The telemetry leaves `ttft` null on the non-streaming path today.
  If a future benchmark wants real TTFT, Go's `GenerateContentStream` returns an iterator
  — equivalent to Python's, so that door is equally open in either language.
- **The classification pass itself** (the `parser_prompt.txt` layered prompt) is pure
  prompt engineering — entirely language-independent. It doesn't "port"; it's a text file.

Net: the LLM module is the one with the most external-dependency exposure, and it comes
out **cleaner in Go** — the two nastiest bug classes (`timeout` unit, trailing JSON) are
structurally prevented, and the durable-auth investment transfers unchanged.

## Sharp edges and risks

- **Pydantic → structs, but mostly N/A.** The instinct is "Pydantic models become Go
  structs with JSON tags." The reality is the hot path is untyped passthrough
  (`json.RawMessage`), so there's little to translate. The `SendBody` request model
  (`keys`, `enter`, `literal`) *does* become a small struct with `json:"..."` tags and
  manual defaulting (Go zero-values `bool` to `false`, but `literal` and `enter` default
  *true* — so the struct needs pointer fields or an explicit unmarshal to preserve the
  "absent means true" semantics). A small, real gotcha worth calling out: Go's zero-value
  defaults are the opposite of what two of these three fields want.
- **The `.txt` prompts → `go:embed`.** `parser_prompt.txt` and `bootstrap_prompt.txt` are
  force-included in the Python wheel via a hatchling `force-include`. In Go this is
  `//go:embed parser_prompt.txt` — *simpler and more robust* than the wheel machinery,
  and it's compile-time verified (a missing file fails the build). Same for the PWA
  static dir via `embed.FS`. This is a place Go is strictly better: the single-binary
  embed removes the entire "did the data file make it into the package" failure mode
  (which [deployment.md](deployment.md) notes bit the daemon — running from a worktree
  that got deleted). **A single self-contained binary can't lose its prompt files or its
  PWA.**
- **The reload-on-edit dev loop is lost.** Python's `uvicorn --reload` restarts the
  process on source edits — the current dev loop. Go has no built-in equivalent; you'd
  add `air` or `wgo` (third-party file-watch-recompile), or accept a manual
  `go run`/rebuild. Given the daemon rebuilds its state from tmux in a couple ticks, a
  fast `go build && restart` is tolerable but it *is* a small ergonomic regression, and
  the prompt-mtime-reread trick in `classify.py` (edit the prompt, see it live without
  restart) doesn't survive embedding. Mitigation: in dev, read prompts from disk; in the
  release binary, embed. A few lines, worth it.
- **The GIL-free concurrency needs explicit locks.** Called out under `watcher.py`: the
  HTTP handlers and the watcher goroutine share the state/events structures, and Go won't
  make those reads atomic for free. This is *new code with no Python analogue* — a
  `sync.RWMutex` around the per-pane state — and it's the single most likely place to
  introduce a data race. `go test -race` in CI is the mitigation and should be
  non-negotiable.
- **Not a reason to *stop*, but to sequence:** the two behavior-subtle modules
  (`tmux.py`'s ANSI parser, `watcher.py`'s heuristics) mean a big-bang rewrite risks a
  hard-to-spot regression in exactly the logic that took the most tuning (the fingerprint
  volatile-set, the tool-stickiness window, the event dedup). This argues for
  strangler-fig phasing (below), not against porting.

There is no module here that is a *reason not to port* — nothing depends on a
Python-only library with no Go equivalent, nothing uses a dynamic-language feature the
design needs. The risks are all "subtle behavioral regression," which is a testing
problem, not a blocker.

## Does Go actually help the future? (honest accounting)

The user's premise is that the docs' plan already assumes a Go server. Weighing each
claimed benefit against the actual workload:

- **Shared language with the relay — real and the main prize.** This is the one that
  matters. [cloud-architecture.md](cloud-architecture.md) specifies the relay as Go and
  even names the promised open-source artifact "reference tunnel relay (standalone Go
  binary)." With a Go daemon, the WebSocket uplink protocol (the `hello`/`state`/`command`
  JSON messages) is **defined once and shared** between daemon and relay; the message
  structs, the auth token handling, the reconnect logic can be one package. With a Python
  daemon, that protocol is hand-mirrored across two languages and drifts. If the cloud
  future gets built, this is a substantial, ongoing simplification — and it's the honest
  reason to port.

- **Single static binary for deployment — nice, modestly load-bearing.**
  [deployment.md](deployment.md) is moving to a systemd user unit, and its problem
  statement is full of "which checkout did the shell cd into," "binary lived in /tmp,"
  "data file didn't make it into the package." A single Go binary with the PWA and
  prompts embedded genuinely dissolves several of those: `go build` produces one artifact
  with no venv, no `uv sync`, no interpreter, no force-include, no worktree-relative asset
  paths. `git pull && systemctl restart` becomes `deploy one binary && restart`. This is
  a real (if unglamorous) win — though note the systemd unit already tames most of the
  *operational* pain in Python, so Go **sharpens** the deploy story rather than **rescuing**
  it.

- **Concurrency for many panes/sessions — marginal.** This is the weakest argument, so be
  honest about it. The workload is a `capture-pane` + occasional LLM call per pane at 1.5s
  cadence. A power user has, what, 14 sessions ([deployment.md](deployment.md)'s example)
  — dozens of panes. That is *nothing* for either language; Python's `asyncio` +
  `to_thread` already handles it without breaking a sweat, and the LLM latency (hundreds
  of ms) dwarfs any per-pane overhead. Goroutines are a *cleaner model* for it (the
  watcher becomes N independent goroutines instead of a serial per-pane loop, and the
  event-loop-protection dance vanishes), but they don't unlock a scale the tool needs.
  Don't port for concurrency; port for the shared-language and single-binary reasons and
  *enjoy* the cleaner concurrency model as a bonus.

- **Raw speed / memory — irrelevant.** The daemon is I/O-bound on tmux and Vertex. CPU
  and RAM are not constraints and won't be. Not a reason.

So: one strong reason (shared language with the specified-Go relay), one solid reason
(single-binary deploy sharpening an already-improving story), and two marginal ones
(concurrency model, speed) that are bonuses, not justifications. That's a real case — but
it's a case that **matures when the cloud work does**, which is what the phasing reflects.

## Recommendation and phasing

**Port: yes, conditionally and incrementally — timed to the cloud/relay work, not before.**
Porting the daemon in isolation *today* spends one-to-two weeks to arrive at feature
parity with a slightly-more-verbose codebase and a marginally-better deploy story — a
lateral move. The port pays off when it's done *alongside* the Go relay, so the two share
a protocol and a language. So: don't schedule it as "rewrite the daemon"; schedule it as
"build the cloud future in Go, and bring the daemon along."

**Phasing — strangler fig, HTTP surface first, watcher last:**

1. **Stand up the Go skeleton and the HTTP surface.** `net/http` server, `embed.FS` PWA,
   the read endpoints (`/api/state`, `/api/digest`, `/events`, `/snapshots`, `/peek`) and
   the write endpoints (`/send`, `/select`, `/image`). This is the lowest-risk, most
   mechanical part, and it validates the whole shape (JSON passthrough, multipart, static
   embed) early.
2. **Port `tmux.py` into an internal `tmux` package** (`os/exec` + the ANSI/dim/OSC-8
   parser) with a **golden-file test corpus** of real captures asserting byte-identical
   output vs the Python parser. This is where correctness is won or lost; do it under test.
3. **Port `llm.py` + `classify.py` + the prompts (go:embed).** Verify parses match the
   Python daemon on the same captures. The SDK and auth carry over cleanly (above).
4. **Port `telemetry.py`** (OTel Go SDK) — the receiver doesn't care which language
   emitted the record; run both daemons pointing at it and diff the records.
5. **Port `watcher.py` last** — the fingerprint, dedup, stickiness, bootstrap, and
   id-recycle heuristics — with the Python watcher's behavior as the oracle, ideally
   running both against the same tmux and comparing `/api/state` output for a session.
   Add `go test -race` from day one for the shared-state locking.
6. **Build the relay uplink in the same Go module**, sharing the protocol types — the
   payoff step, and the reason the whole port was worth doing.

**Strangler option:** because the daemon is stateless-over-tmux (tmux is the system of
record; caches rebuild in a couple ticks), you can run the Go and Python daemons *side by
side against the same tmux on different ports* throughout — the perfect A/B oracle. Cut
over the tunnel-client's `localhost` target from the Python port to the Go port when the
Go one matches. No data migration, no flag day.

**What to keep in Python: nothing, eventually — but `render.py` never gets ported** until
the image path is actually turned on (it's off today), and `models.py` simply
disappears (vestigial). The `research/probe.py` tooling and any offline analysis scripts
can stay Python indefinitely — they're not the daemon and share only the prompt files,
which are language-neutral text.

**Effort:** ~1–2 focused weeks for daemon parity (steps 1–5), dominated by step 2
(the parser, under golden tests) and step 5 (the watcher heuristics, under an A/B oracle).
Step 6 folds into the cloud-architecture estimate rather than adding to this one. The
risk is entirely regression-in-subtle-heuristics, and the side-by-side strangler setup is
the mitigation that makes it low.

## Where I'm uncertain (and what I'd do anyway)

- **Timing is a judgment call.** If the cloud/relay work is *not* imminent, the strong
  reason to port evaporates and it's a lateral move — I'd **wait**. If the relay is on the
  near roadmap, I'd start the daemon port to converge with it. My default, absent a firm
  relay date: **do the systemd-unit + SA-key durability work in Python now** (already
  planned, closes the operational pain), and **hold the Go port until the relay is
  scheduled** — then do them together.
- **The ANSI parser is the sleeper risk.** I'm confident it *ports*; I'm less confident it
  ports without a subtle dim-marking edge case that only shows up on some agent's specific
  SGR sequences. I'd de-risk this hard with a golden corpus captured from real Claude
  Code / Codex / Gemini panes *before* committing to the port, because if that parser is
  fussier than it looks, it changes the effort estimate more than anything else here.
- **Keep the untyped JSON passthrough.** The single most important thing to get right in
  the port is *not* succumbing to Go's urge to type the pane state. The "LLM is the
  schema, new fields light up with no backend change" property is a real architectural
  asset, and `json.RawMessage` preserves it. If a future maintainer types it into a struct,
  they'll have quietly broken the thing that makes the parser prompt cheap to evolve.
