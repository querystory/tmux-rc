// Self-destructing service worker. Any previously-installed SW, when the browser
// re-checks this file, replaces itself with this and immediately unregisters + clears
// all caches, then reloads controlled pages. This is how we evict a stale SW that was
// serving old app.js/index.html (which hid new UI like the attach/Ctrl-O buttons).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", async () => {
  await caches.keys().then((ks) => Promise.all(ks.map((k) => caches.delete(k))));
  await self.registration.unregister();
  const clients = await self.clients.matchAll();
  clients.forEach((c) => c.navigate(c.url));
});
