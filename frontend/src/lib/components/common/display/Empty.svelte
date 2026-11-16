<script lang="ts" module>
  import type { IconifyIcon } from 'iconify-icon';
  import type { Snippet } from 'svelte';

  export type EmptyProps = {
    /** The illustration icon. */
    icon?: string | IconifyIcon;
    /** The headline of the placeholder. */
    title?: string;
    /** The supporting text below the headline. */
    description?: string;
    /** Whether to use the smaller inline layout. */
    compact?: boolean;
    /** The class names for the container. */
    class?: string;
    /** The actions rendered below the text. */
    children?: Snippet;
  };
</script>

<script lang="ts">
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { fade } from 'svelte/transition';

  let {
    icon = icons.collectionsEmpty,
    title,
    description,
    compact = false,
    class: _class,
    children
  }: EmptyProps = $props();
</script>

<div
  class="empty flex-col-center w-full text-center {compact ? 'gap-3 py-6' : 'gap-5 py-12'} {_class}"
  in:fade={{ duration: 200 }}
>
  <div class="empty-art flex-center" class:empty-compact={compact} aria-hidden="true">
    <span class="empty-ring"></span>
    <span class="empty-ring empty-ring-outer"></span>
    <span class="empty-dot empty-dot-a"></span>
    <span class="empty-dot empty-dot-b"></span>
    <span class="empty-dot empty-dot-c"></span>
    <span class="empty-badge flex-center">
      <iconify-icon {icon} width={compact ? '1.75rem' : '2.5rem'}></iconify-icon>
    </span>
  </div>
  <div class="flex max-w-md flex-col gap-1.5 px-4">
    <span class="font-semibold text-base-content/70 {compact ? 'text-sm' : 'text-lg'}">
      {title ?? $_('data.nodata')}
    </span>
    {#if description}
      <p class="leading-relaxed text-balance text-base-content/50 {compact ? 'text-xs' : 'text-sm'}">
        {description}
      </p>
    {/if}
  </div>
  {#if children}
    <div class="flex flex-wrap items-center justify-center gap-2">
      {@render children()}
    </div>
  {/if}
</div>

<style>
  .empty-art {
    position: relative;
    width: 9rem;
    height: 9rem;
  }

  .empty-compact {
    width: 5.5rem;
    height: 5.5rem;
  }

  .empty-badge {
    position: relative;
    width: 56%;
    height: 56%;
    border-radius: 32%;
    background-color: var(--color-base-100);
    background-image: linear-gradient(
      140deg,
      color-mix(in oklab, var(--color-primary) 16%, transparent),
      color-mix(in oklab, var(--color-primary) 4%, transparent)
    );
    color: color-mix(in oklab, var(--color-primary) 55%, var(--color-base-content));
    box-shadow:
      0 0 0 1px color-mix(in oklab, var(--color-primary) 18%, transparent),
      0 10px 24px -10px color-mix(in oklab, var(--color-primary) 45%, transparent);
    transform: rotate(-6deg);

    :global(iconify-icon) {
      transform: rotate(6deg);
    }
  }

  .empty-ring {
    position: absolute;
    inset: 12%;
    border: 1px dashed color-mix(in oklab, var(--color-base-content) 16%, transparent);
    border-radius: calc(infinity * 1px);
  }

  .empty-ring-outer {
    inset: 0;
    border-style: solid;
    border-color: color-mix(in oklab, var(--color-base-content) 7%, transparent);
  }

  .empty-dot {
    position: absolute;
    border-radius: calc(infinity * 1px);
  }

  .empty-dot-a {
    top: 9%;
    right: 20%;
    width: 0.5rem;
    height: 0.5rem;
    background-color: color-mix(in oklab, var(--color-primary) 55%, transparent);
  }

  .empty-dot-b {
    bottom: 16%;
    left: 8%;
    width: 0.375rem;
    height: 0.375rem;
    background-color: color-mix(in oklab, var(--color-secondary) 55%, transparent);
  }

  .empty-dot-c {
    right: 4%;
    bottom: 30%;
    width: 0.25rem;
    height: 0.25rem;
    background-color: color-mix(in oklab, var(--color-accent) 60%, transparent);
  }
</style>
