import type { Notification as AppNotification } from '$lib/types';

/**
 * Shared prefix for service worker notification messages and browser notification tags.
 */
export const KALOSCOPE_NOTIFICATION = 'kaloscope_notification';

/**
 * The minimal notification data sent from the app shell to the service worker.
 */
export type ServiceWorkerNotificationPayload = {
  id: number;
  title: string;
  body: string;
  tag: string;
};

/**
 * Message posted to the service worker when new app notifications should be displayed.
 */
export type ServiceWorkerNotificationMessage = {
  type: typeof KALOSCOPE_NOTIFICATION;
  notifications: ServiceWorkerNotificationPayload[];
};

/**
 * Client-side state used to remember which unread notifications were already considered.
 */
export type NotificationDispatchState = {
  initialized: boolean;
  knownIds: Set<number>;
};

/**
 * Add generic yes/no presence flags for template values.
 *
 * For example, `{ error: 'x' }` becomes `{ error: 'x', has_error: 'yes' }`,
 * which lets ICU select messages hide optional fragments without per-template code.
 *
 * @param values - The values passed to the i18n formatter.
 * @param optionalKeys - The keys that are optional and should be checked for presence.
 * @returns The values with derived `has_*` flags.
 */
export function withConditionalFlags(
  values: Record<string, unknown>,
  optionalKeys: string[] = []
): Record<string, unknown> {
  const result = { ...values };

  for (const key of optionalKeys) {
    result[key] ??= '';
  }

  for (const [key, value] of Object.entries(result)) {
    const flag = `has_${key}`;
    if (!(flag in result)) {
      let hasValue = value !== null && value !== undefined;
      if (hasValue && (typeof value === 'string' || Array.isArray(value))) {
        hasValue = value.length > 0;
      }
      result[flag] = hasValue ? 'yes' : 'no';
    }
  }

  return result;
}

/**
 * Create the client-side state used to avoid sending the same unread notification repeatedly.
 *
 * @returns The initial dispatch state.
 */
export function createNotificationDispatchState(): NotificationDispatchState {
  return {
    initialized: false,
    knownIds: new Set()
  };
}

/**
 * Request browser notification permission and normalize unsupported browsers.
 *
 * @param notificationApi - The browser notification API to request permission from.
 * @returns The resolved notification permission, or unsupported when the API is unavailable.
 */
export async function requestWebNotificationPermission(notificationApi?: {
  readonly permission: NotificationPermission;
  requestPermission: () => Promise<NotificationPermission>;
}): Promise<NotificationPermission | 'unsupported'> {
  if (!notificationApi) {
    return 'unsupported';
  }
  if (notificationApi.permission !== 'default') {
    return notificationApi.permission;
  }
  try {
    return await notificationApi.requestPermission();
  } catch {
    return notificationApi.permission;
  }
}

/**
 * Convert unread API records into service worker payloads that have not been sent yet.
 *
 * @param notifications - The API notification records to inspect.
 * @param state - The dispatch state used to skip notifications that were already considered.
 * @param format - The formatter that converts an API notification into display text.
 * @returns The service worker notification payloads for newly received unread notifications.
 */
export function collectServiceWorkerNotifications(
  notifications: AppNotification[],
  state: NotificationDispatchState,
  format: (notification: AppNotification) => { title: string; body: string }
): ServiceWorkerNotificationPayload[] {
  const unread = notifications.filter((notification) => !notification.seen);
  const pending = state.initialized ? unread.filter((notification) => !state.knownIds.has(notification.id)) : [];

  for (const notification of unread) {
    state.knownIds.add(notification.id);
  }
  state.initialized = true;

  return pending.map((notification) => {
    const { title, body } = format(notification);
    return {
      id: notification.id,
      title,
      body,
      tag: `${KALOSCOPE_NOTIFICATION}_${notification.id}`
    };
  });
}
