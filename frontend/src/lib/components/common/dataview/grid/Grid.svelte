<script lang="ts" generics="T">
  import { Overlay } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { throttle } from '$lib/utils/timing';
  import type { Snippet } from 'svelte';
  import { flip } from 'svelte/animate';
  import { fade } from 'svelte/transition';

  type GridProps = {
    /** The grid data. */
    data: T[];
    /** The grid item snippet. */
    item: Snippet<[T, number]>;
    /** The grid tail snippet. */
    tail?: Snippet;
    /** The unique key for each item. */
    uniqueKey?: keyof T;
    /** The callback function when the items are dragged. */
    ondragged?: (data: T[]) => void;
    /** Whether the grid items are draggable. */
    draggable?: boolean;
    /** Whether to show the loading overlay. */
    loading?: boolean | null;
    /** The number of columns for the grid. */
    cols?: number;
    /** The gap between the grid items. */
    gap?: string;
    /** The class names for the container. */
    class?: string;
    /** The class names for the grid. */
    gridClass?: string;
    /** The class names for each item. */
    itemClass?: string;
    /** The class names for the tail item. */
    tailClass?: string;
  };

  let {
    data,
    item,
    tail,
    uniqueKey,
    ondragged,
    draggable = !!ondragged,
    loading,
    cols,
    gap,
    class: _class,
    gridClass,
    itemClass,
    tailClass
  }: GridProps = $props();
  let notLoading = $derived(loading === null || loading === undefined);
  let notEmpty = $derived(data.length > 0 || !!tail);
  let draggedItem: T | null = null;

  /**
   * Handle the drag start event.
   *
   * @param event - Drag event.
   * @param item - Item being dragged.
   */
  function ondragstart(event: DragEvent, item: T) {
    const target = event.currentTarget;
    if (target instanceof HTMLLIElement) {
      setTimeout(() => target.classList.add('!shadow-none', '!border-none', '*:invisible'));
      draggedItem = item;
    }
  }

  /**
   * Handle the drag enter event.
   *
   * @param item - Item being dragged over.
   */
  function ondragenter(item: T) {
    if (draggedItem !== null && draggedItem !== item) {
      drop(draggedItem, data.indexOf(item));
    }
  }

  /**
   * Handle the drag end event.
   *
   * @param event - Drag event.
   */
  function ondragend(event: DragEvent) {
    const target = event.currentTarget;
    if (target instanceof HTMLLIElement) {
      target.classList.remove('!shadow-none', '!border-none', '*:invisible');
      draggedItem = null;
      ondragged?.(data);
    }
  }

  /**
   * Move an item to a new position.
   *
   * @param item - Item to move.
   * @param index - New index to move to.
   */
  const drop = throttle((item: T, index: number) => {
    const origIndex = data.indexOf(item);
    if (origIndex < index) {
      data = [
        // move down the item to new index
        ...data.slice(0, origIndex),
        ...data.slice(origIndex + 1, index + 1),
        item,
        ...data.slice(index + 1)
      ];
    } else if (index < origIndex) {
      data = [
        // move up the item to new index
        ...data.slice(0, index),
        item,
        ...data.slice(index, origIndex),
        ...data.slice(origIndex + 1)
      ];
    }
  });
</script>

<svelte:window ondragover={(event) => event.preventDefault()} />

<div class="w-full px-2 py-4 {_class}" in:fade>
  {#if notEmpty}
    <ul
      role="list"
      style:gap
      style:grid-template-columns={cols ? `repeat(${cols}, minmax(0, 1fr))` : ''}
      class="relative grid gap-2 lg:gap-4 {gridClass}"
      in:fade
    >
      <Overlay {loading} />
      {@render body()}
      {#if tail && (data.length || notLoading)}
        <li class="relative {tailClass}">
          {@render tail()}
        </li>
      {/if}
    </ul>
  {:else}
    <ul role="list" class="relative grid grid-cols-1" in:fade>
      <Overlay {loading} />
      {@render nodata()}
    </ul>
  {/if}
</div>

{#snippet body()}
  {#if uniqueKey}
    {#each data as itemData, index (itemData[uniqueKey])}
      <li
        {draggable}
        ondragstart={(event) => ondragstart(event, itemData)}
        ondragenter={() => ondragenter(itemData)}
        ondragend={(event) => ondragend(event)}
        class="relative min-w-0 rotate-0 duration-200 {itemClass} {draggable ? 'cursor-move' : ''}"
        animate:flip={{ duration: 500 }}
      >
        {@render item(itemData, index)}
      </li>
    {/each}
  {:else}
    {#each data as itemData, index (index)}
      <li class="relative min-w-0 rotate-0 duration-200 {itemClass}" transition:fade={{ duration: 200 }}>
        {@render item(itemData, index)}
      </li>
    {/each}
  {/if}
{/snippet}

{#snippet nodata()}
  <li class="col-span-full h-96">
    {#if notLoading}
      <div class="flex-col-center h-full gap-2 opacity-20" in:fade={{ duration: 200 }}>
        <iconify-icon icon={icons.collectionsEmpty} width="5rem" aria-hidden="true"></iconify-icon>
        <span class="text-2xl">{$_('data.nodata')}</span>
      </div>
    {/if}
  </li>
{/snippet}
