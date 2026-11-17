<script lang="ts" module>
  import type { Snippet } from 'svelte';

  export type ContainerProps = {
    /** The container content snippet. */
    children: Snippet;
    /** Whether to show the loading overlay. */
    loading?: boolean | null;
    /** The placeholder layout shown while loading, replacing the overlay. */
    skeleton?: Snippet;
    /** Whether to use the dynamic viewport height. */
    dvh?: boolean | null;
    /** The class names for the container. */
    class?: string;
    /** The key of the preset styles. */
    type?: keyof typeof PRESETS;
    width?: string;
    minWidth?: string;
    maxWidth?: string;
    rowGap?: string;
    padding?: string;
  };

  /**
   * Preset styles for the container.
   */
  const PRESETS = {
    default: {
      width: '100%',
      minWidth: '0',
      maxWidth: '100%',
      rowGap: '0',
      padding: '0'
    },
    settings: {
      width: '100%',
      minWidth: '24rem',
      maxWidth: '40rem',
      rowGap: '4rem',
      padding: '1rem 1rem 4rem'
    }
  };
</script>

<script lang="ts">
  import { Overlay, Skeleton } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { fade } from 'svelte/transition';

  let {
    children,
    loading,
    skeleton,
    dvh = false,
    class: _class,
    type = 'default',
    width = '',
    minWidth = '',
    maxWidth = '',
    rowGap = '',
    padding = ''
  }: ContainerProps = $props();

  const preset = $derived(PRESETS[type]);
</script>

<div
  class="relative mx-auto {_class}"
  style:width={width || preset.width}
  style:min-width={minWidth || preset.minWidth}
  style:max-width={maxWidth || preset.maxWidth}
>
  {#if skeleton || type === 'settings'}
    {#if loading !== null && loading !== undefined}
      <!-- stay invisible during the short grace period to avoid flashing -->
      <div class="transition-opacity duration-300 {loading ? '' : 'opacity-0'}" aria-busy="true">
        <span class="sr-only" role="status">{$_('data.loading')}</span>
        {#if skeleton}
          {@render skeleton()}
        {:else}
          <!-- the default placeholder for setting sections -->
          <div class="flex flex-col" style:row-gap={rowGap || preset.rowGap} style:padding={padding || preset.padding}>
            {#each [3, 2] as rows (rows)}
              <div>
                <Skeleton type="block" class="h-6 w-36" />
                <div class="divider"></div>
                <div class="flex flex-col gap-5 p-1">
                  {#each { length: rows }, i}
                    <div class="flex flex-col gap-2">
                      <Skeleton type="block" class="h-3 {i % 2 ? 'w-32' : 'w-24'}" />
                      <Skeleton type="block" class="h-9 w-full" />
                    </div>
                  {/each}
                </div>
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
  {:else}
    <Overlay dvh {loading} />
  {/if}
  {#if loading === null || loading === undefined}
    <div
      class="flex flex-col {dvh ? 'h-(--ks-svh) sm:h-(--ks-lvh)' : ''}"
      style:row-gap={rowGap || preset.rowGap}
      style:padding={padding || preset.padding}
      in:fade={{ duration: 150 }}
    >
      {@render children()}
    </div>
  {/if}
</div>
