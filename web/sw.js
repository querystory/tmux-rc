// Minimal service worker — required for install/add-to-home-screen. We deliberately
// do NOT cache API responses (state must be live); only the app shell is cached so
// the PWA opens instantly. Network-first for everything, cache as offline fallback.
const SHELL = ["/", "/app.js", "/manifest.json", "/icon.svg"];
const CACHE = "termiphone-v1";

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // never intercept live API
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
