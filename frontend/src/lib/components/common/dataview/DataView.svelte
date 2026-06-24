<script lang="ts" generics="T">
  import type { ScrollPosition, ViewMode, ViewModes } from '$lib/types';
  import { throttle } from '$lib/utils';
  import type { Snippet } from 'svelte';
  import { tick } from 'svelte';
  import { slide } from 'svelte/transition';
  import Grid from './grid/Grid.svelte';
  import Table from './table/Table.svelte';
  import ViewSwitcher from './ViewSwitcher.svelte';

  type DataViewProps = {
    /** The supported view modes. */
    modes?: ViewModes;
    /** The active view mode. */
    mode?: ViewMode;
    /** The data to display. */
    data: T[];
    /** The unique key for each item. */
    uniqueKey?: keyof T;

    /** The table class names. */
    tableClass?: string;
    /** The table row class names. */
    rowClass?: string;
    /** The table header snippet. */
    header?: Snippet;
    /** The table row snippet. */
    row?: Snippet<[T, number]>;
    /** Whether clicking a table row toggles its checkbox. */
    rowSelect?: boolean;

    /** The grid class names. */
    gridClass?: string;
    /** The grid item class names. */
    itemClass?: string;
    /** The grid item snippet. */
    item?: Snippet<[T, number]>;

    /** Whether to show the loading overlay. */
    loading?: boolean | null;
    /** Whether to hide the operation panel on scroll. */
    hideOnScroll?: boolean;
    /** Whether to use the dynamic viewport height. */
    dvh?: boolean | null;

    /** The filters snippet. */
    filters?: Snippet;
    filtersClass?: string;
    /** The actions snippet. */
    actions?: Snippet;
    actionsClass?: string;
    /** The paginator snippet. */
    paginator?: Snippet<[boolean]>;
    /** The class names for the dataview. */
    class?: string;
    viewClass?: string;
  };

  let {
    modes = ['table'],
    mode = $bindable(modes[0]),
    data,
    uniqueKey,

    tableClass,
    rowClass,
    header,
    row,
    rowSelect,

    gridClass,
    itemClass,
    item,

    loading,
    hideOnScroll = false,
    dvh = hideOnScroll,

    filters,
    filtersClass,
    actions,
    actionsClass,
    paginator,
    class: _class,
    viewClass
  }: DataViewProps = $props();

  let view: HTMLDivElement;
  let viewScrollTop = 0;
  let panel: boolean = $derived(modes.length > 1 || !!filters || !!actions);
  let panelSlide: number = $state(200);
  let panelToggling: boolean = false;
  let timeoutId: ReturnType<typeof setTimeout> | null;

  /**
   * Captures the current scroll position of the view.
   */
  export function scrollPosition(): ScrollPosition {
    return { left: view.scrollLeft, top: view.scrollTop, panel: panel };
  }

  /**
   * Restores the scroll position of the view.
   *
   * @param options - The scroll position options.
   */
  export function scrollTo(options: ScrollPosition) {
    view.scrollTo(options);
    const panelOption = options.panel;
    if (panelOption !== undefined) {
      panelSlide = 0;
      setTimeout(() => {
        panel = panelOption;
        tick().then(() => {
          // reset the duration after the scroll
          panelSlide = 200;
        });
      });
    }
  }

  /**
   * The scroll event handler.
   *
   * @param end - Whether the scrolling has ended.
   */
  const onscroll = (end: boolean = false) => {
    if (!dvh || !hideOnScroll) {
      return;
    }
    if (timeoutId) {
      clearTimeout(timeoutId);
    }
    const current = view.scrollTop;
    // show the panel when the user scrolls to the top
    const endAtTop = end && !panel && current === 0;
    // hide or show the panel when the user scrolls down or up
    const hidePanel = !panelToggling && panel && current - viewScrollTop > 0;
    const showPanel = !panelToggling && !panel && viewScrollTop - current > 0;
    if (endAtTop || hidePanel || showPanel) {
      panel = !panel;
      panelToggling = true;
      setTimeout(() => (panelToggling = false), 200);
    }
    viewScrollTop = current;
    // simulate the scroll end event
    if (!end) {
      timeoutId = setTimeout(() => onscroll(true), 250);
    }
  };

  /**
   * The throttled scroll event handler.
   */
  const _onscroll = throttle(onscroll, 200);
</script>

<div class="flex w-full flex-col lg:px-2 {dvh && !hideOnScroll ? 'h-(--ks-svh) sm:h-(--ks-lvh)' : ''} {_class}">
  <!-- operation panel -->
  {#if panel}
    <div class="flex min-h-14 items-center gap-2 p-2" transition:slide={{ duration: panelSlide }}>
      <ViewSwitcher {modes} bind:mode />
      {#if filters}
        <!-- filters wrapper -->
        <span class="flex shrink grow gap-2 {filtersClass}">
          {@render filters()}
        </span>
      {/if}
      {#if actions}
        <!-- actions wrapper -->
        <span class="flex shrink-0 grow-0 gap-2 {actionsClass}">
          {@render actions()}
        </span>
      {/if}
    </div>
  {:else if dvh && hideOnScroll}
    <div class="z-1 h-px nav-shadow"></div>
  {/if}
  <!-- data view -->
  <div
    class="overflow-auto {dvh && hideOnScroll ? 'h-(--ks-svh) sm:h-(--ks-lvh)' : 'h-full'} {viewClass}"
    class:overscroll-none={dvh}
    onscroll={() => _onscroll()}
    bind:this={view}
  >
    <div class={dvh && hideOnScroll ? 'min-h-[calc(100%+1px)]' : ''}>
      {#if mode === 'grid' && item}
        <Grid {data} {uniqueKey} {loading} {item} {itemClass} {gridClass} />
      {:else if header && row}
        <Table {data} {uniqueKey} {loading} {header} {row} {rowSelect} {rowClass} {tableClass} />
      {/if}
      {#if paginator}
        {@render paginator(loading !== undefined && loading !== null)}
      {/if}
    </div>
  </div>
</div>
