/// <reference types="vite/client" />
/// <reference no-default-lib="true"/>
/// <reference lib="esnext" />
/// <reference lib="webworker" />
import { cleanupOutdatedCaches, precacheAndRoute } from 'workbox-precaching';

declare let self: ServiceWorkerGlobalScope;

// activate the waiting service worker after the user accepts the update prompt
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

// keep mutable app shell files on the network so deployment cache headers can take effect
const mutableFrontendPaths = new Set(['/', '/index.html', '/404.html', '/manifest.webmanifest', '/_app/version.json']);
// filter the injected Workbox manifest before precaching
const precacheManifest = self.__WB_MANIFEST.filter((entry) => {
  const url = typeof entry === 'string' ? entry : entry.url;
  const { pathname } = new URL(url, self.location.origin);
  return !mutableFrontendPaths.has(pathname) && !pathname.endsWith('.html');
});

// precache immutable build assets only
precacheAndRoute(precacheManifest);

// clean up incompatible Workbox caches
cleanupOutdatedCaches();
