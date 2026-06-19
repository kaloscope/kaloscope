<script lang="ts" module>
  import type { IconifyIcon } from 'iconify-icon';
  import type { Snippet } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';

  export type ModalProps = Partial<{
    /** Whether the dialog can be closed by navigating back. */
    shallow: boolean;
    /** The icon to display before the title. */
    icon: string | IconifyIcon;
    /** The title of the modal dialog. */
    title: string;
    /** The content of the modal dialog. */
    children: Snippet;
    /** The maximum width of the modal dialog. */
    maxWidth: string;
    /** The class names for the modal dialog. */
    class: string;
    boxClass: string;
    cornerClass: string;
    /** The callback function when the dialog is closed. */
    onclose: () => void;
    /** Whether the dialog is closed explicitly. */
    explicit?: boolean;
  }>;

  /**
   * The reactive map of modal dialogs.
   */
  const modals = new SvelteMap<string, ModalProps>();

  /**
   * The duration of the modal dialog transition in milliseconds.
   */
  const TRANSITION_DURATION = 200;
</script>

<script lang="ts">
  import { pushState } from '$app/navigation';
  import { portal } from '$lib/actions';
  import { Alerts } from '$lib/components';
  import { messages } from '$lib/components/common/notice/Messages.svelte';
  import { freeze } from '$lib/stores';
  import { italic, sniffer } from '$lib/utils';
  import { tick } from 'svelte';
  import { fade } from 'svelte/transition';
  import { v4 as uuidv4 } from 'uuid';

  let {
    shallow = !sniffer.isAndroidEdge(),
    icon,
    title,
    children,
    maxWidth,
    class: _class,
    boxClass,
    cornerClass,
    onclose
  }: Omit<ModalProps, 'explicit'> = $props();
  let dialog: HTMLDialogElement | null = $state(null);
  const id: string = `modal-${uuidv4()}`;

  /**
   * Show the modal dialog.
   */
  export function show() {
    if (shallow) {
      pushState('', {});
    }
    modals.set(id, { shallow, onclose, explicit: false });
    sniffer.isMobileSafari() && freeze.set(true);
    tick().then(() => dialog?.showModal());
  }

  /**
   * Close the modal dialog.
   *
   * @param event - The mouse event that triggered the close action.
   */
  export function close(event: MouseEvent | null = null) {
    if (event) {
      event.preventDefault();
    }
    let modal = modals.get(id);
    if (modal) {
      modal.explicit = true;
      if (modal.onclose) {
        setTimeout(() => modal.onclose?.(), TRANSITION_DURATION);
      }
      if (shallow) {
        history.back();
      } else {
        modals.delete(id);
        freeze.set(false);
      }
    }
  }

  /**
   * Check if the modal dialog is currently open.
   *
   * @return True if the modal dialog is open, false otherwise.
   */
  export function isOpen(): boolean {
    return modals.has(id);
  }
</script>

<!-- close the topmost modal dialog when the user navigates back -->
<svelte:window
  onpopstate={(event) => {
    if (messages.size === 0 && modals.size > 0) {
      const [id, modal] = Array.from(modals.entries()).pop() ?? [];
      if (id && modal && modal.shallow) {
        event.stopPropagation();
        if (!modal.explicit && modal.onclose) {
          setTimeout(() => modal.onclose?.(), TRANSITION_DURATION);
        }
        modals.delete(id);
        freeze.set(false);
      }
    }
  }}
/>

{#if modals.has(id)}
  <dialog
    {id}
    use:portal
    bind:this={dialog}
    class="modal transition-none {_class}"
    transition:fade={{ duration: TRANSITION_DURATION }}
  >
    <form method="dialog" class="modal-backdrop">
      <button onclick={close} aria-label="Close"></button>
    </form>
    <div class="modal-box {boxClass}" style:max-width={maxWidth}>
      <form method="dialog" class="modal-corner {cornerClass}">
        <button onclick={close}>✕</button>
      </form>
      {#if title}
        <h3 class="modal-title">
          {#if icon}
            <iconify-icon {icon} width="1.5rem" class="size-6 opacity-90"></iconify-icon>
          {/if}
          <!-- eslint-disable-next-line svelte/no-at-html-tags -->
          {@html italic(title)}
        </h3>
      {/if}
      {#if children}
        {@render children()}
      {/if}
    </div>
    <Alerts dialog />
  </dialog>
{/if}
