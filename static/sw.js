// Brick Scanner service worker — app-shell caching for the installed PWA.
// Strategy: navigations are network-first (always get fresh HTML online, fall
// back to the cached shell offline); same-origin /static/ assets are
// stale-while-revalidate; API calls and cross-origin requests (Brickognize,
// Rebrickable/BrickLink images, Google Fonts) are never touched — they must be
// live. Bump CACHE to invalidate old caches on a breaking change.
const CACHE = 'brick-scanner-v1';
const SHELL = [
  '/',
  '/static/favicon.svg',
  '/static/brick.svg',
  '/static/minifig.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/apple-touch-icon.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;            // fonts, images, APIs on other hosts
  if (url.pathname.startsWith('/api/')) return;               // live data — never cache
  if (url.pathname === '/sw.js' || url.pathname === '/manifest.webmanifest') return;

  // App shell (page loads): network-first, fall back to cached '/'
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put('/', copy)).catch(() => {});
          return resp;
        })
        .catch(() => caches.match('/').then((r) => r || caches.match(req)))
    );
    return;
  }

  // Same-origin static assets: stale-while-revalidate
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.match(req).then((cached) => {
        const net = fetch(req)
          .then((resp) => {
            if (resp && resp.status === 200) {
              const copy = resp.clone();
              caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
            }
            return resp;
          })
          .catch(() => cached);
        return cached || net;
      })
    );
  }
});
