/* Authenticated CardScanner PWA: cache static assets only, never CSRF-bearing HTML or account/API data. */
const CACHE = 'cardscanner-mobile-v3';
const STATIC = ['./manifest.webmanifest', './cardscanner.svg', './cardscanner-192.png', './cardscanner-512.png'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(STATIC)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || url.pathname.includes('/api/')) return;
  if (event.request.method !== 'GET') return;
  // Always fetch app HTML from the server: it contains a session-specific CSRF token.
  if (event.request.mode === 'navigate' || /\/scanner\/mobile\/?$/.test(url.pathname) || url.pathname.endsWith('/scanner/mobile/index.html')) return;
  event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request).then(response => {
    if (response.ok && response.type === 'basic') { const copy=response.clone(); caches.open(CACHE).then(cache => cache.put(event.request,copy)); }
    return response;
  })));
});
