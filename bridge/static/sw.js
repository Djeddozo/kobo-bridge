// Caches only the app shell and an offline page; API responses are never cached.
const CACHE = "kobo-bridge-v1";
const SHELL = ["/", "/static/style.css", "/static/app.js", "/static/icon.svg", "/static/icon-192.png", "/offline.html"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  // Shares (POST /share) and API calls always go straight to the Pi.
  if (req.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/offline.html")));
    return;
  }
  event.respondWith(fetch(req).catch(() => caches.match(req)));
});
