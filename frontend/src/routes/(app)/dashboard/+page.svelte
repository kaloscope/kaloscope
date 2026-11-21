<script lang="ts">
  import { registerAssistantContext } from '$lib/assistant/context';
  import { goto } from '$app/navigation';
  import { api } from '$lib/api';
  import {
    Button,
    confirm,
    Container,
    DataView,
    Empty,
    HCell,
    Image,
    SearchHit,
    Skeleton,
    VideoPlayer,
    ViewSwitcher
  } from '$lib/components';
  import { LibType } from '$lib/enums';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { restorePosition, user } from '$lib/stores.svelte';
  import type {
    FlowGraph,
    IndexerAuth,
    IndexerConfig,
    MediaItem,
    ReadingHistory,
    Resource,
    Resp,
    ViewMode,
    ViewModes,
    WatchHistory
  } from '$lib/types';
  import { buildStreamUrl, mediaTitle } from '$lib/utils/media';
  import { aspectRatio } from '$lib/utils/number';
  import { onMount, tick, untrack } from 'svelte';
  import { flip } from 'svelte/animate';
  import { SvelteSet } from 'svelte/reactivity';
  import { fade } from 'svelte/transition';
  import type { PageData } from './$types';

  /**
   * Type of the board resources.
   */
  type Resources = {
    title?: string | null;
    items: Resource[];
  };

  /**
   * Type of the dashboard panel.
   */
  type Board = {
    id: number;
    name: string;
    icon: string | null;
    loading: boolean | null;
    activeId: number | null;
    resources: Resources[];
    coverRatio: string;
    viewModes: ViewModes;
    viewMode: ViewMode;
    config: IndexerConfig;
  };

  /**
   * Type of the history entry.
   */
  type HistoryType = 'video' | 'search' | 'reading';

  /**
   * Type of the search history.
   */
  type SearchHistory = {
    id: number;
    rel_id: number;
    updated_at: string;
    keyword: string;
    repetitions: number;
    graph: FlowGraph | null;
  };

  let { data }: { data: PageData } = $props();

  let boards: Record<number, Board> = $state({});
  let watches: WatchHistory[] = $state([]);
  let latestWatches = $derived.by(() => {
    const mediaIds = new SvelteSet<number>();
    // histories arrive newest first; keep the latest record for each parent
    return watches.filter(({ media }) => {
      if (!media) {
        return false;
      }
      const parentId = media.parent?.id ?? 0;
      const mediaId = parentId > 0 ? parentId : media.id;
      if (mediaIds.has(mediaId)) {
        return false;
      }
      mediaIds.add(mediaId);
      return true;
    });
  });
  let searches: SearchHistory[] = $state([]);
  let reads: ReadingHistory[] = $state([]);
  let loadWatches = $derived(user.current?.preferences?.recent_watches ?? false);
  let loadSearches = $derived(user.current?.preferences?.recent_searches ?? false);
  let loadReads = $derived(
    (user.current?.preferences?.recent_reads ?? false) && user.current?.preferences?.read_records !== 0
  );
  let showWatches = $derived(loadWatches && latestWatches.length > 0);
  let showSearches = $derived(loadSearches && searches.length > 0);
  let showReads = $derived(loadReads && reads.length > 0);
  let historyCount = $derived([showSearches, showWatches, showReads].filter(Boolean).length);
  // the history requests still in flight, so the empty dashboard message waits for them
  let historyPending: number = $state(0);

  // the placeholder cards shown while a board loads
  const BOARD_SKELETONS = 7;
  // the number of board workflows run at the same time
  const BOARD_CONCURRENCY = 3;
  // the graphs whose boards are being prepared, and whether every board has settled
  const preparing = new SvelteSet<number>();
  let settled: boolean = $state(false);
  // whether a board is still loading before it has a section of its own
  let pendingBoard = $derived([...preparing].some((id) => !boards[id]));

  // the player instance and playing state
  let player: VideoPlayer | null = $state(null);
  let playing = $state(false);

  /**
   * Load board resources for a given flow graph.
   *
   * @param graph - Flow graph instance.
   */
  async function load(graph: FlowGraph) {
    const { id, name, icon } = graph;
    const reload = !!boards[id];
    let board = reload ? boards[id] : ({} as Board);
    try {
      if (!reload) {
        // load the indexer config
        const config = (await api.get(`flow/indexer/${id}/config`).json<Resp<IndexerConfig>>()).data;
        const { login } = config.auth ?? {};
        const { display } = config.board ?? {};

        // check if login is required
        if (login?.required) {
          const resp = await api.get(`flow/indexer/${id}/auth`).json<Resp<IndexerAuth>>();
          if (!resp.data || resp.data.name === null || resp.data.name === undefined) {
            return;
          }
        }

        // create a new board object
        const modes = display?.view_modes ?? [];
        const viewModes = (Array.isArray(modes) && modes.length > 0 ? modes : ['grid']) as ViewModes;
        board = {
          id: id,
          name: name,
          icon: icon,
          loading: null,
          activeId: 0,
          resources: [],
          coverRatio: display?.cover_ratio ?? '2/3',
          viewModes: viewModes,
          viewMode: viewModes[0],
          config: config
        };
        boards[id] = board;
      }

      // set the loading state
      boards[id].loading = false;
      setTimeout(() => {
        if (boards[id] && boards[id].loading === false) {
          boards[id].loading = true;
        }
      }, 500);

      // load the resources
      const resp = await api
        .post(`flow/graph/${id}/execute`, {
          json: { $start: 'board_start' }
        })
        .json<Resp<Resources[]>>();
      board.resources = resp.data || [];

      // set the active tab based on the calendar config
      const { calendar } = board.config.board ?? {};
      if (calendar?.week && board.resources.length === 7) {
        const weekStart = calendar.week_start ?? 0;
        board.activeId = (new Date().getDay() - weekStart + 7) % 7;
      }
    } catch (error) {
      console.error(error);
      board.resources = [];
    } finally {
      if (boards[id]) {
        boards[id] = board;
        boards[id].loading = null;
      }
    }
  }

  // the colors for the week tabs
  const WEEKDAY_COLORS = ['#ef4444', '#f97316', '#eab308', '#84cc16', '#22c55e', '#0ea5e9', '#2563eb'];

  /**
   * Get metadata for a week tab.
   *
   * @param today - Current date.
   * @param index - Index of the tab.
   * @param weekStart - Week start day.
   * @returns The metadata for the week tab.
   */
  function getWeekMeta(today: Date, index: number, weekStart: number | null | undefined) {
    const todayIndex = (today.getDay() - (weekStart ?? 0) + 7) % 7;
    const date = new Date(today.getFullYear(), today.getMonth(), today.getDate() + index - todayIndex);

    return {
      label: `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')}`,
      color: WEEKDAY_COLORS[date.getDay()]
    };
  }

  /**
   * Load user histories if enabled in preferences.
   */
  async function loadHistories() {
    if (loadWatches) {
      try {
        const resp = await api
          .get('user/history/list', { searchParams: { rel_type: 'video', page_num: 0, ordering: '-updated_at' } })
          .json<Resp<{ items: WatchHistory[] }>>();
        watches = resp.data.items.filter((w) => w.media);
      } catch {
        watches = [];
      }
    }
    if (loadSearches) {
      try {
        const resp = await api
          .get('user/history/list', { searchParams: { rel_type: 'search', page_num: 0, ordering: '-repetitions' } })
          .json<Resp<{ items: SearchHistory[] }>>();
        searches = resp.data.items;
      } catch {
        searches = [];
      }
    }
  }

  /**
   * Load novels and comics together, retaining successful results if one type fails.
   *
   * @param signal - Cancellation when leaving the dashboard or changing preferences or accounts.
   */
  async function loadReadingHistory(signal: AbortSignal) {
    const results = await Promise.allSettled(
      ['text', 'image'].map((rel_type) =>
        api
          .get('user/history/list', {
            searchParams: { rel_type, page_num: 0, ordering: '-updated_at' },
            signal
          })
          .json<Resp<{ items: ReadingHistory[] }>>()
      )
    );
    if (signal.aborted) return;
    reads = results
      .flatMap((result) => (result.status === 'fulfilled' ? result.value.data.items : []))
      .filter((item) => item.media?.lib)
      .sort((a, b) => new Date(b.updated_at ?? 0).getTime() - new Date(a.updated_at ?? 0).getTime() || b.id - a.id);
  }

  /**
   * Delete a history entry by ID and type.
   *
   * @param id - The ID of the history entry to delete.
   * @param type - Type of the history entry.
   */
  function deleteHistory(id: number, type: HistoryType) {
    api.post('user/history/delete', { json: { ids: [id] } }).then(() => {
      if (type === 'video') {
        watches = watches.filter((w) => w.id !== id);
      } else if (type === 'search') {
        searches = searches.filter((s) => s.id !== id);
      } else if (type === 'reading') {
        reads = reads.filter((r) => r.id !== id);
      }
    });
  }

  /**
   * Delete all history entries of a given type with confirmation.
   *
   * @param type - Type of the history entries to delete.
   */
  function deleteAllHistories(type: HistoryType) {
    confirm({
      icon: icons.clear,
      title: $_('message.clear.title'),
      message: $_('message.clear.content'),
      onconfirm: () => {
        if (type === 'video') {
          const ids = watches.map((w) => w.id);
          api.post('user/history/delete', { json: { ids } }).then(() => (watches = []));
        } else if (type === 'search') {
          const ids = searches.map((s) => s.id);
          api.post('user/history/delete', { json: { ids } }).then(() => (searches = []));
        } else if (type === 'reading') {
          const ids = reads.map((r) => r.id);
          api.post('user/history/delete', { json: { ids } }).then(() => (reads = []));
        }
      }
    });
  }

  /**
   * Play a media item from the watch history in the video player.
   *
   * @param w - Watch history entry.
   */
  function playMedia(w: WatchHistory) {
    const media = w.media;
    if (!media || !media.path) {
      return;
    }
    playing = true;
    tick().then(() => {
      player?.mount({
        url: buildStreamUrl(media.path),
        back: () => (playing = false),
        title: mediaTitle(media),
        startTime: w.position ?? 0,
        danmakuServer: media.lib?.danmaku_server
      });
    });
  }

  /**
   * Build the search page link for the given history entry.
   *
   * @param s - Search history entry.
   * @returns The global or indexer search link, or `undefined` for an entry without a keyword.
   */
  function searchHref(s: SearchHistory): string | undefined {
    if (!s.keyword) {
      return undefined;
    }
    const path = s.graph ? `/websearch/${s.rel_id}` : '/websearch/global';
    return `${path}?restore=false&keyword=${encodeURIComponent(s.keyword)}`;
  }

  /**
   * Normalize an optional history progress percentage into the `0..100` range.
   *
   * @param percentage - Raw percentage from the history entry.
   * @returns Clamped integer percentage, or `null` when unavailable.
   */
  function clampPercentage(percentage: number | null | undefined): number | null {
    if (percentage === null || percentage === undefined || Number.isNaN(percentage)) {
      return null;
    }
    return Math.min(100, Math.max(0, Math.round(percentage)));
  }

  /**
   * Open a reading history entry and resume the saved position on primary click.
   *
   * @param event - Anchor click event.
   * @param media - Reading media item to open.
   */
  function openRead(event: MouseEvent, media: MediaItem) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0 || !media.lib) {
      return;
    }
    event.preventDefault();
    goto(`/medialibs/${media.lib.id}/${media.id}`, { state: { resumeReading: true } });
  }

  /**
   * Toggle scroll-edge fade classes when CSS scroll-driven animations are unavailable.
   *
   * @param node - Scrollable list element.
   * @param count - Reactive item count used to re-observe the edge items.
   * @returns Svelte action lifecycle hooks.
   */
  function scrollFade(node: HTMLElement, count: number) {
    if (typeof CSS !== 'undefined' && CSS.supports('animation-timeline', 'scroll()')) {
      return {};
    }
    let observer: IntersectionObserver | null = null;
    const observe = (itemCount: number) => {
      observer?.disconnect();
      node.classList.remove('more-start', 'more-end');
      if (itemCount <= 1) {
        return;
      }
      const first = node.firstElementChild;
      const last = node.lastElementChild;
      if (!first || !last || first === last) {
        return;
      }
      // rootMargin keeps sub-pixel edge items from reporting as clipped
      observer = new IntersectionObserver(
        (entries) => {
          for (const { target, intersectionRatio } of entries) {
            if (target === first) {
              node.classList.toggle('more-start', intersectionRatio < 1);
            }
            if (target === last) {
              node.classList.toggle('more-end', intersectionRatio < 1);
            }
          }
        },
        { root: node, threshold: 1, rootMargin: '1px' }
      );
      observer.observe(first);
      observer.observe(last);
    };
    observe(count);
    return {
      update(nextCount: number) {
        tick().then(() => observe(nextCount));
      },
      destroy() {
        observer?.disconnect();
      }
    };
  }

  // load the user histories
  $effect(() => {
    if (loadWatches || loadSearches) {
      untrack(() => {
        historyPending++;
        void loadHistories().finally(() => historyPending--);
      });
    }
  });

  // discard reading results from an old account or a disabled dashboard section
  $effect(() => {
    const owner = user.current?.id;
    const enabled = loadReads;
    const controller = new AbortController();
    reads = [];
    if (owner && enabled) {
      untrack(() => {
        historyPending++;
        void loadReadingHistory(controller.signal).finally(() => historyPending--);
      });
    }
    return () => controller.abort();
  });

  /**
   * Load the queued boards one at a time, so a few loaders can share the queue.
   *
   * @param queue - Graphs whose boards have not started loading.
   */
  async function loadQueue(queue: FlowGraph[]) {
    for (let graph = queue.shift(); graph; graph = queue.shift()) {
      preparing.add(graph.id);
      try {
        await load(graph);
      } finally {
        preparing.delete(graph.id);
      }
    }
  }

  // load the board resources a few at a time; boards keep their id order whatever finishes first
  onMount(async () => {
    boards = {};
    const queue = [...data.graphs];
    const loaders = Math.min(BOARD_CONCURRENCY, queue.length);
    await Promise.all(Array.from({ length: loaders }, () => loadQueue(queue)));
    settled = true;
    restorePosition(window);
  });
  onMount(() =>
    registerAssistantContext(() => ({
      page_type: 'dashboard',
      route: '/dashboard',
      watch_ids: showWatches ? latestWatches.slice(0, 8).map((w) => w.media!.id) : [],
      read_ids: showReads
        ? reads
            .filter((r) => r.media)
            .slice(0, 8)
            .map((r) => r.media!.id)
        : []
    }))
  );
</script>

<Container padding="1.25rem 0.75rem 2rem" rowGap="2.5rem" maxWidth="max(150vh,72rem)">
  <!-- recent histories -->
  {#if historyCount > 0}
    <div
      class="grid gap-x-6 gap-y-8 {historyCount > 1 ? 'lg:grid-cols-2' : ''} {historyCount === 3
        ? 'xl:grid-cols-3'
        : ''}"
      transition:fade={{ duration: 200 }}
    >
      <!-- recent searches -->
      {#if showSearches}
        <section class="min-w-0">
          {@render historyHead($_('preference.dashboard.search'), searches.length, 'search')}
          <ul class="history-list max-sm:scrollbar-none" use:scrollFade={searches.length}>
            {#each searches as item (item.id)}
              <li class="history-slot" animate:flip={{ duration: 200 }}>
                <article class="history-card search-card card-surface">
                  <span
                    class="search-tile flex-center icon-tile"
                    style:--tone={item.graph?.icon ? undefined : 'var(--color-primary)'}
                    aria-hidden="true"
                  >
                    {#if item.graph?.icon}
                      <Image transparent src={item.graph.icon} width="1.375rem" />
                    {:else}
                      <iconify-icon icon={icons.search} width="1.125rem" height="1.125rem" class="history-inline-icon"
                      ></iconify-icon>
                    {/if}
                  </span>
                  <div class="history-body">
                    <h3 class="history-title">
                      <a href={searchHref(item)} class="card-link" title={item.keyword}>
                        <span class="block truncate">{item.keyword}</span>
                      </a>
                    </h3>
                    <div class="history-meta">
                      <span class="history-meta-label">{item.graph?.name ?? $_('nav.websearch.global.search')}</span>
                      <span class="history-dot" aria-hidden="true">·</span>
                      <span class="history-time">{$dateTime(item.updated_at)}</span>
                    </div>
                  </div>
                  {@render historyDelete(item.id, 'search')}
                </article>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      <!-- recent watches -->
      {#if showWatches}
        <section class="min-w-0">
          {@render historyHead($_('preference.dashboard.watch'), latestWatches.length, 'video')}
          <ul class="history-list max-sm:scrollbar-none" use:scrollFade={latestWatches.length}>
            {#each latestWatches as item (item.id)}
              {@const media = item.media}
              {@const parent = media?.parent}
              {@const libType = media?.lib ? LibType[media.lib.lib_type] : null}
              {@const badgeLabel = media?.lib?.name || (libType ? $_(libType.label) : '')}
              {@const poster = parent?.poster ?? media?.poster}
              {@const watchTitle = parent?.title ?? parent?.name ?? mediaTitle(media)}
              {@const pct = clampPercentage(item.percentage)}
              <li class="history-slot" animate:flip={{ duration: 200 }}>
                <article class="history-card media-card card-surface">
                  <div class="media-cover" aria-hidden="true">
                    {#if poster}
                      <Image
                        proxy="store"
                        src={poster}
                        icon={libType?.icon}
                        width="100%"
                        ratio="2/3"
                        class="cover-image size-full! rounded-none!"
                      />
                    {:else}
                      <div class="cover-fallback">
                        {#if libType?.icon}
                          <iconify-icon icon={libType.icon} width="1.5rem" height="1.5rem" class="history-inline-icon"
                          ></iconify-icon>
                        {/if}
                      </div>
                    {/if}
                  </div>
                  <div class="media-body">
                    <div class="history-top">
                      {#if libType || badgeLabel}
                        <span class="history-badge">
                          {#if libType?.icon}
                            <iconify-icon
                              icon={libType.icon}
                              width="0.75rem"
                              height="0.75rem"
                              class="history-inline-icon"
                              aria-hidden="true"
                            ></iconify-icon>
                          {/if}
                          {#if badgeLabel}
                            <span class="history-badge-text trim-cap">{badgeLabel}</span>
                          {/if}
                        </span>
                      {/if}
                    </div>
                    <h3 class="history-title media-title">
                      <button
                        type="button"
                        class="card-link"
                        onclick={() => playMedia(item)}
                        aria-label="{$_('media.continue_play')} {watchTitle}"
                        title={watchTitle}
                      >
                        <span class="block truncate">{watchTitle}</span>
                      </button>
                    </h3>
                    <div class="history-meta">
                      {#if parent}
                        <span class="history-meta-label" title={mediaTitle(media)}>{mediaTitle(media)}</span>
                        <span class="history-dot" aria-hidden="true">·</span>
                      {/if}
                      <span class="history-time">{$dateTime(item.updated_at)}</span>
                    </div>
                    {@render historyProgress(pct, $_('media.continue_play'))}
                  </div>
                  {@render historyDelete(item.id, 'video')}
                </article>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      <!-- recent reads -->
      {#if showReads}
        <section class="min-w-0 {historyCount === 3 ? 'lg:col-span-2 xl:col-span-1' : ''}">
          {@render historyHead($_('preference.dashboard.read'), reads.length, 'reading')}
          <ul class="history-list max-sm:scrollbar-none" use:scrollFade={reads.length}>
            {#each reads as item (item.id)}
              {@const media = item.media!}
              {@const libType = media.lib ? LibType[media.lib.lib_type] : null}
              {@const badgeLabel = media.lib?.name || (libType ? $_(libType.label) : '')}
              {@const pct = clampPercentage(item.percentage)}
              <li class="history-slot" animate:flip={{ duration: 200 }}>
                <article class="history-card media-card card-surface">
                  <div class="media-cover read-cover" aria-hidden="true">
                    {#if media.poster}
                      <Image
                        proxy="store"
                        src={media.poster}
                        icon={libType?.icon}
                        width="100%"
                        ratio="2/3"
                        class="cover-image size-full! rounded-none!"
                      />
                    {:else}
                      <div class="cover-fallback">
                        {#if libType?.icon}
                          <iconify-icon icon={libType.icon} width="1.5rem" height="1.5rem" class="history-inline-icon"
                          ></iconify-icon>
                        {/if}
                      </div>
                    {/if}
                  </div>
                  <div class="media-body">
                    <div class="history-top">
                      {#if libType || badgeLabel}
                        <span class="history-badge">
                          {#if libType?.icon}
                            <iconify-icon
                              icon={libType.icon}
                              width="0.75rem"
                              height="0.75rem"
                              class="history-inline-icon"
                              aria-hidden="true"
                            ></iconify-icon>
                          {/if}
                          {#if badgeLabel}
                            <span class="history-badge-text trim-cap">{badgeLabel}</span>
                          {/if}
                        </span>
                      {/if}
                    </div>
                    <h3 class="history-title media-title">
                      <a
                        href="/medialibs/{media.lib!.id}/{media.id}"
                        class="card-link"
                        onclick={(event) => openRead(event, media)}
                        aria-label="{$_('media.continue_read')} {mediaTitle(media)}"
                        title={mediaTitle(media)}
                      >
                        <span class="block truncate">{mediaTitle(media)}</span>
                      </a>
                    </h3>
                    <div class="history-meta">
                      <span class="history-time">{$dateTime(item.updated_at)}</span>
                    </div>
                    {@render historyProgress(pct, $_('media.continue_read'))}
                  </div>
                  {@render historyDelete(item.id, 'reading')}
                </article>
              </li>
            {/each}
          </ul>
        </section>
      {/if}
    </div>
  {/if}

  <!-- dashboard panels -->
  {#each Object.values(boards) as board (board.id)}
    {@const gridClass = `grid-cols-2 ${aspectRatio(board.coverRatio) > 1 ? 'grid-cols-sparse' : 'grid-cols-medium'}`}
    <section class="min-w-0" transition:fade={{ duration: 200 }}>
      <div class="flex h-8 items-center justify-between gap-2 px-2">
        <h2 class="section-title">
          {#if board.icon}
            <Image transparent src={board.icon} width="1.375rem" />
          {/if}
          <span class="truncate">{board.name}</span>
        </h2>
        {#if board.loading}
          <span class="loading mx-1.5 loading-sm loading-spinner opacity-60"></span>
        {:else if board.resources.length > 0}
          <ViewSwitcher modes={board.viewModes} bind:mode={board.viewMode} />
        {/if}
      </div>
      {#if board.resources.length > 0}
        {@const weekMode = !!board.config.board?.calendar?.week && board.resources.length === 7}
        <div class="board-tabs tabs-border tabs" transition:fade>
          {#each board.resources as rsrcs, index (index)}
            {@const week = weekMode ? getWeekMeta(new Date(), index, board.config.board?.calendar?.week_start) : null}
            <label
              class="tab {board.resources.length === 1 ? 'hidden' : ''}"
              class:week-tab={weekMode}
              style:color={board.activeId === index ? week?.color : undefined}
            >
              <input type="radio" checked={board.activeId === index} onclick={() => (board.activeId = index)} />
              <span
                class="font-semibold {board.activeId === index ? 'text-shadow-xs' : ''}"
                class:week-tab-label={weekMode}
              >
                {rsrcs.title}
                {#if week}
                  <small class="week-tab-date">{week.label}</small>
                {/if}
              </span>
            </label>
            {#if board.activeId === index}
              <div class="tab-content" transition:fade>
                <DataView
                  data={rsrcs.items}
                  mode={board.viewMode}
                  loading={board.loading}
                  class="px-0!"
                  tableClass="[&_thead_tr]:border-0"
                  {gridClass}
                  itemClass="card-surface card-hover overflow-hidden"
                  skeleton={{ ratio: board.coverRatio }}
                >
                  <!-- table view -->
                  {#snippet header()}
                    <HCell width="100%" />
                    <HCell width="3rem" />
                  {/snippet}
                  {#snippet row(rsrc)}
                    <SearchHit
                      mode="table"
                      {rsrc}
                      searchButton
                      indexerId={board.id}
                      indexerConfig={board.config}
                      coverRatio={board.coverRatio}
                    />
                  {/snippet}

                  <!-- grid view -->
                  {#snippet item(rsrc)}
                    <SearchHit
                      mode="grid"
                      {rsrc}
                      searchButton
                      indexerId={board.id}
                      indexerConfig={board.config}
                      coverRatio={board.coverRatio}
                    />
                  {/snippet}
                </DataView>
              </div>
            {/if}
          {/each}
        </div>
      {:else if board.loading !== null}
        <!-- stay invisible during the short grace period to avoid flashing -->
        <ul
          class="grid gap-2 px-2 py-4 transition-opacity duration-300 lg:gap-4 {gridClass} {board.loading
            ? ''
            : 'opacity-0'}"
          aria-busy="true"
        >
          <Skeleton count={BOARD_SKELETONS} ratio={board.coverRatio} />
        </ul>
      {:else}
        <Empty compact />
      {/if}
    </section>
  {/each}

  <!-- the board being prepared -->
  {#if pendingBoard}
    <section class="min-w-0 fade-in-late" aria-busy="true">
      <span class="sr-only" role="status">{$_('data.loading')}</span>
      <div class="flex h-8 items-center px-2">
        <Skeleton type="block" class="h-5 w-40" />
      </div>
      <ul class="grid grid-cols-medium grid-cols-2 gap-2 px-2 py-4 lg:gap-4">
        <Skeleton count={BOARD_SKELETONS} ratio="2/3" />
      </ul>
    </section>
  {/if}

  <!-- nothing to show -->
  {#if settled && historyPending === 0 && historyCount === 0 && Object.keys(boards).length === 0}
    <div class="flex-center min-h-[60vh]">
      <Empty title={$_('empty.boards.title')} description={$_('empty.boards.tip')}>
        {#if user.current?.role === 'admin'}
          <a href="/settings/workflows/templates" class="empty-action">
            <iconify-icon icon={icons.flowchart} width="1rem" aria-hidden="true"></iconify-icon>
            {$_('empty.browse_templates')}
          </a>
        {/if}
      </Empty>
    </div>
  {/if}
</Container>

{#snippet historyHead(title: string, count: number, type: HistoryType)}
  <div class="mb-1.5 flex h-8 items-center justify-between gap-2 px-2">
    <h2 class="section-title">
      <span class="truncate">{title}</span>
      <span class="history-count" aria-hidden="true"><span class="trim-cap">{count}</span></span>
    </h2>
    <Button
      icon={icons.clear}
      text={$_('action.clear', title)}
      iconClass="opacity-70"
      onclick={() => deleteAllHistories(type)}
    />
  </div>
{/snippet}

{#snippet historyDelete(id: number, type: HistoryType)}
  <button
    type="button"
    class="history-delete-button"
    aria-label={$_('action.delete')}
    onclick={() => deleteHistory(id, type)}
  >
    <iconify-icon icon={icons.dismiss} width="1rem" height="1rem" class="history-inline-icon" aria-hidden="true"
    ></iconify-icon>
  </button>
{/snippet}

{#snippet historyProgress(percentage: number | null, resumeLabel: string)}
  {#if percentage !== null}
    <div class="history-progress" class:history-progress-zero={percentage === 0}>
      <div class="history-progress-track" aria-hidden="true">
        <div class="history-progress-fill" style:width="{percentage}%"></div>
      </div>
      <span class="history-progress-value">
        {#if percentage === 100}
          <iconify-icon
            icon={icons.checkmarkCircle}
            width="0.75rem"
            height="0.75rem"
            class="history-inline-icon"
            aria-hidden="true"
          ></iconify-icon>
        {/if}
        <span class="trim-cap">{percentage}%</span>
      </span>
    </div>
  {:else}
    <div class="history-resume" aria-hidden="true">
      <span class="trim-cap">{resumeLabel}</span>
      <iconify-icon
        icon={icons.chevronRight}
        width="0.75rem"
        height="0.75rem"
        class="history-inline-icon history-resume-arrow"
      ></iconify-icon>
    </div>
  {/if}
{/snippet}

<!-- player overlay -->
{#if playing}
  <div class="fixed inset-0 layer-1 max-sm:bottom-(--ks-dock-h)">
    <VideoPlayer bind:this={player} />
  </div>
{/if}

<style>
  .week-tab {
    height: 3rem;
    padding-inline: 0.875rem;
    transition: color 200ms ease;
  }

  .week-tab-label {
    display: flex;
    min-width: 3rem;
    flex-direction: column;
    align-items: center;
    gap: 0.2rem;
    line-height: 1;
  }

  .week-tab-date {
    min-width: 2.75rem;
    font-size: 0.6875rem;
    font-variant-numeric: tabular-nums;
    font-weight: 500;
    letter-spacing: 0.04em;
    opacity: 0.65;
  }

  .board-tabs {
    --tab-border-color: color-mix(in oklab, var(--color-base-content) 10%, transparent);
    margin-top: 0.25rem;
  }

  .history-count {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    min-inline-size: 1.375rem;
    block-size: 1.25rem;
    padding-inline: 0.4375rem;
    border-radius: 9999px;
    background-color: color-mix(in oklab, var(--color-base-content) 7%, transparent);
    color: color-mix(in oklab, var(--color-base-content) 55%, transparent);
    font-size: 0.75rem;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }

  .history-list {
    --fade-size: 1.5rem;
    --fade-dir: right;
    --fade-start-pos: 0;
    --fade-end-pos: 100%;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 17.5rem), 1fr));
    align-items: stretch;
    gap: 0.625rem;
    padding: 0.375rem 0.5rem 0.625rem;
    max-block-size: 22.5rem;
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
    scrollbar-width: thin;
    scroll-padding-block: var(--fade-size);
    mask:
      linear-gradient(black, black) no-clip subtract,
      linear-gradient(to bottom, black, transparent) 0 0 / 100% var(--ks-fade-start) no-repeat no-clip,
      linear-gradient(to bottom, transparent, black) 0 100% / 100% var(--ks-fade-end) no-repeat no-clip;
    transition:
      --ks-fade-start 200ms ease,
      --ks-fade-end 200ms ease;

    &:global(.more-start) {
      --ks-fade-start: var(--fade-size);
    }

    &:global(.more-end) {
      --ks-fade-end: var(--fade-size);
    }
  }

  @supports (animation-timeline: scroll()) {
    .history-list {
      transition: none;
      animation:
        reveal-history-fade-start linear both,
        hide-history-fade-end linear both;
      animation-timeline: scroll(self block);
      animation-range:
        0 var(--fade-size),
        calc(100% - var(--fade-size)) 100%;
    }
  }

  @keyframes reveal-history-fade-start {
    to {
      --ks-fade-start: var(--fade-size);
    }
  }

  @keyframes hide-history-fade-end {
    from {
      --ks-fade-end: var(--fade-size);
    }
  }

  .history-slot {
    min-inline-size: 0;
    container-type: inline-size;
  }

  .history-card {
    position: relative;
    display: flex;
    inline-size: 100%;
    block-size: 100%;
    min-inline-size: 0;
    align-items: center;
    gap: 0.75rem;
    padding: 0.625rem 0.75rem;
    overflow: clip;
    transition:
      box-shadow 200ms ease,
      translate 200ms ease,
      scale 150ms ease,
      background-color 200ms ease;

    &:has(.card-link:focus-visible) {
      outline: 2px solid var(--color-primary);
      outline-offset: 2px;
    }

    &:hover {
      box-shadow:
        0 0 0 1px color-mix(in oklab, var(--color-base-content) 10%, transparent),
        0 10px 22px -12px color-mix(in oklab, var(--color-shade) 22%, transparent);

      .history-title {
        color: var(--color-primary);
      }

      .history-resume {
        color: var(--color-primary);
      }

      .history-resume-arrow {
        translate: 2px 0;
      }

      .history-delete-button {
        opacity: 0.7;
      }
    }

    &:focus-within .history-delete-button {
      opacity: 0.7;
    }
  }

  @media (hover: hover) and (prefers-reduced-motion: no-preference) {
    .history-card:hover {
      translate: 0 -1px;

      :global(.cover-image) {
        transform: scale(1.05);
      }
    }
  }

  @media (prefers-reduced-motion: no-preference) {
    .history-card:has(.card-link:active) {
      scale: 0.99;
    }
  }

  .search-card {
    min-block-size: 4.375rem;
    padding-block: 0.75rem;
  }

  .media-card {
    align-items: stretch;
    gap: 0.875rem;
    padding: 0.75rem;
  }

  .search-tile {
    flex-shrink: 0;
    inline-size: 2.5rem;
    block-size: 2.5rem;
  }

  .media-cover {
    position: relative;
    flex-shrink: 0;
    align-self: center;
    inline-size: 3.5rem;
    aspect-ratio: 2 / 3;
    overflow: clip;
    border-radius: min(var(--radius-field), 0.5rem);
    box-shadow:
      0 0 0 1px color-mix(in oklab, var(--color-base-content) 10%, transparent),
      0 4px 10px -4px color-mix(in oklab, var(--color-shade) 20%, transparent);

    :global(.cover-image) {
      transition: transform 280ms cubic-bezier(0.22, 1, 0.36, 1);
    }
  }

  .read-cover {
    /* subtle book-spine lighting sheen along the leading edge */
    &::after {
      content: '';
      position: absolute;
      inset: 0;
      pointer-events: none;
      border-radius: inherit;
      background: linear-gradient(
        90deg,
        oklch(100% 0 0 / 18%) 0%,
        oklch(0% 0 0 / 10%) 7%,
        oklch(100% 0 0 / 6%) 11%,
        transparent 18%
      );
      box-shadow: 0 0 0 1px color-mix(in oklab, var(--color-base-content) 10%, transparent) inset;
    }
  }

  .cover-fallback {
    display: flex;
    align-items: center;
    justify-content: center;
    inline-size: 100%;
    block-size: 100%;
    background: linear-gradient(
      145deg,
      color-mix(in oklab, var(--color-base-content) 4%, var(--color-base-100)),
      color-mix(in oklab, var(--color-base-content) 10%, var(--color-base-100))
    );
    color: color-mix(in oklab, var(--color-base-content) 38%, transparent);
  }

  .history-body,
  .media-body {
    display: flex;
    min-inline-size: 0;
    flex: 1;
    flex-direction: column;
    justify-content: center;
    gap: 0.25rem;
  }

  .media-body {
    justify-content: space-between;
    gap: 0.3125rem;
  }

  .history-top {
    display: flex;
    min-inline-size: 0;
    align-items: center;
    gap: 0.375rem;
    padding-inline-end: 1.75rem;
  }

  .history-badge {
    display: inline-flex;
    min-inline-size: 0;
    max-inline-size: 100%;
    block-size: 1.25rem;
    align-items: center;
    gap: 0.25rem;
    padding-inline: 0.4375rem;
    border-radius: 9999px;
    background-color: color-mix(in oklab, var(--color-base-content) 6%, transparent);
    color: color-mix(in oklab, var(--color-base-content) 64%, transparent);
    font-size: 0.6875rem;
    font-weight: 500;
    line-height: 1;
  }

  .history-badge-text {
    min-inline-size: 0;
    overflow-x: clip;
    overflow-y: visible;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .history-title {
    min-inline-size: 0;
    padding-inline-end: 1.75rem;
    font-size: 0.875rem;
    font-weight: 600;
    line-height: 1.35;
    transition: color 200ms ease;
  }

  .media-title {
    padding-inline-end: 0;
  }

  .card-link {
    display: block;
    inline-size: 100%;
    max-inline-size: 100%;
    padding: 0;
    border: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: start;
    cursor: pointer;

    &::after {
      content: '';
      position: absolute;
      inset: 0;
      z-index: 1;
    }

    &:focus-visible {
      outline: none;
    }
  }

  .history-meta {
    display: flex;
    min-inline-size: 0;
    align-items: baseline;
    gap: 0.375rem;
    color: color-mix(in oklab, var(--color-base-content) 60%, transparent);
    font-size: 0.75rem;
    line-height: 1.35;
  }

  .history-meta-label {
    min-inline-size: 0;
    overflow: clip;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .history-dot {
    flex-shrink: 0;
    color: color-mix(in oklab, var(--color-base-content) 30%, transparent);
  }

  .history-time {
    flex-shrink: 0;
    color: color-mix(in oklab, var(--color-base-content) 43%, transparent);
    font-size: 0.6875rem;
    font-variant-numeric: tabular-nums;
  }

  .history-progress {
    display: flex;
    min-block-size: 0.75rem;
    align-items: center;
    gap: 0.5rem;
    margin-block-start: 0.125rem;
  }

  .history-progress-track {
    flex: 1;
    block-size: 0.25rem;
    overflow: clip;
    border-radius: 9999px;
    background-color: color-mix(in oklab, var(--color-base-content) 8%, transparent);
  }

  .history-progress-fill {
    block-size: 100%;
    border-radius: 9999px;
    background-color: color-mix(in oklab, var(--color-primary) 62%, transparent);
    transition: width 300ms ease;
  }

  .history-progress-value {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    gap: 0.25rem;
    color: color-mix(in oklab, var(--color-base-content) 54%, transparent);
    font-size: 0.6875rem;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }

  .history-inline-icon {
    display: block;
    flex-shrink: 0;
  }

  .trim-cap {
    text-box: trim-both cap alphabetic;
  }

  .history-progress-zero .history-progress-value {
    color: color-mix(in oklab, var(--color-base-content) 40%, transparent);
  }

  .history-resume {
    display: inline-flex;
    min-block-size: 0.75rem;
    align-items: center;
    gap: 0.1875rem;
    margin-block-start: 0.125rem;
    color: color-mix(in oklab, var(--color-base-content) 45%, transparent);
    font-size: 0.6875rem;
    font-weight: 500;
    line-height: 1;
    transition: color 200ms ease;
  }

  .history-resume-arrow {
    transition: translate 200ms ease;
  }

  .history-delete-button {
    position: absolute;
    inset-block-start: 0;
    inset-inline-end: 0;
    z-index: 2;
    display: flex;
    inline-size: 2rem;
    block-size: 2rem;
    align-items: flex-start;
    justify-content: flex-end;
    border-end-start-radius: 9999px;
    background-color: color-mix(in oklab, var(--color-base-content) 5%, transparent);
    padding-block-start: 0.25rem;
    padding-inline-end: 0.25rem;
    color: color-mix(in oklab, var(--color-base-content) 45%, transparent);
    cursor: pointer;
    opacity: 0;
    transition: all var(--default-transition-duration) var(--default-transition-timing-function);

    &:is(:hover, :focus-visible) {
      background-color: color-mix(in oklab, var(--color-error) 15%, transparent);
      color: var(--color-error);
      opacity: 1 !important;
    }

    &:focus-visible {
      outline: 2px solid var(--color-error);
      outline-offset: -2px;
    }
  }

  @media (hover: none) {
    .history-delete-button {
      opacity: 0.58;
    }
  }

  @container (inline-size < 16.5rem) {
    .media-card {
      gap: 0.6875rem;
      padding: 0.625rem;
    }

    .media-cover {
      inline-size: 3rem;
    }
  }

  @media (width < 40rem) {
    .history-list {
      --fade-size: 1.75rem;
      display: flex;
      max-block-size: none;
      flex-wrap: nowrap;
      overflow-x: auto;
      overflow-y: hidden;
      overscroll-behavior-x: contain;
      scroll-snap-type: x mandatory;
      scroll-padding-inline: var(--fade-size);
      scrollbar-gutter: auto;
      mask:
        linear-gradient(black, black) no-clip subtract,
        linear-gradient(to var(--fade-dir), black, transparent) var(--fade-start-pos) 0 / var(--ks-fade-start) 100%
          no-repeat no-clip,
        linear-gradient(to var(--fade-dir), transparent, black) var(--fade-end-pos) 0 / var(--ks-fade-end) 100%
          no-repeat no-clip;

      &:dir(rtl) {
        --fade-dir: left;
        --fade-start-pos: 100%;
        --fade-end-pos: 0;
      }
    }

    @supports (animation-timeline: scroll()) {
      .history-list {
        animation-timeline: scroll(self inline);
      }
    }

    .history-slot {
      flex: 0 0 min(19.5rem, 84vw);
      scroll-snap-align: start;
    }
  }
</style>
