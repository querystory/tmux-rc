# Design: the native phone app, a thin shell around the web app

Status: **shell, push bridge and build lanes written; compiled in CI, not yet run on a
device.** The daemon's side (the `apns` subscription kind and sending to Apple) is described
in [push notifications](push-notifications.md), "Native transports". Nothing here changes
what a browser or the installed PWA does.

## The problem

The phone client is a PWA, and on iPhone a PWA is a second-class app in exactly the places
this product leans on:

- **Answering from a notification.** A blocked agent's question arrives with its options,
  and iOS draws no action buttons for a web notification, so every answer is a tap, an app
  launch and another tap ([push notifications](push-notifications.md), "Replying").
- **Taps that land.** A tap on a web notification often never reaches the service worker,
  and the workaround (Declarative Web Push) needs iOS 18.4 and cannot carry a chat.
- **Getting it installed.** "Open in Safari, Share, Add to Home Screen, then turn on
  notifications inside the installed copy" is a setup guide ([iPhone setup](../iphone-setup.md)),
  where everything else on the phone is one tap on a link.

A native app fixes those three. It should fix nothing else, because everything else already
works and already ships from the daemon.

## The decision: a Capacitor shell that loads the user's own daemon

The app is a webview pointed at the address the user types in once. The daemon keeps
serving `web/m`, so the app is the PWA, and a web change reaches every phone without an
app release. Native code exists only where the web has no API: the APNs device token,
notification categories and their action buttons, and the answer sent while no page is
running.

### Why not the alternatives

- **A hand-written WKWebView wrapper.** It is the same app with the bridge written by us:
  a script message handler, a plugin registry, the project template, and then all of it
  again for Android. Capacitor is that code, maintained, and its push plugin already speaks
  both platforms. What we would save is one dependency; what we would own is its whole job.
- **React Native, Flutter or SwiftUI.** A rewrite of a UI that works, to be kept in step
  with the web one for ever. Every feature would be built twice and released on Apple's
  schedule. The web app is not the problem; three gaps in iOS's PWA support are.
- **Bundling the web assets in the app.** This is what Capacitor expects, and it would
  make the app a second release train: a daemon upgraded ahead of the app would talk to an
  older UI, and the no-stale-UI rule the service worker is built around would be broken by
  design. It would also need the daemon's address anyway, for the API, and cross-origin
  requests through every user's login front end. Loading the page from the daemon keeps one
  origin, one version and one cookie.

### What the remote page costs

The daemon's address differs per user, so it cannot be listed at build time: the shell lets
the webview navigate to any host, and on iOS Capacitor's bridge is injected into whichever
page is in the main frame. Left alone that is a hole, not a tradeoff. The bridge carries
more than the push plugin: Capacitor registers its own HTTP and cookie plugins, and a
native HTTP request ignores the browser's cross-origin rules and the daemon's origin check
while carrying the login cookie. Any page the webview was taken to (a link followed in
place, a redirect) could type into a terminal.

So the shell answers bridge calls from two origins only: its bundled page, and the daemon,
which is defined as the address the bundled page sent the webview to. No page but the
bundled one can change that. The login provider's pages, and anything else, load and run
with a bridge that says nothing back. Links that open a new window leave for Safari. The
same stored address is where notification answers go, and changing it clears the
notifications still on screen and takes the device off the old daemon, since its questions
cannot be answered at the new one.

Capacitor documents open navigation as a development setting. It is the mechanism this
design rests on, so it is used knowingly, and it is the first thing App Review would ask
about (below).

### Android

Android builds from the same project and gets the shell: the address page, the PWA in a
webview, the way back to the address page. It does not get native push here. Capacitor's
Android runtime injects its bridge only into the bundled origin, so the daemon's page
cannot see the push plugin, and the daemon has no FCM transport to send through. Android is
also where the PWA already works: Chrome draws action buttons and
delivers taps. So the Android app is a convenience until someone needs more, and the
"nearly free" part is the shell, not the notifications.

## What native does

**The address.** A bundled page asks for the daemon's https address, keeps it on the
device, and opens it. It connects by itself once per launch; arriving there again means a
load failed or the user chose Settings, "Change server", and both want the form rather
than another silent attempt. Only https is accepted: iOS refuses plain http in a webview,
and the address carries a login.

**The token.** The web app's notification switch is the same switch; inside the shell it
asks the push plugin instead of the service worker, and posts the device token to the
daemon as an `apns` subscription. It turns on only against a daemon whose push config lists
`apns`, which it does when it holds an APNs key; otherwise the switch says so instead of
failing later. The token is re-posted on every launch, as the web subscription is, so a
daemon that lost its state heals.

The token is labelled with Apple's environment, because a push sent to the wrong one is
refused as a bad token and the daemon then drops the device. The label comes from how the
build was signed rather than how it was compiled: a build installed from Xcode carries its
provisioning profile, which names the environment, and a TestFlight or App Store build
carries none and is always production.

**Taps.** A tapped notification carries the path to open. The page receives it from the
plugin and routes with the URL state it already has, so a tap opens the pane without a
reload, and a cold start gets the tap as soon as the page is listening.

**Answers, with the app not running.** This is the part the plugin cannot do, and the
reason there is native code of our own. A button on a notification is delivered to the app
in the background, and if the app was not running iOS starts it with no window: no
webview, so no page, so nothing for the plugin's event to reach. The app's delegate
therefore takes the response itself, before launch finishes as Apple requires, and posts
the notification's one-shot nonce and the option index to the daemon's existing answer
endpoint, with the login cookie read from the webview's store. The daemon's gates are
unchanged: the nonce binds the offered options, the pane and its question.

If that request does not succeed (an expired login redirects to a sign-in page, the tunnel
is down), a local notification says the answer was not sent and opens the pane when tapped.
Silence would read as "answered".

Two choices in that path are deliberate:

- **Answer buttons wait for the unlock.** The web push on Android types from the lock
  screen; here the action requires authentication, because a phone on a desk should not be
  able to approve a command.
- **Buttons carry the question's own options, through a notification service extension.**
  iOS draws only the actions of a category the app registered beforehand, with fixed
  titles, so a push cannot name its own buttons. The alternative was a fixed pair reading
  "Option 1" and "Option 2" with the real text in the body, which needs no second target
  and makes every answer a lookup; an approval tapped by number is the wrong place to save
  code. So the daemon marks an ask mutable and sends its titles, and the extension, which
  iOS runs on each such push before showing it, registers a category for exactly those
  titles and points the notification at it. Categories are shared by the whole app, so one
  is named by its titles and kept only while a notification on screen uses it. If the
  extension fails or runs out of its time, the push shows as sent: no buttons, and a tap
  opens the pane.

### What it does not fix

- **Live Mode audio.** Background audio, the audio session and the microphone prompt are
  the other half of the iPhone pain. None of it is in this version; the shell does not even
  declare microphone use, so voice stays in Safari or the PWA for now.
- **The login** in front of the daemon (next section).
- **Sending pushes.** A device token is useless until something holds an APNs key (below).

## Signing in through the tunnel

The daemon authenticates nothing; the tunnel in front of it does
([Deploying](../../deploy/)), and that login now happens inside a webview.

- **A private mesh** (Tailscale) has no web login. Nothing to do.
- **A cookie login with its own form** (Cloudflare Access with a one-time code, most
  identity providers) is an ordinary page and works in a webview.
- **Google sign-in** is the open case, and it is the common one: Access with Google, or an
  identity-aware proxy. Google's policy is to refuse sign-in from embedded webviews. What
  can be observed without a phone is only that the sign-in page is still served to a
  webview's user agent; the refusal, where it happens, comes later in the flow and is
  decided on more than the user agent. Whether today's WKWebView gets through has to be
  tried on a device, and a pass would not be a promise.

If it is refused, pretending to be Safari (overriding the user agent) is the cheap fix and
the wrong one: it breaks Google's terms and works until it does not. The fix that holds is
to sign in where Google allows it, in the system's authentication session, and bring the
session back:

1. The shell opens a daemon address in an `ASWebAuthenticationSession` (Custom Tabs on
   Android). The front end runs its login there, in Safari's engine.
2. The request that finally reaches the daemon carries the front end's session cookie. A
   small daemon endpoint answers it with a redirect to the app's own URL scheme, carrying
   the cookies it was sent.
3. The shell puts those cookies in the webview's store and loads the daemon.

The cost is that the daemon gains an endpoint that reflects an HttpOnly cookie into a URL.
It must redirect only to the app's scheme, and it relies on the authentication session
handing that URL to the app that started it and to no other. It also assumes the front end
forwards its cookie to the daemon, which Cloudflare Access does and others may not. This is
specified, not built: it needs a daemon change and a device that shows the refusal.

Answers sent from a notification use the same cookie, so a login that expires daily makes
the buttons fail daily until the app is opened. That is the front end's session length, and
the "Answer not sent" notice is how it shows.

## Open: who sends the push

Apple accepts a push for an app only from a holder of that app's APNs key. One key signs
for every install of the bundle id, so it cannot be handed to each self-hosted daemon: any
copy could push to any user of the app. Web Push has no such problem (each daemon makes its
own VAPID key), which is why the PWA needed no service of ours.

- **Each operator builds their own app** with their own bundle id and key. No shared
  service and nothing to trust, and it is how the app is used until this is decided, but it
  needs a paid developer account per operator.
- **A push relay.** The app's publisher runs a small service that holds the key; a daemon
  posts the device token and payload to it. This is what ntfy and Home Assistant do. It
  puts a third party in the path of pane text, which the push design rejected once for
  cleartext relays, so payloads would have to be encrypted to the device and opened by the
  notification service extension. The relay
  also needs abuse limits, since a device token is all it takes to address a phone.
- **Keep Web Push for other people's daemons** and offer native push only where the
  operator holds the key.

The relay is the only one that gives an App Store install working notifications, and it is
a service to run. The decision waits on whether the app is for one organisation or for
everyone.

## Getting it onto phones

- **TestFlight, internal testers.** Up to 100 members of the developer team, no review,
  builds available minutes after upload. This is what the release lane feeds and is enough
  for a team.
- **TestFlight, public link.** Up to 10,000 people by link. The first build of a version
  goes through Beta App Review, which is lighter than App Review but does look at the app.
  Builds expire after 90 days, so it is a channel that needs feeding.
- **App Store.** Full review. A shell whose content is a website is what guideline 4.2
  (minimum functionality) exists to reject, and one that opens an address the user types
  gives the reviewer nothing to see without a daemon of their own. The honest case is the
  native part: actionable notifications for a self-hosted service, in the manner of other
  self-hosted clients, with a demo server for review. Likely to pass on a second try, not
  certain, and it brings the push-relay question with it.
- **Unlisted distribution.** A reviewed App Store app reachable only by link. It suits an
  app for one organisation's users but is granted on request and still passes full review.

TestFlight is the plan while the app is used by its own team; the App Store question is
taken up together with the relay, since neither matters without the other.

## Why the native projects are generated

Capacitor's advice is to commit `ios/` and `android/`. Here they are generated at build
time from the templates the lockfile pins, and the repository holds only what is ours: the
config, the address page, the iOS app delegate, notification extension and entitlement,
and the build recipe. The
generated projects are dozens of files nobody reads, a Gradle wrapper binary, and a diff
on every Capacitor upgrade; committing it would also invite edits in files a
regeneration would overwrite. The price is that a customization must be a whole file or a
scripted change to the project. The notification extension is the costly case: a second
target is added to the generated project by a script, using the project editor Capacitor's
own tooling ships. If that script starts to grow, committing the iOS project becomes the
better trade.

The bundle id is one setting, read at generation time, with a placeholder default. The
release lane supplies the real one, with the team id and the App Store Connect key, from
repository secrets; signing is Xcode's automatic signing driven by that key, so no
certificate or profile is stored anywhere. Capacitor accepts no dashes in the id, and the
extension takes the same id with `.notify` appended, so the App Store Connect key must be
allowed to register both.
