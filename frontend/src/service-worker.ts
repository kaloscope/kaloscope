/// <reference types="vite/client" />
/// <reference no-default-lib="true"/>
/// <reference lib="esnext" />
/// <reference lib="webworker" />
import { cleanupOutdatedCaches, precacheAndRoute } from 'workbox-precaching';
import {
  KALOSCOPE_NOTIFICATION,
  type ServiceWorkerNotificationMessage,
  type ServiceWorkerNotificationPayload
} from './lib/notifications';

declare let self: ServiceWorkerGlobalScope;

/**
 * Validate a notification payload before using it in the service worker.
 *
 * @param value - The value to validate.
 * @returns Whether the value is a service worker notification payload.
 */
function isNotificationPayload(value: unknown): value is ServiceWorkerNotificationPayload {
  if (!value || typeof value !== 'object') {
    return false;
  }
  const payload = value as Partial<ServiceWorkerNotificationPayload>;
  return (
    typeof payload.id === 'number' &&
    typeof payload.title === 'string' &&
    typeof payload.body === 'string' &&
    typeof payload.tag === 'string'
  );
}

/**
 * Check whether a message from the app shell is a notification display request.
 *
 * @param data - The message data received by the service worker.
 * @returns Whether the data is a service worker notification message.
 */
function isNotificationMessage(data: unknown): data is ServiceWorkerNotificationMessage {
  if (!data || typeof data !== 'object') {
    return false;
  }
  const message = data as Partial<ServiceWorkerNotificationMessage>;
  return (
    message.type === KALOSCOPE_NOTIFICATION &&
    Array.isArray(message.notifications) &&
    message.notifications.every(isNotificationPayload)
  );
}

/**
 * Display app notifications through the service worker registration.
 *
 * @param message - The notification message posted from the app shell.
 */
async function showNotifications(message: ServiceWorkerNotificationMessage) {
  await Promise.all(
    message.notifications.map((notification) =>
      self.registration.showNotification(notification.title, {
        body: notification.body,
        tag: notification.tag,
        data: { id: notification.id }
      })
    )
  );
}

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    // activate the waiting service worker after the user accepts the update prompt
    self.skipWaiting();
  } else if (isNotificationMessage(event.data)) {
    // display notifications
    event.waitUntil(showNotifications(event.data));
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
