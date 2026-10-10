// Minimal service worker: caches the app shell so the page opens instantly; API calls always hit the network.
const CACHE = "att-shell-v1";
const SHELL = ["/m/", "/m/manifest.webmanifest", "/m/icon-192.png"];
self.addEventListener("install", e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))));
self.addEventListener("activate", e => e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))));
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || !u.pathname.startsWith("/m/")) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
