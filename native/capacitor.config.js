// The one build-time identity setting: the real bundle id is supplied by the release lane
// (docs/design/native-app.md), and every other build keeps this placeholder.
module.exports = {
  appId: process.env.TMUXRC_APP_ID || "com.example.tmuxrc",
  appName: "tmux-rc",
  webDir: "www",
  server: {
    // The daemon's address is typed in by the user, so no host can be listed here: the
    // webview may navigate anywhere, and on iOS the bridge follows it.
    allowNavigation: ["*"],
    errorPath: "index.html",
  },
  // overlay/ios's AppDelegate is the notification delegate, from before launch finishes.
  ios: { handleApplicationNotifications: false },
};
