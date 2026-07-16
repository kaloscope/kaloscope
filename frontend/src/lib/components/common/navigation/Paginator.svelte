<script lang="ts" module>
  export type PaginatorProps = {
    /** Current page number. */
    current: number;
    /** Size of each page. */
    size: number;
    /** Total number of entries. */
    total?: number | null;
    /** Total number of pages. */
    totalPages?: number | null;
    /** The callback function when the parameters changes. */
    onchange?: (page: number, size: number) => void;
    /** Whether the paginator is disabled. */
    disabled?: boolean;
    /** Whether to use simple mode. */
    simpleMode?: boolean;
  };
</script>

<script lang="ts">
  import { _, number } from '$lib/i18n';

  let {
    current = $bindable(),
    size = $bindable(),
    total: _total,
    totalPages,
    onchange,
    disabled = false,
    simpleMode = false
  }: PaginatorProps = $props();

  // `!= null` catches both null and undefined in a single check
  let total = $derived(_total ?? (totalPages != null ? totalPages * size : null));
  let isEstimated = $derived(_total == null && totalPages != null);

  let pages: number[] = $state([]);
  $effect(() => {
    if (simpleMode || total === null) {
      return;
    }
    const first = 1;
    const last = Math.max(first, Math.ceil(total / size));
    if (current > last) {
      // reset current page if it exceeds the last page
      onchange?.((current = last), size);
    }
    let start = first;
    let end = last;
    // adjust start and end if there are more than 10 pages
    if (end >= 10) {
      start = Math.max(first, current - 2);
      end = Math.min(last, current + 2);
      if (end - start < 4) {
        start === first ? (end = start + 4) : (start = end - 4);
      }
    }
    // generate pages array
    pages = Array.from(
      new Set([
        first,
        ellipsis(first, start),
        ...Array.from({ length: end - start + 1 }, (_, i) => start + i),
        ellipsis(end, last),
        last
      ])
    );
  });

  /**
   * Generate ellipsis number flag for pagination.
   *
   * @param left - The left number.
   * @param right - The right number.
   * @returns The ellipsis number flag.
   */
  function ellipsis(left: number, right: number) {
    const diff = right - left;
    if (diff > 1) {
      // use negative number to indicate ellipsis
      return diff === 2 ? left + 1 : -left;
    }
    return left;
  }
</script>

{#if total !== null}
  {@const bgClass = simpleMode ? 'bg-linear-to-t from-base-125 to-transparent to-50%' : 'bg-blur-90'}
  {@const showFirst = simpleMode && current > 2}
  <!-- whether the previous and next buttons are disabled -->
  {@const prevDisabled = current === 1}
  {@const nextDisabled = simpleMode ? total < size : current === pages[pages.length - 1]}
  <!-- hide paginator if there is only one page or both previous and next buttons are disabled -->
  {#if !(prevDisabled && nextDisabled) && (simpleMode || pages.length > 1)}
    {@const btnClass = 'btn btn-sm'}
    {@const prevClass = `${btnClass} ${simpleMode ? 'join-item' : 'btn-subtle'}`}
    {@const nextClass = `${btnClass} ${simpleMode ? 'join-item' : 'btn-subtle'}`}
    <div class="bottom-0 lg:sticky">
      <div
        style:pointer-events={disabled ? 'none' : 'auto'}
        class="flex h-14 w-full items-center overflow-x-auto rounded-t-md px-2 {bgClass}"
      >
        <!-- total number of entries -->
        {#if !simpleMode && !isEstimated}
          <span class="mx-2 truncate text-sm font-semibold opacity-50">
            {$_('data.paginator.total', $number(total))}
          </span>
        {/if}

        <div
          class={simpleMode
            ? `join mx-auto grid rounded-field shadow-sm ${showFirst ? 'grid-cols-4' : 'grid-cols-3'}`
            : 'ml-auto flex gap-2 sm:mx-auto sm:gap-1'}
        >
          <!-- first page button -->
          {#if showFirst}
            <button class="text-base-content/80 {prevClass}" onclick={() => onchange?.((current = 1), size)}>
              {$_('data.paginator.first')}
            </button>
          {/if}

          <!-- previous page button -->
          {#if prevDisabled}
            <button class="btn-disabled {simpleMode ? 'bg-base-300!' : ''} {prevClass}">
              {$_('data.paginator.previous')}
            </button>
          {:else}
            <button class="text-base-content/80 {prevClass}" onclick={() => onchange?.(--current, size)}>
              &lt; {$_('data.paginator.previous')}
            </button>
          {/if}

          <!-- page buttons -->
          {#if simpleMode}
            <button class="pointer-events-none join-item text-base-content/50 {btnClass}">
              {$_('data.paginator.current', current)}
            </button>
          {:else}
            {#each pages as num (num)}
              {#if num < 0}
                <button class="pointer-events-none btn-subtle px-1 max-sm:hidden {btnClass}">...</button>
              {:else}
                {@const activeClass = current === num ? 'item-emphasis active:!bg-emphasis' : 'btn-subtle'}
                <button
                  class="px-[0.7rem] max-sm:hidden {btnClass} {activeClass}"
                  onclick={() => current !== num && onchange?.((current = num), size)}
                >
                  {num}
                </button>
              {/if}
            {/each}
            <!-- select dropdown for mobile -->
            <select
              class="select max-w-24 appearance-none truncate select-sm sm:hidden"
              bind:value={current}
              onchange={() => onchange?.(current, size)}
            >
              {#each pages as num (num)}
                {#if num < 0}
                  <option disabled>...</option>
                {:else}
                  <option value={num}>{num}</option>
                {/if}
              {/each}
            </select>
          {/if}

          <!-- next page button -->
          {#if nextDisabled}
            <button class="btn-disabled {simpleMode ? 'bg-base-300!' : ''} {nextClass}">
              {$_('data.paginator.next')}
            </button>
          {:else}
            <button class="text-base-content/80 {nextClass}" onclick={() => onchange?.(++current, size)}>
              {$_('data.paginator.next')} &gt;
            </button>
          {/if}
        </div>
      </div>
    </div>
  {/if}
{/if}
