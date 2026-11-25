#!/bin/sh
# CI's look at the built app (the Native shell workflow; macOS with Xcode): boots a
# simulator, runs the app against the demo fleet (AGENTS.md, "UI screenshots") served over
# TLS on this machine, and leaves screenshots and logs in build/shots. Run from native/.
set -eux
app=build/ios/Build/Products/Debug-iphonesimulator/App.app id=com.example.tmuxrc out=$PWD/build/shots tls=$(mktemp -d)
mkdir -p "$out"
shot() { sleep "$1"; xcrun simctl io booted screenshot "$out/$2.png"; }

xcrun simctl boot "$(xcrun simctl list devices available | grep -m1 iPhone | grep -oE '[0-9A-F-]{36}')"
xcrun simctl bootstatus booted
# The webview takes only https, so the demo gets a certificate the simulator is told to trust.
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj /CN=localhost -keyout "$tls/key.pem" -out "$tls/cert.pem" \
  -addext subjectAltName=DNS:localhost -addext extendedKeyUsage=serverAuth -addext basicConstraints=critical,CA:TRUE
xcrun simctl keychain booted add-root-cert "$tls/cert.pem"
(cd .. && uv sync --locked && uv run python -c 'import sys, uvicorn; from scripts import demo_server as demo; demo.install()
uvicorn.run(demo.server.app, host="127.0.0.1", port=8443, ssl_keyfile=sys.argv[1], ssl_certfile=sys.argv[2])' \
  "$tls/key.pem" "$tls/cert.pem" > "$out/demo.log" 2>&1) &
curl --retry 60 --retry-connrefused --retry-delay 2 -fsS --cacert "$tls/cert.pem" -o /dev/null https://localhost:8443/m

xcrun simctl install booted "$app"
xcrun simctl launch booted "$id"
shot 8 1-first-run

# Nothing can type in a simulator from here, so the address is put where the page keeps it.
xcrun simctl terminate booted "$id"
perl -pi -e 's|<script>|<script>localStorage.setItem("daemon", "https://localhost:8443");|' "$(xcrun simctl get_app_container booted "$id" app)/public/index.html"
xcrun simctl launch --stdout="$out/app.log" --stderr="$out/app.log" booted "$id"
shot 20 2-daemon

# A push shaped like the daemon's ask. Apple's simulated pushes skip notification service
# extensions (Xcode 11.4 release notes), so this shows the banner and not the buttons.
brew install wix/brew/applesimutils
applesimutils --booted --bundle "$id" --setPermissions notifications=YES
xcrun simctl terminate booted "$id"
xcrun simctl push booted "$id" - <<'JSON'
{"aps": {"alert": {"title": "deploy-notes", "body": "Run the migration now?"}, "sound": "default", "category": "answer", "mutable-content": 1},
 "url": "/m#pane=%251&from=push", "tag": "block:%1", "nonce": "0123456789abcdef0123",
 "actions": [{"action": "answer:0", "title": "Yes"}, {"action": "answer:1", "title": "No"}]}
JSON
shot 2 3-push
xcrun simctl spawn booted log show --last 2m --predicate 'process == "Notify"' > "$out/extension.log"
