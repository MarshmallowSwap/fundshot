// FundShot Service Worker — PWA offline support
const CACHE = 'fundshot-v1';
const STATIC = [
  '/',
  '/manifest.json',
  '/bybit-logo.svg',
  '/binance-logo.svg',
  '/okx-logo.svg',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  // Network first per API calls, cache first per static assets
  if (e.request.url.includes('/api/')) {
    e.respondWith(fetch(e.request).catch(() =>
      new Response(JSON.stringify({ok:false,error:'offline'}), {
        headers: {'Content-Type': 'application/json'}
      })
    ));
  } else {
    e.respondWith(
      caches.match(e.request).then(cached => cached || fetch(e.request))
    );
  }
});

// Push notifications (per futuri alert push)
self.addEventListener('push', e => {
  if (!e.data) return;
  const data = e.data.json();
  e.waitUntil(self.registration.showNotification(data.title || 'FundShot Alert', {
    body:  data.body  || '',
    icon:  '/icon-192.png',
    badge: '/icon-192.png',
    tag:   data.tag   || 'fundshot-alert',
    data:  data,
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.openWindow('/'));
});
