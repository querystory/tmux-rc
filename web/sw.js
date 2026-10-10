// Push-only service worker. Deliberately NO fetch handler and NO cache writes: every app
// asset continues to come from the network, preserving tmux-rc's no-stale-UI contract.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    await Promise.all((await caches.keys()).map((key) => caches.delete(key)));
    await clients.claim();
  })());
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data?.json() || {}; } catch {}
  const actions = Array.isArray(data.actions) ? data.actions.slice(0, 2) : [];
  event.waitUntil(self.registration.showNotification(data.title || "tmux-rc", {
    body: data.body || "A session needs your attention",
    tag: data.tag || "tmux-rc",
    renotify: true,
    icon: "/apple-touch-icon.png",
    badge: "/apple-touch-icon.png",
    actions,
    data: { url: data.url || "/m", nonce: data.nonce || null, chat: !!data.chat },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const match = /^answer:(\d+)$/.exec(event.action || "");
  event.waitUntil((async () => {
    if (match && data.nonce) {
      try {
        const response = await fetch("/api/push/answer", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ nonce: data.nonce, option_index: Number(match[1]) }),
        });
        if (response.ok) return;
      } catch {}
    }
    const target = new URL(data.url || "/m", self.location.origin).href;
    const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
    const app = windows.find((client) => {
      try { return new URL(client.url).pathname.startsWith("/m"); } catch { return false; }
    }), existing = app || windows[0];
    if (existing) {
      try {
        // A chat lives in the open app: navigating would reload it away, so ask it instead.
        if (data.chat && app) app.postMessage("chat"); else await existing.navigate(target);
        return existing.focus();
      } catch {}
    }
    return clients.openWindow(target);
  })());
});
