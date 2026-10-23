<script lang="ts" module>
  import type { IconifyIcon } from 'iconify-icon';
  import type { Snippet } from 'svelte';
  import type { MouseEventHandler } from 'svelte/elements';

  export type DropdownProps = {
    /** The dropdown content snippet. */
    children: Snippet;
    /** The trigger button snippet. */
    trigger?: Snippet;
    /** The trigger button text. */
    triggerText?: string;
    /** The trigger button icon. */
    triggerIcon?: string | IconifyIcon;
    /** The width of the dropdown content. */
    contentWidth?: string;
    /** The max height of the dropdown content. */
    contentMaxHeight?: string;
    /** The class names for the dropdown. */
    class?: string;
    triggerClass?: string;
    contentClass?: string;
    /** Whether the dropdown is expanded. */
    open?: boolean;
    /** Whether clicking a content button closes the dropdown. */
    closeOnClick?: boolean;
    /** Whether clicks outside the dropdown dismiss it. */
    closeOnOutsideClick?: boolean;
    /** The click event handler. */
    onclick?: MouseEventHandler<HTMLElement>;
  };

  /**
   * Close all dropdowns except the current one.
   *
   * @param currentTarget - Current dropdown's event target.
   */
  export function closeDropdowns(currentTarget?: EventTarget & HTMLElement) {
    document.querySelectorAll('details.dropdown').forEach((dropdown) => {
      if (dropdown !== currentTarget?.closest('details.dropdown')) {
        dropdown.removeAttribute('open');
      }
    });
  }
</script>

<script lang="ts">
  let {
    children,
    trigger,
    triggerText,
    triggerIcon,
    contentWidth = '14rem',
    contentMaxHeight = 'var(--ks-svh)',
    class: _class,
    triggerClass,
    contentClass,
    open = $bindable(false),
    closeOnClick = true,
    closeOnOutsideClick = true,
    onclick
  }: DropdownProps = $props();

  let dropdown: HTMLDetailsElement;
  let dropdownContent: HTMLElement | null = $state(null);

  $effect(() => {
    if (open) {
      // scroll the dropdown content into view when opened
      dropdownContent?.scrollIntoView({
        behavior: 'smooth',
        block: 'nearest'
      });
    }
  });
</script>

<svelte:window
  onclick={(event) => {
    if (!(event.target instanceof Node)) {
      return;
    }
    const outside = !dropdown.contains(event.target);
    if ((outside && closeOnOutsideClick) || (!outside && closeOnClick && event.target instanceof HTMLButtonElement)) {
      open = false;
    }
  }}
/>

<details class="dropdown {_class}" bind:this={dropdown} bind:open>
  <summary class="flex-center {onclick ? 'btn-scale' : ''} {triggerClass}" {onclick}>
    {#if trigger}
      {@render trigger()}
    {:else}
      <!-- can't use <button> here because summary can't contain interactive content -->
      <div class="btn h-10 min-h-10 gap-1 btn-subtle px-3">
        {#if triggerText}
          <span class="font-semibold">{triggerText}</span>
        {/if}
        {#if triggerIcon}
          <iconify-icon icon={triggerIcon} width="1.5rem" class="size-6 opacity-90"></iconify-icon>
        {/if}
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" class="size-4 opacity-60">
          <path
            fill="none"
            stroke="currentColor"
            stroke-linecap="round"
            stroke-linejoin="round"
            stroke-width="3"
            d="m6 9l6 6l6-6"
          />
        </svg>
      </div>
    {/if}
  </summary>
  <div
    bind:this={dropdownContent}
    class="dropdown-content overflow-auto rounded-box border bg-base-100 shadow-xl [&_.menu]:w-full {contentClass}"
    style:max-height={contentMaxHeight}
    style:width={contentWidth}
  >
    {@render children()}
  </div>
</details>
