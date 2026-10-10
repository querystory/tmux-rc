---
title: Install the client
---

The tmux-rc client is an installable web app served by your own daemon. Open your
remote HTTPS address on desktop or phone; the dashboard loads in your browser.
Installation is optional for browsing and direct control. iPhone push notifications
require the installed Home Screen app.

Set up the [daemon](getting-started.md) and [remote access](deploy/_index.md) first.
Install the app from your daemon's address, rather than from this documentation website.

## iOS

1. Open your authenticated HTTPS tmux-rc address in Safari or Chrome.
2. Choose **Share → Add to Home Screen → Add**. Keep **Open as Web App** enabled if offered.
3. Launch tmux-rc from its Home Screen icon and sign in if prompted.
4. Tap the bell and allow notifications.

Follow [iPhone setup](iphone-setup.md) for voice, notification testing, and screen-lock
behavior. Use iOS 16.4 or newer for Web Push.

## Android

1. Open your authenticated HTTPS tmux-rc address in Chrome.
2. Open the browser menu and choose **Install and create shortcut → Install**.
   Depending on your browser version, the entry may be called **Install app** or
   **Add to Home screen**.
3. Launch tmux-rc from the installed icon.
4. Tap the bell and allow notifications. For voice, allow microphone access separately.

The client updates from your daemon; you do not need to download an APK.
See [Chrome's web app installation instructions](https://support.google.com/chrome/answer/9658361?co=GENIE.Platform%3DAndroid&hl=en).

## Desktop

Open your daemon's address in your browser. Chrome and Edge offer web app installation
from the address bar or browser menu when supported. Installation gives the dashboard
its own window; using an ordinary browser tab works too.

## Chat and voice

Chat and voice require [Live Mode configuration](design/live-mode.md) on the daemon.
They are available alongside direct pane controls. The basic no-model quick start does
not enable a conversational model by itself.
