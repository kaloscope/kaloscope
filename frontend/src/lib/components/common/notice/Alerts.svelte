<script lang="ts" module>
  import type { IconifyIcon } from 'iconify-icon';

  export type AlertsProps = {
    /** Whether to show alerts in dialog. */
    dialog?: boolean;
    /** Maximum number of alerts to show. */
    maxSize?: number;
    /** Default timeout for each alert in milliseconds. */
    timeout?: number;
  };

  export type Alert = {
    /** The level of the alert. */
    level?: 'info' | 'success' | 'warning' | 'error';
    /** The message of the alert. */
    message: string;
    /** The timeout for the alert in milliseconds. */
    timeout?: number;
    /** Whether the alert message is unique. */
    unique?: boolean;
  };

  // reactive store for the dialog ID
  let _dialogId: string = $state('');

  // reactive store for the alert message
  let _alert: Alert | null = $state(null);

  /**
   * Show an alert message.
   *
   * @param alert - Alert message instance.
   */
  export function alert(message: Alert | string | null) {
    if (typeof message === 'string') {
      message = { message };
    }
    _alert = message;
  }

  // mapping of alert levels to colors and icons
  const mappings: Record<string, { color: string; icon: IconifyIcon }> = {
    info: { color: 'var(--color-info)', icon: icons.info },
    success: { color: 'var(--color-success)', icon: icons.checkmarkCircle },
    warning: { color: 'var(--color-warning)', icon: icons.warning },
    error: { color: 'var(--color-error)', icon: icons.dismissCircle }
  };
</script>

<script lang="ts">
  import { afterNavigate } from '$app/navigation';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { fly, slide } from '$lib/utils/motion';
  import { onMount } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import { fade } from 'svelte/transition';
  import { v4 as uuidv4 } from 'uuid';

  const { dialog = false, maxSize = 5, timeout = 3000 }: AlertsProps = $props();
  const dialogId = $derived(dialog ? uuidv4() : '');

  // the reactive map of alert instances
  const alerts = new SvelteMap<string, Alert>();

  // whether to show alerts in this component
  let instance = $derived(dialogId === _dialogId ? _alert : null);
  $effect(() => {
    if (instance) {
      const id = instance.unique ? instance.message : uuidv4();
      alerts.set(id, instance);
      setTimeout(() => alerts.delete(id), instance.timeout || timeout);
      alert(null);
    }
    return () => {
      if (alerts.size > maxSize) {
        const [id] = alerts.keys();
        alerts.delete(id);
      }
    };
  });

  // the bottom navigation bar element
  let dockElement: Element | null = $state(document.querySelector('.dock'));
  afterNavigate(() => (dockElement = document.querySelector('.dock')));

  // update the dialog ID state
  onMount(() => {
    const temp = _dialogId;
    _dialogId = dialogId;
    return () => (_dialogId = temp);
  });
</script>

{#if alerts.size > 0}
  {@const bottom = !dialog && dockElement ? 'mb-(--ks-dock-h) sm:mb-0' : 'mb-0'}
  <div class="pointer-events-none toast toast-end layer-5 {bottom}" in:fly={{ y: 200 }} out:fade>
    {#each alerts.entries() as [id, alert] (id)}
      {@const { icon, color } = mappings[alert.level ?? 'info']}
      <div role="alert" class="alert popover-surface" in:slide={{ duration: 100 }} out:fade>
        <iconify-icon {icon} style:color width="1.5rem" class="size-6"></iconify-icon>
        <span>{$_(`alert.${alert.message}`, { default: alert.message })}</span>
      </div>
    {/each}
  </div>
{/if}
