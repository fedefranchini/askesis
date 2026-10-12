// Offline page only. Nothing personal is ever cached: pages and data always come from the server; when the server
// cannot be reached, page navigations get a static explanation instead of the browser's error.
const CACHE = "askesis-offline-v1";
const OFFLINE = ["/static/offline.html", "/static/app.css", "/static/vendor/barlow/Barlow-Regular.ttf",
                 "/static/vendor/barlow/Barlow-SemiBold.ttf", "/static/vendor/barlow/Barlow-Bold.ttf",
                 "/static/icons/askesis.svg"];

self.addEventListener("install", (ev) => {
  ev.waitUntil(caches.open(CACHE).then((c) => c.addAll(OFFLINE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (ev) => {
  ev.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (ev) => {
  const req = ev.request;
  if (req.method !== "GET") return;  // forms always go to the server, never queued
  if (req.mode === "navigate") {
    ev.respondWith(fetch(req).catch(() => caches.match("/static/offline.html")));
    return;
  }
  const path = new URL(req.url).pathname;
  if (OFFLINE.includes(path)) {  // only what the offline page needs, and only when the network fails
    ev.respondWith(fetch(req).catch(() => caches.match(path)));
  }
});
