<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import {
    Backdrop,
    Container,
    DataView,
    Image,
    MediaActions,
    Paginator,
    Rating,
    Search,
    type PaginatorProps
  } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { attachMediaProgress, hasProgress, isWatched, loadMediaProgress } from '$lib/progress';
  import { captureScrollPosition, compactRecord, restorePosition, subroutes, user } from '$lib/stores';
  import type { MediaItem, Page, Resp } from '$lib/types';
  import { tick, untrack } from 'svelte';
  import { MediaQuery } from 'svelte/reactivity';
  import { get } from 'svelte/store';
  import { queryParameters, ssp } from 'sveltekit-search-params';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  // the library ID
  let libId: string = $derived(page.params.lib_id ?? '');

  // the URL query parameters
  const query = queryParameters(
    {
      page_num: ssp.number(0),
      page_size: ssp.number(0),
      keyword: ssp.string('')
    },
    {
      pushHistory: false
    }
  );

  // the data view instance
  let view: DataView<MediaItem>;

  // the items to display
  let items: MediaItem[] = $state([]);
  let backdrop: string | null = $derived(items.length > 0 ? (items[0].backdrop ?? items[0].poster) : null);
  let pagination: Omit<PaginatorProps, 'current' | 'size'> = $state({ onchange: () => search(true) });

  // the loading states
  const outerLoading = createLoading();
  const innerLoading = createLoading();

  // the abort controller
  let abortController: AbortController | null = null;

  // the standalone display mode media query
  const standaloneMode = new MediaQuery('(display-mode: standalone)');

  // capture the scroll position of the current page
  beforeNavigate(({ from, to }) => {
    captureScrollPosition(from, to, view, standaloneMode.current);
  });

  /**
   * Search for media items.
   *
   * @param toTop - Whether to scroll to the top of the page after the search.
   */
  function search(toTop: boolean = false) {
    let aborted = false;
    // abort the previous request
    abortController?.abort();
    // execute the search request
    abortController = new AbortController();
    innerLoading.start();
    api
      .get('media/list', {
        signal: abortController.signal,
        searchParams: {
          page_num: query.page_num,
          page_size: query.page_size,
          lib_id: page.params.lib_id ?? '',
          keyword: query.keyword
        }
      })
      .json<Resp<Page<MediaItem>>>()
      .then(async ({ data }) => {
        const progress = await loadMediaProgress(data.items.map((item) => item.id));
        attachMediaProgress(data.items, progress);
        items = data.items;
        pagination.total = data.total;
      })
      .catch((error) => {
        if (error.name === 'AbortError') {
          aborted = true;
        } else {
          items = [];
          pagination.total = 0;
        }
      })
      .finally(() => {
        if (!aborted) {
          innerLoading.end();
          outerLoading.end();
          tick().then(() => {
            restorePosition(standaloneMode.current ? view : window, toTop);
          });
        }
      });
  }

  let _libId: string | null = null;
  $effect(() => {
    if (_libId !== libId) {
      untrack(() => {
        // check if the library ID is valid
        if (!data.menus[0]?.routes.some((r) => r.path === `/medialibs/${libId}`)) {
          const route = data.menus[0]?.routes[0]?.path;
          if (!route) {
            setTimeout(() => {
              const _subroutes = { ...(get(subroutes) ?? {}) };
              delete _subroutes['/medialibs'];
              subroutes.set(compactRecord(_subroutes));
            });
          } else {
            goto(route, { replaceState: true });
          }
          return;
        }
        outerLoading.start();
        // initialize query parameters
        const params = page.url.searchParams;
        query.keyword = params.get('keyword') || '';
        query.page_num = Number(params.get('page_num')) || 1;
        query.page_size = Number(params.get('page_size')) || 20;
        search();
        _libId = libId;
      });
    }
  });

  function progressLabel(item: MediaItem): string {
    if (!item.progress) {
      return '';
    }
    return isWatched(item.progress) ? $_('media.progress.watched') : $_('media.progress.watching');
  }
</script>

<Container class="pull-to-refresh" loading={$outerLoading}>
  <DataView
    bind:this={view}
    mode="grid"
    data={items}
    loading={$innerLoading}
    hideOnScroll={standaloneMode.current}
    filtersClass="sm:justify-center"
    gridClass="grid-cols-2 grid-cols-compact"
    itemClass="mb-4 z-1"
  >
    {#snippet filters()}
      <Search
        label={$_('field.keyword')}
        bind:value={query.keyword}
        onsearch={() => {
          query.page_num = 1;
          search(true);
        }}
        maxWidth="36rem"
      />
    {/snippet}

    <!-- grid view -->
    {#snippet item(item)}
      {@const pointerClass = 'cursor-pointer transition-colors hover:text-primary'}
      {@const transClass = 'transition-all duration-300'}
      {@const btnClass = 'text-white hover:bg-base-300 hover:text-base-content'}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <div
        tabindex="0"
        role="button"
        class="group @container relative cursor-pointer"
        onmouseenter={() => (backdrop = item.backdrop ?? item.poster ?? backdrop)}
        onclick={() => goto(`${page.url.pathname}/${item.id}`)}
      >
        <Rating score={item.rating} class="absolute top-1 left-1 z-1 text-[clamp(0.875rem,8cqw,1rem)]" />
        {#if hasProgress(item.progress)}
          <span class="badge absolute top-8 left-1 z-1 border-0 bg-base-100/85 text-xs shadow-sm backdrop-blur-sm">
            {progressLabel(item)}
          </span>
        {/if}
        <div
          class:hidden={$user?.role !== 'admin'}
          class="absolute right-0 bottom-0 z-1 p-1 opacity-0 group-hover:opacity-100 {transClass}"
        >
          <MediaActions
            {item}
            class="dropdown-left dropdown-end"
            triggerClass={btnClass}
            onscrape={() => search()}
            ondelete={() => search()}
          />
        </div>
        <Image
          proxy="store"
          src={item.poster}
          width="100%"
          ratio="2/3"
          class="shadow-sm group-hover:brightness-60 hover:shadow-lg {transClass}"
        />
      </div>
      <div class="@container flex-col-center gap-1 p-1">
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <div
          tabindex="0"
          role="button"
          class="line-clamp-1 text-[clamp(0.875rem,8cqw,1rem)] font-semibold {pointerClass}"
          title={item.title ?? item.name}
          onclick={() => goto(`${page.url.pathname}/${item.id}`)}
        >
          {item.title ?? item.name}
        </div>
        <div class="text-sm opacity-50">{item.year}</div>
      </div>
    {/snippet}

    {#snippet paginator(disabled)}
      <Paginator {disabled} {...pagination} bind:current={query.page_num} size={query.page_size} />
    {/snippet}
  </DataView>
</Container>

<Backdrop proxy="store" src={backdrop} />
