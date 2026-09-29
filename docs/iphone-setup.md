# iPhone happy path: Live Mode and notifications

Use **Safari or Chrome → Add to Home Screen → launch the installed tmux-rc app**
for the iPhone workflow. Do not enable push from an ordinary browser tab or an
embedded browser. Installation is required for iPhone Web Push; it is also the supported
path for our Live Mode + notifications workflow. It does not guarantee uninterrupted
voice when iOS locks or suspends the app.

## Set up once

1. Use iOS 16.4 or later. In **Safari or Chrome**, open your authenticated **HTTPS** tmux-rc
   address at `/m`. Sign in and confirm that your panes appear.
2. Open **Share → Add to Home Screen**. If offered, leave **Open as Web App**
   enabled, then tap **Add**.
3. Leave the browser and launch **tmux-rc from its Home Screen icon**. Sign in again if
   prompted. Use this installed app from now on, including when enabling permissions.
4. Tap the **bell** in the header and allow notifications when iOS asks. A question
   card inside the app is not proof that push permission or delivery works.
5. Open **Live Mode** with the microphone button, select an available model, and
   start the session. Allow microphone access. Wait for **Listening**, then speak
   and confirm you hear a response. The header's connection indicator alone does
   not establish a voice session.
6. Test notifications separately: leave an agent at a real, unanswered permission
   or choice prompt. Wait for the app to classify it as **Needs you**, then leave it
   unanswered for at least five seconds. Check both a banner and Notification Center;
   tap the notification and confirm it opens the correct pane. Repeat with the phone
   locked. Do not test using a menu that was already answered and remains in scrollback.

Apple documents the installed-app and user-gesture requirements in
[Web Push for Web Apps on iOS and iPadOS](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/).
Installing from Chrome also works: see [Chrome's iPhone web-app instructions](https://support.google.com/chrome/answer/9658361?co=GENIE.Platform%3DiOS&hl=en).
The requirement is the installed web app, not which browser installed it. A shortcut
that merely opens a normal browser tab is not equivalent.
Microphone and notification permissions are separate: allowing one does not allow
the other. If notifications were denied, enable them for the installed app in iOS
**Settings → Notifications**; repeatedly tapping the bell cannot override a denial.

## While using it: screen timeout, lock, and interruptions

| Situation | Expected behavior / what to do |
|---|---|
| Live Mode in the foreground | The app requests a screen wake lock to prevent automatic screen timeout. This is best-effort: browser policy or Low Power Mode can deny it. |
| Switch apps or lock the phone | The app does not deliberately stop Live Mode just because it becomes hidden. It keeps the microphone/socket and attempts audio recovery, but iOS can interrupt audio or suspend the page. Do not assume it is still listening. |
| Screen timed out; voice stopped | Unlock and reopen the Home Screen app. Open Live Mode and follow the **Audio interrupted** message; tap the microphone control to resume, checking its mute state. If recovery fails, stop and start Live Mode again. |
| Network or tunnel interruption | Live Mode attempts bounded reconnection after an established connection drops. If it ends with **Live Mode disconnected. Try again.**, start it again once connectivity returns. |
| Navigate away, reload, or terminate the app | Navigation releases the voice session; start Live Mode again when you return. Closing only the voice dialog does not stop it—use its Stop control to end the session. |
| Phone locked, voice stopped, or app closed | Push does not depend on the browser's voice socket. The server continues watching panes and sends through Apple's push service, provided the daemon/tunnel setup and device subscription remain healthy. |

For a reliable hands-free conversation, keep the installed app foregrounded and
check that it says **Listening** after an interruption. For unattended agents, use
push as the attention channel; do not depend on background voice staying alive.
iOS Focus, notification settings, network availability, and delivery scheduling can
affect when a push is presented. Turning Live Mode off does **not** turn notifications off.

## Exactly what triggers a notification today

- The normal pane watcher/classifier supplies the state. Push adds no second LLM
  judgment and works whether Live Mode is on or off.
- A valid, non-stale pane must be **waiting for the user**, not running, idle, or
  waiting on an external agent/CI. Structured questions use their prompt/options;
  questionless user-waits (such as a rewind picker) use the headline.
- The same answer contract must remain stable for **five seconds**. The notifier
  checks roughly once per second, so this is not a five-second delivery guarantee.
- Each contract is queued once while it remains active. Its identity includes the
  pane incarnation, wait state, question text, option list/order, and answer style.
  Changing those can create a new notification; clearing the wait allows a later
  occurrence of the same question to notify again.
- The cap is **three queued notifications per pane incarnation per rolling fifteen
  minutes**. Being at the keyboard, viewing that pane, or having another browser
  tab open does **not** suppress notifications.
- Every subscribed device receives the push. The title uses the pane title, then
  label; the body uses the question, then headline. Notifications share a per-pane
  tag so subsequent ones can replace the prior entry.
- A tap opens that pane in `/m`. On iPhone, answer using the app's buttons or composer;
  do not expect inline replies or notification action buttons. Supporting platforms
  can offer up to two options, validated against the still-current question before
  sending input. Stale actions fall back to opening the app.
- Delivery is best-effort, with a ten-minute push TTL. Relay failures are logged and
  dropped, not replayed after an outage. An accepted queue entry counts as notified
  even if its later delivery fails. **Completion/milestone pushes are not implemented.**

The classifier is fallible: an old menu above a newer idle prompt can be mistaken
for a current question. Five seconds of a cached wrong classification can still
notify—it is not five independent confirmations. This is a classification bug, not
an instruction to answer that old menu. Unchanged panes normally reuse their last
classification; content changes, explicit input-driven refreshes, startup, and
bounded failed-parse retries can cause another read. Volatile weekly percentages
are ignored by the content fingerprint. Notification deduplication/rate state is
in memory, so a daemon restart can notify again for an outstanding wait; subscriptions
and VAPID keys survive the restart.

## Operator prerequisites and troubleshooting

Before the phone steps, serve the app over authenticated HTTPS with WebSocket
support and configure Live Mode (`TMUXRC_LIVE_MODE=1` plus credentials for the
selected provider). See [Live Mode](design/live-mode.md) and [deployment](../deploy/).
The server must keep running even when the phone is asleep.

The sender contact defaults to `mailto:tmux-rc@openbus.io`; no configuration is
required. This identifies the push sender, not the tunnel URL. To use your own
contact, set it in the daemon's root `.env` and restart the service:

```dotenv
TMUXRC_PUSH_SUBJECT=mailto:you@your-real-domain.com
```

Replace that example with your actual address. The old default
`mailto:tmux-rc@localhost` caused Apple to reject delivery during iPhone testing with
**403 BadJwtToken**, and using a real mail contact fixed it. Use `mailto:` with the
current VAPID library; an HTTPS subject failed its validation in our setup.

Subscriptions and the stable VAPID keypair live in the owner-only file
`~/.local/state/tmux-rc/push.json` (under the XDG state directory), outside Git.
Do not delete it or rotate the key to troubleshoot an ordinary delivery failure:
existing subscriptions are bound to that key. Reinstalling the Home Screen app or
clearing its data can require enabling the bell again. Tap the bell to disable this
device; `POST /api/push/revoke-all` removes all saved device subscriptions.

If a card says **Needs you** but no push arrives, check in this order:

1. You launched the Home Screen installation, enabled its bell, and granted iOS
   notifications. Check Notification Center, Focus, and the app's alert settings.
2. The question is genuinely current, has settled, and has not already notified or
   reached the fifteen-minute cap. Reopening a card does not request another push.
3. The daemon has fresh valid classifications and a saved subscription. The browser
   re-registers an existing subscription on startup; a bell reconnect error means
   that request failed.
4. Inspect `journalctl --user -u tmux-rc` for `push delivery failed` or queue errors.
   For Apple `BadJwtToken`, check the configured contact above. HTTP 404/410 removes
   expired subscriptions; reopen the installed app and re-enable notifications.

Do not reintroduce active-browser/tmux suppression to solve false alerts. Diagnose
the classifier's current-screen judgment or delivery failure separately. The detailed
[push design](design/push-notifications.md) covers contracts, transport, and reply safety.
