<script lang="ts" module>
  import { sniffer } from '$lib/utils/browser';
  import { flip } from '$lib/utils/motion';

  const isSafari = sniffer.isSafari();
  const INTERACTIVE_SELECTOR = [
    'a',
    'button',
    'input',
    'select',
    'summary',
    'textarea',
    '[contenteditable="true"]',
    '[role="button"]'
  ].join(',');

  /**
   * Toggle the row checkbox unless the click starts from an interactive element.
   *
   * @param event - Click event.
   */
  function toggleRowSelection(event: MouseEvent) {
    const { target, currentTarget } = event;
    if (target instanceof Element && target.closest(INTERACTIVE_SELECTOR)) {
      return;
    }
    if (!(currentTarget instanceof Element)) {
      return;
    }
    currentTarget.querySelector<HTMLInputElement>('input.sub-checkbox:not(:disabled)')?.click();
  }
</script>

<script lang="ts" generics="T">
  import { Empty, Overlay, Skeleton, type SkeletonProps } from '$lib/components';
  import { _ } from '$lib/i18n';
  import type { Snippet } from 'svelte';
  import { fade } from 'svelte/transition';

  type TableProps = {
    /** The table header snippet. */
    header: Snippet;
    /** The table data. */
    data: T[];
    /** The table row snippet. */
    row: Snippet<[T, number]>;
    /** The unique key for each row. */
    uniqueKey?: keyof T;
    /** Whether to show the loading overlay. */
    loading?: boolean | null;
    /** The class names for the container. */
    class?: string;
    /** The class names for the table. */
    tableClass?: string;
    /** The class names for each row. */
    rowClass?: string;
    /** Whether clicking a row toggles its checkbox. */
    rowSelect?: boolean;
    /** The placeholder shown when there is no data. */
    empty?: Snippet;
    /** The placeholder rows shown while the first data is loading. */
    skeleton?: SkeletonProps | false;
  };

  let {
    header,
    data,
    row,
    uniqueKey,
    loading,
    class: _class,
    tableClass,
    rowClass,
    rowSelect,
    empty,
    skeleton
  }: TableProps = $props();
  let notLoading = $derived(loading === null || loading === undefined);
  let notEmpty = $derived(data.length > 0);
</script>

<div class="w-full px-1 {_class}" in:fade>
  <table class:table-composite-fix={isSafari} class="table-pin-rows table table-fixed {tableClass}">
    <thead>
      <tr class="layer-5 bg-base-125 {notEmpty ? 'border-base-content/10' : 'border-b-0'}">
        {@render header()}
      </tr>
    </thead>
    {#if notEmpty}
      <tbody class="relative" in:fade>
        <Overlay {loading} />
        {@render body()}
      </tbody>
    {:else if !notLoading && skeleton}
      <!-- stay invisible during the short grace period to avoid flashing -->
      <tbody class="transition-opacity duration-300 {loading ? '' : 'opacity-0'}" aria-busy="true">
        <tr class="sr-only">
          <td><span role="status">{$_('data.loading')}</span></td>
        </tr>
        {#each { length: 6 }, i}
          <tr class="border-base-content/10">
            <td class="px-0" colspan="1000">
              <Skeleton type="row" {...skeleton} class={i % 2 ? 'opacity-80' : ''} />
            </td>
          </tr>
        {/each}
      </tbody>
    {:else}
      <tbody class="relative" in:fade>
        <Overlay {loading} />
        {@render nodata()}
      </tbody>
    {/if}
  </table>
</div>

{#snippet body()}
  {#if uniqueKey}
    {#each data as rowData, index (rowData[uniqueKey])}
      <tr
        class="border-base-content/10 hover:bg-base-200 {rowClass}"
        onclick={rowSelect ? toggleRowSelection : undefined}
        animate:flip={{ duration: 500 }}
      >
        {@render row(rowData, index)}
      </tr>
    {/each}
  {:else}
    {#each data as rowData, index (index)}
      <tr
        class="border-base-content/10 hover:bg-base-200 {rowClass}"
        onclick={rowSelect ? toggleRowSelection : undefined}
        transition:fade={{ duration: 200 }}
      >
        {@render row(rowData, index)}
      </tr>
    {/each}
  {/if}
{/snippet}

{#snippet nodata()}
  <tr class="border-b-0">
    <td class="h-96" colspan="1000">
      {#if notLoading}
        {#if empty}
          {@render empty()}
        {:else}
          <Empty />
        {/if}
      {/if}
    </td>
  </tr>
{/snippet}
