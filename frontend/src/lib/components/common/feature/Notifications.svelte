<script lang="ts" module>
  import type { Notification, Resp } from '$lib/types';

  export type NotificationsProps = {
    /** The class names for the wrapper element. */
    class?: string;
    /** The class names for the trigger button. */
    triggerClass?: string;
    /** The callback function when refreshing the notifications. */
    onrefresh?: (unread: number) => void;
  };

  // the notification templates that need i18n processing and their optional values
  const TEMPLATES: Record<string, string[]> = {
    DOWNLOAD_FAILED: ['error'],
    DOWNLOAD_COMPLETED: []
  };

  let notifications: Notification[] = $state([]);
  let total = $derived(notifications.length);
  let unread = $derived(notifications.filter((n) => !n.seen).length);
</script>

<script lang="ts">
  import { api } from '$lib/api';
  import { Modal } from '$lib/components';
  import { _, dateTime, number } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import {
    collectServiceWorkerNotifications,
    createNotificationDispatchState,
    KALOSCOPE_NOTIFICATION,
    requestWebNotificationPermission,
    withConditionalFlags
  } from '$lib/notifications';
  import { token } from '$lib/stores';
  import { onMount } from 'svelte';

  let { class: _class, triggerClass, onrefresh }: NotificationsProps = $props();

  // the modal dialog
  let modal: Modal;

  // the dispatch state
  const notificationDispatchState = createNotificationDispatchState();

  // show the notifications center
  export const showModal = () => {
    if ('Notification' in window) {
      requestWebNotificationPermission(window.Notification).then((permission) => {
        if (permission === 'granted' && !notificationDispatchState.initialized) {
          getAll(true);
        }
      });
    }
    modal.show();
  };

  /**
   * Get all notifications.
   *
   * @param sw - Whether to send new notifications to the service worker.
   */
  function getAll(sw = false) {
    api
      .get('notification/list')
      .json<Resp<Notification[]>>()
      .then(({ data }) => {
        notifications = data;
        onrefresh?.(unread);
        if (sw) {
          sendServiceWorkerNotifications(notifications);
        }
      });
  }

  /**
   * Send newly received API notifications to the service worker for system display.
   *
   * @param notifications - The API notifications to check and display.
   */
  async function sendServiceWorkerNotifications(notifications: Notification[]) {
    if (
      !('serviceWorker' in navigator) ||
      !('Notification' in window) ||
      window.Notification.permission !== 'granted'
    ) {
      return;
    }

    const serviceWorkerNotifications = collectServiceWorkerNotifications(
      notifications,
      notificationDispatchState,
      (notification) => ({
        title: title(notification),
        body: content(notification)
      })
    );
    if (serviceWorkerNotifications.length === 0) {
      return;
    }

    try {
      const registration = await navigator.serviceWorker.ready;
      registration.active?.postMessage({
        type: KALOSCOPE_NOTIFICATION,
        notifications: serviceWorkerNotifications
      });
    } catch {
      return;
    }
  }

  /**
   * Clear all notifications.
   */
  function clear() {
    api.post('notification/clear').then(() => getAll());
  }

  /**
   * Delete a notification by ID.
   *
   * @param id - The notification ID.
   */
  function del(id: number) {
    api.post('notification/delete', { json: { ids: [id] } }).then(() => getAll());
  }

  /**
   * Get the title of a notification.
   *
   * @param notification - The notification object.
   * @returns The title of the notification, processed for i18n if it's a template.
   */
  function title(notification: Notification): string {
    const title = notification.title;
    if (Object.hasOwn(TEMPLATES, title)) {
      return $_(`notification.${title.toLowerCase()}.title`);
    }
    return title;
  }

  /**
   * Get the content of a notification.
   *
   * @param notification - The notification object.
   * @returns The content of the notification, processed for i18n if it's a template.
   */
  function content(notification: Notification): string {
    const { title, content } = notification;
    if (Object.hasOwn(TEMPLATES, title)) {
      const options = { values: withConditionalFlags(JSON.parse(content), TEMPLATES[title]) };
      return $_(`notification.${title.toLowerCase()}.content`, options);
    }
    return content;
  }

  onMount(() => {
    if ($token) {
      getAll();
      // refresh the notifications every minute
      const refreshInterval = setInterval(() => getAll(true), 60 * 1000);
      return () => clearInterval(refreshInterval);
    }
  });
</script>

<div class="indicator {_class}">
  {#if unread > 0}
    <span class="indicator-item mt-1 badge badge-xs badge-primary">
      {unread > 99 ? '99+' : unread}
    </span>
  {/if}
  <button
    class="btn h-10 min-h-10 btn-subtle px-3 {triggerClass}"
    onclick={showModal}
    aria-label={$_('app.notifications')}
  >
    <iconify-icon icon={icons.alertUrgent} width="1.625rem" class="size-6.5 opacity-90"></iconify-icon>
  </button>
</div>

<Modal
  icon={icons.alertUrgent}
  title={$_('app.notifications')}
  maxWidth="42rem"
  bind:this={modal}
  onclose={() => api.post('notification/read').then(() => getAll())}
>
  <div class="flex items-center justify-between gap-2">
    <span class="mx-1 text-sm font-semibold opacity-50">
      {$_('data.paginator.total', $number(total))}
    </span>
    <button class="btn btn-subtle btn-sm" onclick={clear} disabled={total === 0}>
      <iconify-icon icon={icons.delete} width="1rem"></iconify-icon>
      {$_('action.clear', $_('entity.notifications'))}
    </button>
  </div>
  {#if notifications.length === 0}
    <div class="rounded-box border border-dashed py-10">
      <div class="h-6.5 text-center opacity-20">{$_('data.nodata')}</div>
    </div>
  {:else}
    <ul class="flex max-h-[50vh] flex-col gap-3 overflow-y-auto">
      {#each notifications as notification (notification.id)}
        <li class="flex items-start justify-between gap-1 rounded-box border p-2 shadow-sm">
          <div class="min-w-0 flex-1 space-y-2 pt-1.5 pl-2">
            <div class="flex items-center gap-2">
              {#if !notification.seen}
                <span class="status animate-bounce status-info"></span>
              {/if}
              <h4 class="truncate text-sm font-semibold text-surface">
                {title(notification)}
              </h4>
            </div>
            <p class="text-sm leading-6 whitespace-pre-wrap opacity-80">
              {content(notification)}
            </p>
            <time class="text-xs opacity-50">
              {$dateTime(notification.created_at)}
            </time>
          </div>
          <button
            class="btn btn-square btn-subtle btn-sm"
            aria-label={$_('action.delete', $_('entity.notification'))}
            onclick={() => del(notification.id)}
          >
            <iconify-icon icon={icons.deleteDismiss} width="1rem"></iconify-icon>
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</Modal>
