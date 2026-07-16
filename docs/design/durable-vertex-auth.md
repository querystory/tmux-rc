# Durable Vertex auth for the daemon

Status: **proposed** (not yet implemented). Tracks GitHub issue: daemon Vertex auth wedge.

## Problem

The tmux-rc daemon classifies panes by calling Gemini Flash Lite on Vertex. It
authenticates with bare **Application Default Credentials** (`daemon/llm.py` →
`genai.Client(vertexai=True, project=…)` with no explicit credentials). Developer ADC is
short-lived: the access token expires, and the reauth-proof token (RAPT) expires on a
policy cadence. When it lapses, every classify call fails until a human runs
`gcloud auth application-default login` on the host.

This is not hypothetical — it caused **two production incidents in one session**:

1. **Daemon Vertex ADC (the ~9h silent wedge).** Overnight the ADC token needed a refresh;
   the google-genai client had no request timeout, so the refresh **hung** the parse
   thread. The watcher loop `await`s that thread, so it stopped ticking entirely — every
   card frozen for ~9h with `errors: 0` and nothing logged (a blocking hang never reaches
   the `except` that records errors). Mitigated by adding a 20s client timeout (a hang now
   becomes a caught, recorded error instead of a freeze) — but **the daemon still can't
   classify until someone re-auths on the host.** The timeout made it loud and recoverable,
   not self-healing.

2. **Receiver GCS ADC (`invalid_rapt`).** Separately, the local otel bench receiver used
   developer ADC for its GCS uploads; the RAPT expired and every flush 401'd
   (`invalid_grant` / `reauth related error`). Fixed durably by giving the receiver a
   **long-lived service-account key** (`otel-receiver-service@qsi-automation`, which
   already had `roles/storage.objectCreator` on the bucket) via
   `GOOGLE_APPLICATION_CREDENTIALS`. That closed the receiver side.

The daemon side is still on ADC — same class of failure, unaddressed. This doc is the plan
to close it.

## Why ADC is the wrong credential here

ADC is designed for interactive developer use and short-lived tokens with periodic reauth.
The daemon is a **long-running unattended service** — exactly the workload ADC is not for.
A credential that requires a human to periodically re-run a browser login is structurally
incompatible with "runs for days classifying panes." The tunnel service (`qsi-automation/
tunnel`) already learned this and moved to long-lived SA credentials for the same reason.

## Options

### A. Long-lived service-account key (recommended)

Create a dedicated SA (e.g. `tmux-rc-classifier@qs-backend-dev`), grant it the minimal
Vertex role (`roles/aiplatform.user`), mint a JSON key, and point the daemon at it via
`GOOGLE_APPLICATION_CREDENTIALS` in `.env`. google-genai/google-auth honors that env var
with **zero daemon code change** (it's the first entry in the credential resolution chain).

- **Pros:** never expires; no reauth; no human in the loop; mirrors the proven tun +
  receiver pattern; zero code change (env-only).
- **Cons:** a long-lived key file on disk (mitigate: `chmod 600`, outside the repo under
  `~/.config/tmux-rc/`, gitignored path in `.env`). Key rotation is a manual/periodic
  chore.
- **Note:** Vertex/Gemini runs in **`qs-backend-dev`** (that's `GOOGLE_CLOUD_PROJECT`), so
  the SA + role binding + key are in that project — distinct from the receiver's SA, which
  is in `qsi-automation` (where the bucket lives).

### B. SA impersonation via developer ADC

Daemon impersonates a Vertex SA using the developer's ADC to mint short-lived tokens.

- **Pros:** no key file on disk.
- **Cons:** **does not solve the problem** — it still depends on the developer's ADC being
  fresh, so it wedges on the same reauth expiry. This is tun's "convenient for a short
  session" path that it explicitly warns drops on timeout. Rejected.

### C. Browser re-auth flow in the phone UI

On auth failure, surface a re-auth link in the PWA; user taps → completes gcloud OAuth in
the browser they're already holding → daemon resumes.

- **Pros:** self-service recovery without SSH-to-host; no SA key to manage; elegant given
  the UI is already a browser.
- **Cons:** real implementation cost (OAuth callback wiring, token handling in the daemon);
  still reactive (auth *does* lapse, user just recovers faster). Good as a **fallback/UX
  layer**, not the primary fix.

## Recommendation

**Do A (long-lived SA key)** as the primary durable fix — it's the same pattern already
proven on the receiver and tun, needs no daemon code, and structurally ends the expiry
class. Optionally layer **C** later as a nicer recovery UX for any residual auth failure
(e.g. key revoked/rotated). **B is rejected** (doesn't fix the root cause).

Pair the SA with the **actionable auth-expired UI banner** (already scoped): when
`last_error` indicates an auth failure, the PWA should show a distinct, prominent
"LLM auth expired — re-authenticate" state instead of the current subtle error, so any
residual failure is loud and obvious rather than a silent stale-card freeze.

## Implementation sketch (option A)

1. `gcloud iam service-accounts create tmux-rc-classifier --project=qs-backend-dev …`
2. `gcloud projects add-iam-policy-binding qs-backend-dev --member=serviceAccount:… --role=roles/aiplatform.user`
3. `gcloud iam service-accounts keys create ~/.config/tmux-rc/vertex-sa.json --iam-account=…` ; `chmod 600`
4. Add `GOOGLE_APPLICATION_CREDENTIALS=~/.config/tmux-rc/vertex-sa.json` to `.env` (and
   document in `.env.example`). No `daemon/llm.py` change.
5. Verify: unset ADC in a shell, confirm the daemon still classifies (creds come from the
   key, not ADC).
6. Fold the SA-key launch into the durability work (systemd unit) so it's reproducible, not
   a one-off manual env var.

## Related

- **Receiver side already done:** SA-key GCS auth on `otel-receiver-service@qsi-automation`.
- **Timeout mitigation already merged** (PR #1): the 20s client timeout turns a hung refresh
  into a recoverable error — necessary but not sufficient; this SA plan is the real fix.
- **Durability (separate follow-up):** daemon + receiver are currently `setsid` background
  procs; they should be systemd units (survive reboot, auto-restart, env baked in). The SA
  key path belongs in that unit's environment.
