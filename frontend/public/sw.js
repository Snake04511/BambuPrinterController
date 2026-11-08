// Minimal service worker (only registers on HTTPS, e.g. later through Tailscale):
// network first for the app shell, cached copy when offline. API calls are never cached.
const CACHE = 'shell-v1'
self.addEventListener('install', (e) => {
  self.skipWaiting()
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(['/'])))
})
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))),
  )
  self.clients.claim()
})
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url)
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        if (resp.ok && url.origin === self.location.origin) {
          const copy = resp.clone()
          caches.open(CACHE).then((c) => c.put(e.request, copy))
        }
        return resp
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match('/'))),
  )
})
