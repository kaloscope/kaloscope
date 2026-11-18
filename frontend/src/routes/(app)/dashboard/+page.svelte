<script lang="ts">
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
  import { restorePosition, user } from '$lib/stores';
  import type {
    FlowGraph,
    IndexerAuth,
    IndexerConfig,
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
  let loadWatches = $derived($user?.preferences?.recent_watches ?? false);
  let loadSearches = $derived($user?.preferences?.recent_searches ?? false);
  let loadReads = $derived(($user?.preferences?.recent_reads ?? false) && $user?.preferences?.read_records !== 0);
  let showWatches = $derived(loadWatches && latestWatches.length > 0);
  let showSearches = $derived(loadSearches && searches.length > 0);
  let showReads = $derived(loadReads && reads.length > 0);
  let historyCount = $derived([showSearches, showWatches, showReads].filter(Boolean).length);

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

  // load the user histories
  $effect(() => {
    if (loadWatches || loadSearches) {
      untrack(loadHistories);
    }
  });

  // discard reading results from an old account or a disabled dashboard section
  $effect(() => {
    const owner = $user?.id;
    const enabled = loadReads;
    const controller = new AbortController();
    reads = [];
    if (owner && enabled) untrack(() => void loadReadingHistory(controller.signal));
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
          {@render historyHead($_('preference.dashboard.search'), 'search')}
          <ul class="history-list max-sm:scrollbar-none">
            {#each searches as item (item.id)}
              <li
                class="group history-list-item card-surface p-3 pr-10 max-sm:w-[min(20rem,85vw)]"
                animate:flip={{ duration: 200 }}
              >
                <a href={searchHref(item)} class="flex min-w-0 cursor-pointer items-center gap-3" title={item.keyword}>
                  <span class="history-icon flex-center" aria-hidden="true">
                    <iconify-icon icon={icons.search} width="1.125rem"></iconify-icon>
                  </span>
                  <div class="flex min-w-0 flex-col gap-1">
                    <span class="truncate text-sm font-medium transition-colors group-hover:text-primary">
                      {item.keyword}
                    </span>
                    <div class="flex min-w-0 items-center gap-2 text-xs">
                      <span class="truncate opacity-70">{item.graph?.name ?? $_('nav.websearch.global.search')}</span>
                      <span class="shrink-0 opacity-50">·</span>
                      <span class="shrink-0 opacity-40">{$dateTime(item.updated_at)}</span>
                    </div>
                  </div>
                </a>
                {@render historyDelete(item.id, 'search')}
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      <!-- recent watches -->
      {#if showWatches}
        <section class="min-w-0">
          {@render historyHead($_('preference.dashboard.watch'), 'video')}
          <ul class="history-list max-sm:scrollbar-none">
            {#each latestWatches as item (item.id)}
              {@const media = item.media}
              {@const parent = media?.parent}
              <li
                class="group history-list-item card-surface p-2 pr-10 pb-2.5 max-sm:w-[min(20rem,85vw)]"
                animate:flip={{ duration: 200 }}
              >
                <!-- svelte-ignore a11y_click_events_have_key_events -->
                <div
                  tabindex="0"
                  role="button"
                  class="flex min-w-0 cursor-pointer items-center gap-3"
                  onclick={() => playMedia(item)}
                  title={parent?.title ?? parent?.name ?? mediaTitle(media)}
                >
                  <Image
                    proxy="store"
                    src={parent?.poster ?? media?.poster}
                    icon={media?.lib ? LibType[media.lib.lib_type].icon : undefined}
                    width="3rem"
                    ratio="2/3"
                    class="rounded-md!"
                  />
                  <div class="flex min-w-0 flex-col gap-1">
                    {#if parent}
                      <span class="truncate text-sm font-medium transition-colors group-hover:text-primary">
                        {parent.title ?? parent.name}
                      </span>
                      <span class="truncate text-xs opacity-70">{mediaTitle(media)}</span>
                      <span class="truncate text-xs opacity-40">{$dateTime(item.updated_at)}</span>
                    {:else}
                      <span class="truncate text-sm font-medium transition-colors group-hover:text-primary">
                        {mediaTitle(media)}
                      </span>
                      <span class="text-xs opacity-40">{$dateTime(item.updated_at)}</span>
                    {/if}
                  </div>
                </div>
                {@render historyDelete(item.id, 'video')}
                {@render historyProgress(item.percentage)}
              </li>
            {/each}
          </ul>
        </section>
      {/if}
      <!-- recent reads -->
      {#if showReads}
        <section class="min-w-0">
          {@render historyHead($_('preference.dashboard.read'), 'reading')}
          <ul class="history-list max-sm:scrollbar-none">
            {#each reads as item (item.id)}
              {@const media = item.media!}
              <li
                class="group history-list-item card-surface p-2 pr-10 pb-2.5 max-sm:w-[min(20rem,85vw)]"
                animate:flip={{ duration: 200 }}
              >
                <button
                  type="button"
                  class="flex w-full min-w-0 cursor-pointer items-center gap-3 text-left"
                  onclick={() => goto(`/medialibs/${media.lib!.id}/${media.id}`, { state: { resumeReading: true } })}
                  aria-label="{$_('media.continue_read')} {mediaTitle(media)}"
                  title={mediaTitle(media)}
                >
                  <Image
                    proxy="store"
                    src={media.poster}
                    icon={media.lib ? LibType[media.lib.lib_type].icon : undefined}
                    width="3rem"
                    ratio="2/3"
                    class="rounded-md!"
                  />
                  <div class="flex min-w-0 flex-col gap-1">
                    <span class="truncate text-sm font-medium transition-colors group-hover:text-primary">
                      {mediaTitle(media)}
                    </span>
                    <span class="truncate text-xs opacity-70">{media.lib?.name}</span>
                    <span class="truncate text-xs opacity-40">{$dateTime(item.updated_at)}</span>
                  </div>
                </button>
                {@render historyDelete(item.id, 'reading')}
                {@render historyProgress(item.percentage)}
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
  {#if settled && Object.keys(boards).length === 0}
    <div class="flex-center {historyCount > 0 ? '' : 'min-h-[60vh]'}">
      <Empty compact={historyCount > 0} title={$_('empty.boards.title')} description={$_('empty.boards.tip')}>
        {#if $user?.role === 'admin'}
          <a href="/settings/workflows/templates" class="empty-action">
            <iconify-icon icon={icons.flowchart} width="1rem" aria-hidden="true"></iconify-icon>
            {$_('empty.browse_templates')}
          </a>
        {/if}
      </Empty>
    </div>
  {/if}
</Container>

{#snippet historyHead(title: string, type: HistoryType)}
  <div class="mb-1 flex h-8 items-center justify-between gap-2 px-2">
    <h2 class="section-title"><span class="truncate">{title}</span></h2>
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
    <iconify-icon icon={icons.dismiss} width="1rem"></iconify-icon>
  </button>
{/snippet}

{#snippet historyProgress(percentage: number | null | undefined)}
  {#if percentage !== null}
    <div class="absolute bottom-0 left-0 h-0.75 w-full bg-base-content/8">
      <div class="h-full rounded-r-full bg-primary/60" style="width: {percentage}%"></div>
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

  .history-list {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 16rem), 1fr));
    gap: 0.625rem;
    padding: 0.5rem;
    overflow-y: auto;
    max-height: 19rem;
  }

  .history-list-item {
    position: relative;
    min-width: 0;
    min-height: 4.0625rem;
    overflow: hidden;
    transition: box-shadow 200ms ease;

    &:hover {
      box-shadow:
        0 0 0 1px color-mix(in oklab, var(--color-base-content) 7%, transparent),
        0 6px 16px -8px color-mix(in oklab, var(--color-shade) 19%, transparent);

      .history-delete-button {
        opacity: 0.7;

        &:is(:hover, :focus-visible) {
          opacity: 1;
        }
      }
    }
  }

  .history-icon {
    flex-shrink: 0;
    width: 2.5rem;
    height: 2.5rem;
    border-radius: min(var(--radius-field), 0.625rem);
    background-color: color-mix(in oklab, var(--color-primary) 10%, transparent);
    color: color-mix(in oklab, var(--color-primary) 70%, var(--color-base-content));
  }

  .history-delete-button {
    position: absolute;
    top: 0;
    right: 0;
    display: flex;
    width: 2rem;
    height: 2rem;
    align-items: flex-start;
    justify-content: flex-end;
    border-bottom-left-radius: 9999px;
    background-color: color-mix(in oklab, var(--color-base-content) 5%, transparent);
    padding-top: 0.25rem;
    padding-right: 0.25rem;
    color: color-mix(in oklab, var(--color-base-content) 45%, transparent);
    cursor: pointer;
    opacity: 0;
    transition: all var(--default-transition-duration) var(--default-transition-timing-function);

    &:is(:hover, :focus-visible) {
      background-color: color-mix(in oklab, var(--color-error) 15%, transparent);
      color: var(--color-error);
      opacity: 1;
    }

    &:focus-visible {
      outline: none;
    }
  }

  @media (width < 40rem) {
    .history-list-item {
      flex-shrink: 0;
      scroll-snap-align: start;
    }

    .history-list {
      display: flex;
      max-height: none;
      flex-wrap: nowrap;
      overflow-x: auto;
      overflow-y: hidden;
      scroll-snap-type: x proximity;
      scroll-padding-inline: 0.5rem;
    }
  }
</style>
