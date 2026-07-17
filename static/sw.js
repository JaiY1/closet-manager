// Minimal service worker — enough to make the app installable as a PWA.
// It caches the app shell lightly and otherwise passes requests to the network.
// We deliberately do NOT cache API/generation responses (they're user- and
// cost-sensitive) — only static shell assets, network-first.
const CACHE = 'pop-shell-v1';
const SHELL = ['/static/manifest.json', '/static/icons/icon-192.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Only touch same-origin GETs for static assets; everything else goes straight
  // to the network (never cache uploads, try-on renders, or API calls).
  if (e.request.method !== 'GET' || url.origin !== location.origin || !url.pathname.startsWith('/static/')) {
    return;
  }
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
