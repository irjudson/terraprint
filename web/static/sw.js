// Minimal service worker — enables "Add to Home Screen" on mobile
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());
// No caching strategy — always fetch live (local network tool)
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));
