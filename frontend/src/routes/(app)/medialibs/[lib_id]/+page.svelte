<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { page } from '$app/state';
  import { tooltip } from '$lib/actions';
  import { api } from '$lib/api';
  import { registerAssistantContext } from '$lib/assistant/context';
  import {
    alert,
    Backdrop,
    BatchPlanDialog,
    Container,
    DataView,
    Image,
    MediaActions,
    Paginator,
    Rating,
    Search,
    type PaginatorProps
  } from '$lib/components';
  import { LibType } from '$lib/enums';
  import { createLoading } from '$lib/helpers';
  import { _, number } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { captureScrollPosition, restorePosition, subroutes, user } from '$lib/stores';
  import type { MediaBatchOutcome, MediaItem, Page, Resp } from '$lib/types';
  import { compactRecord } from '$lib/utils/object';
  import { onMount, tick, untrack } from 'svelte';
  import { MediaQuery } from 'svelte/reactivity';
  import { get } from 'svelte/store';
  import { queryParameters, ssp } from 'sveltekit-search-params';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  // the library ID
  let libId: string = $derived(page.params.lib_id ?? '');
  const lib = $derived(data.libs.find((lib) => lib.id === Number(libId)));

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
  const reviewLoading = createLoading();

  // items picked for one reviewed batch, on the shown page only
  let selecting = $state(false);
  let selected: number[] = $state([]);
  let planDialog: BatchPlanDialog | undefined = $state();

  // the abort controller
  let abortController: AbortController | null = null;

  // the standalone display mode media query
  const standaloneMode = new MediaQuery('(display-mode: standalone)');

  // capture the scroll position of the current page
  beforeNavigate(({ from, to }) => {
    captureScrollPosition(from, to, view, standaloneMode.current);
  });

  // show an assistant rescrape in this library's list at once
  onMount(() => {
    const changed = (event: Event) => {
      if ((event as CustomEvent<{ library_id?: number }>).detail?.library_id === lib?.id) search();
    };
    window.addEventListener('media-item-changed', changed);
    return () => window.removeEventListener('media-item-changed', changed);
  });

  // read the library and list filters when a message is sent, since this page is reused across libraries
  onMount(() =>
    registerAssistantContext(() =>
      lib
        ? {
            page_type: 'media_library',
            route: page.url.pathname.replace(/\/$/, ''),
            resource_id: lib.id,
            filters: {
              keyword: query.keyword.slice(0, 255),
              page_num: Math.max(1, query.page_num || 1),
              page_size: Math.min(100, Math.max(1, query.page_size || 20))
            }
          }
        : null
    )
  );

  /**
   * Open an item, or pick it while items are being selected.
   *
   * @param item - Item that was clicked.
   */
  function open(item: MediaItem) {
    if (!selecting) {
      goto(`${page.url.pathname}/${item.id}`);
    } else if (selected.includes(item.id)) {
      selected = selected.filter((id) => id !== item.id);
    } else {
      selected = [...selected, item.id];
    }
  }

  /**
   * Activate a focused poster without handling keys from its nested controls.
   *
   * @param event - Keyboard event from the poster or one of its descendants.
   * @param item - Item to open or select.
   */
  function activate(event: KeyboardEvent, item: MediaItem) {
    if (event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      if (!event.repeat) {
        open(item);
      }
    }
  }

  /**
   * Start or leave selection without keeping earlier picks.
   */
  function toggleSelecting() {
    selecting = !selecting;
    selected = [];
  }

  /**
   * Review deleting the selected items with their files and show the plan for confirmation.
   */
  function reviewDeletion() {
    if (!lib || selected.length === 0) {
      return;
    }
    reviewLoading.start();
    api
      .post('media/batch/review', { json: { library_id: lib.id, operation: 'delete', item_ids: selected } })
      .json<Resp<MediaBatchOutcome>>()
      .then(({ data }) => {
        if (data.batch_id) {
          planDialog?.showModal(data.batch_id);
        } else {
          alert({ level: 'warning', message: 'media_batch_empty' });
        }
      })
      .finally(() => reviewLoading.end());
  }

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
      .then(({ data }) => {
        items = data.items;
        pagination.total = data.total;
        selected = [];
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
</script>

<Container class="pull-to-refresh" loading={$outerLoading}>
  <DataView
    bind:this={view}
    mode="grid"
    data={items}
    loading={$innerLoading}
    hideOnScroll={standaloneMode.current}
    filtersClass="flex-wrap items-center sm:flex-nowrap sm:justify-center"
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
      {#if $user?.role === 'admin'}
        <div class="contents sm:flex sm:shrink-0 sm:items-center sm:gap-2">
          <button
            type="button"
            class="btn btn-square shrink-0 btn-sm"
            class:btn-active={selecting}
            aria-label={$_(selecting ? 'message.cancel' : 'media.batch.select')}
            aria-pressed={selecting}
            use:tooltip={{ content: $_(selecting ? 'message.cancel' : 'media.batch.select') }}
            onclick={toggleSelecting}
          >
            <iconify-icon icon={selecting ? icons.dismiss : icons.listCheck} width="1rem" aria-hidden="true"
            ></iconify-icon>
          </button>
          {#if selecting}
            <div class="flex w-full items-center justify-between gap-2 sm:w-auto sm:justify-start">
              <span class="text-sm whitespace-nowrap text-base-content/60" role="status">
                {$_('media.batch.selected', $number(selected.length))}
              </span>
              <button
                type="button"
                class="btn shrink-0 btn-outline btn-error btn-sm"
                disabled={selected.length === 0 || $reviewLoading !== null}
                onclick={reviewDeletion}
              >
                <iconify-icon icon={icons.delete} width="1rem" aria-hidden="true"></iconify-icon>
                {$_('media.batch.delete_selected')}
              </button>
            </div>
          {/if}
        </div>
      {/if}
    {/snippet}

    <!-- grid view -->
    {#snippet item(item)}
      {@const pointerClass = 'cursor-pointer transition-colors hover:text-primary'}
      {@const transClass = 'transition-all duration-300'}
      {@const btnClass = 'text-white hover:bg-base-300 hover:text-base-content'}
      <div
        tabindex="0"
        role="button"
        class="group @container relative cursor-pointer focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
        aria-label={item.title ?? item.name}
        aria-pressed={selecting ? selected.includes(item.id) : undefined}
        onmouseenter={() => (backdrop = item.backdrop ?? item.poster ?? backdrop)}
        onclick={() => open(item)}
        onkeydown={(event) => activate(event, item)}
      >
        <Rating score={item.rating} class="absolute top-1 left-1 z-1 text-[clamp(0.875rem,8cqw,1rem)]" />
        {#if selecting}
          <input
            type="checkbox"
            class="checkbox absolute top-1 right-1 z-1 checkbox-sm checkbox-primary not-checked:bg-base-100"
            checked={selected.includes(item.id)}
            tabindex="-1"
            aria-hidden="true"
          />
        {/if}
        <div
          class:hidden={$user?.role !== 'admin' || selecting}
          class="absolute right-0 bottom-0 z-1 p-1 opacity-0 group-hover:opacity-100 {transClass}"
        >
          <MediaActions
            {item}
            danmaku={lib?.lib_type === 'movie' || lib?.lib_type === 'tv_show'}
            class="dropdown-left dropdown-end"
            triggerClass={btnClass}
            onscrape={() => search()}
            ondelete={() => search()}
          />
        </div>
        <Image
          proxy="store"
          src={item.poster}
          icon={lib ? LibType[lib.lib_type].icon : undefined}
          width="100%"
          ratio="2/3"
          class="shadow-sm group-hover:brightness-60 hover:shadow-lg {transClass}"
        />
      </div>
      <div class="@container flex-col-center gap-1 p-1">
        <button
          type="button"
          class="line-clamp-1 text-[clamp(0.875rem,8cqw,1rem)] font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary {pointerClass}"
          title={item.title ?? item.name}
          aria-pressed={selecting ? selected.includes(item.id) : undefined}
          onclick={() => open(item)}
        >
          {item.title ?? item.name}
        </button>
        <div class="text-sm opacity-50">{item.year}</div>
      </div>
    {/snippet}

    {#snippet paginator(disabled)}
      <Paginator {disabled} {...pagination} bind:current={query.page_num} size={query.page_size} />
    {/snippet}
  </DataView>
</Container>

<Backdrop proxy="store" src={backdrop} />

<BatchPlanDialog
  bind:this={planDialog}
  onfinish={() => {
    selecting = false;
    search();
  }}
/>
