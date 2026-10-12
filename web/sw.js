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
    data: { url: data.url || "/m", nonce: data.nonce || null, chat: data.chat || null },
  }));
});

// Asks one app window whether its chat holds this waiting card (by id, so with two tabs
// holding cards only the push's own answers): the window, or null.
const holdsCard = (card) => (client) => new Promise((resolve) => {
  const { port1, port2 } = new MessageChannel();
  port1.onmessage = ({ data }) => resolve(data === true ? client : null);
  setTimeout(() => resolve(null), 1000);
  client.postMessage({ chat: card }, [port2]);
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
    const apps = windows.filter((client) => {
      try { return new URL(client.url).pathname.startsWith("/m"); } catch { return false; }
    });
    // A chat lives in the open app: navigating would reload it away, so the app window whose
    // chat holds the card is asked to bring it back. A lone one (a home-screen app's) is
    // focused before it is asked: iOS may drop a focus that waits on a reply, and a suspended
    // page cannot reply until it is woken. Several are each asked first, to focus the owner.
    const owner = data.chat && apps.length > 1 && (await Promise.all(apps.map(holdsCard(data.chat)))).find(Boolean);
    const existing = owner || apps[0] || windows[0];
    if (existing) {
      try {
        // iOS will not navigate() an open app to a new hash, so the app routes itself (app.js).
        if (!apps.length) await existing.navigate(target);
        else if (!data.chat) existing.postMessage({ open: target });
        const focused = existing.focus();
        if (data.chat && apps.length && !owner) holdsCard(data.chat)(existing);
        return focused;
      } catch {}
    }
    return clients.openWindow(target);
  })());
});
