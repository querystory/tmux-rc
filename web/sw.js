// Dev-friendly service worker: registered only so the PWA is installable. It does
// NOT cache anything — every request goes to the network. Caching the app shell was
// serving a stale app.js/index.html and hiding edits. If offline support is wanted
// later, add a cache with a version bust tied to /api/version.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => {
  // Drop any caches left by earlier versions of this SW.
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
// No fetch handler ⇒ browser default network fetch. Nothing is intercepted or cached.
