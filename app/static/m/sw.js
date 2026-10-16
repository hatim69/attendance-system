// Service worker: caches the app shell so the page opens instantly (API calls always hit the network),
// and shows check-in / check-out reminders sent by the server.
const CACHE = "att-shell-v5";
const SHELL = ["/m/", "/m/manifest.webmanifest", "/m/icon-192.png", "/m/icon-512.png"];
self.addEventListener("install", e => { self.skipWaiting(); e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))); });
self.addEventListener("activate", e => e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || !u.pathname.startsWith("/m/")) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Attendance", {
    body: d.body || "", icon: "/m/icon-192.png", badge: "/m/icon-192.png", tag: "attendance-reminder",
    renotify: true, data: { url: d.url || "/m/" }
  }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/m/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(ws => {
    for (const w of ws) { if (new URL(w.url).pathname.startsWith("/m/") && "focus" in w) return w.focus(); }
    return self.clients.openWindow(url);
  }));
});
