<script lang="ts" module>
  import type { Snippet } from 'svelte';
  import type { Readable } from 'svelte/store';

  export type HCellProps = Partial<{
    /** Whether to render the header cell. */
    condition: boolean;
    /** The header cell snippet to render. */
    children: Snippet;
    /** The header cell text to display. */
    text: string;
    /** Whether the header cell contains actions. */
    actions: boolean;
    /** The header cell width on desktop and mobile. */
    width: string | [string, string | null];
    /** The class names for the header cell. */
    class: string;
    /** The sort field store. */
    sort: {
      asc: string;
      desc: string;
      toggle: () => void;
    } & Readable<string>;
  }>;
</script>

<script lang="ts">
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { MediaQuery } from 'svelte/reactivity';
  import { scale } from 'svelte/transition';

  let {
    condition = true,
    children,
    text,
    actions = false,
    width = actions ? ['6rem', '3rem'] : undefined,
    class: _class,
    sort
  }: HCellProps = $props();

  // the screen width media query
  const desktop = new MediaQuery('(min-width: 64rem)');

  // the header cell width
  let cellWidth = $derived.by(() => {
    const isArray = Array.isArray(width);
    if (desktop.current) {
      return isArray ? width[0] : width;
    } else {
      return (isArray ? width[1] : width) || '0';
    }
  });

  // the header cell text
  let cellText: HCellProps['text'] = $derived(text ?? (actions ? $_('field.actions') : ''));
</script>

{#if condition}
  <th
    title={cellText?.toLocaleUpperCase()}
    class={children || cellText ? 'px-0 pt-1 pb-2' : 'p-0'}
    style:width={cellWidth}
  >
    <div
      class="flex {children ? 'pl-2' : 'pl-1'} {actions ? 'justify-center pr-1' : ''} {_class}"
      class:hidden={cellWidth === '0'}
    >
      {#if children}
        {@render children()}
      {:else if cellText}
        {@const sortClass = sort ? 'btn btn-subtle btn-sm' : ''}
        {@const sortedClass =
          sort && ($sort === sort.asc || $sort === sort.desc) ? 'text-base-content' : 'text-inherit'}
        <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
        <div
          tabindex={sort ? 0 : undefined}
          role={sort ? 'button' : 'generic'}
          class="group flex min-h-8 min-w-0 items-center p-1 transition-all duration-300 {sortClass} {sortedClass}"
          onclick={() => sort && sort.toggle()}
        >
          <span class="truncate pt-px text-xs font-semibold uppercase">
            {#if actions && !desktop.current}
              <iconify-icon icon={icons.wrenchSettings} width="1rem"></iconify-icon>
            {:else}
              {cellText}
            {/if}
          </span>
          {@render sorter()}
        </div>
      {/if}
    </div>
  </th>
{/if}

{#snippet sorter()}
  {#if sort}
    <div class="size-4">
      {#if $sort === sort.asc}
        <iconify-icon icon={icons.arrowUp} width="1rem" in:scale></iconify-icon>
      {:else if $sort === sort.desc}
        <iconify-icon icon={icons.arrowDown} width="1rem" in:scale></iconify-icon>
      {:else}
        <iconify-icon
          icon={icons.sort}
          width="1rem"
          class="opacity-0 transition-opacity duration-300 group-hover:opacity-100"
          in:scale
        ></iconify-icon>
      {/if}
    </div>
  {/if}
{/snippet}
