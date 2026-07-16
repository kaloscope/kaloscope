<script lang="ts">
  import { goto } from '$app/navigation';
  import { api } from '$lib/api';
  import {
    Button,
    confirm,
    Container,
    DataView,
    HCell,
    Image,
    mediaTitle,
    SearchHit,
    VideoPlayer,
    ViewSwitcher
  } from '$lib/components';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { restorePosition, user } from '$lib/stores';
  import type {
    FlowGraph,
    IndexerAuth,
    IndexerConfig,
    MediaItem,
    Resource,
    Resp,
    ViewMode,
    ViewModes
  } from '$lib/types';
  import { aspectRatio, buildStreamUrl } from '$lib/utils';
  import { onMount, tick, untrack } from 'svelte';
  import { flip } from 'svelte/animate';
  import { fade } from 'svelte/transition';
  import type { PageData } from './$types';

  /**
   * The type of the board resources.
   */
  type Resources = {
    title?: string | null;
    items: Resource[];
  };

  /**
   * The type of the dashboard panel.
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
   * The type of the history entry.
   */
  type HistoryType = 'video' | 'search';

  /**
   * The type of the watch history.
   */
  type WatchHistory = {
    id: number;
    rel_id: number;
    updated_at: string;
    position: number;
    percentage: number;
    media: MediaItem | null;
  };

  /**
   * The type of the search history.
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
  let searches: SearchHistory[] = $state([]);
  let loadWatches = $derived($user?.preferences?.recent_watches ?? false);
  let loadSearches = $derived($user?.preferences?.recent_searches ?? false);
  let showWatches = $derived(loadWatches && watches.length > 0);
  let showSearches = $derived(loadSearches && searches.length > 0);

  // the player instance and playing state
  let player: VideoPlayer | null = $state(null);
  let playing = $state(false);

  /**
   * Load board resources for a given flow graph.
   *
   * @param graph - The flow graph instance.
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

  /**
   * Load the user histories if enabled in preferences.
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
   * Delete a history entry by ID and type.
   *
   * @param id - The ID of the history entry to delete.
   * @param type - The type of the history entry.
   */
  function deleteHistory(id: number, type: HistoryType) {
    api.post('user/history/delete', { json: { ids: [id] } }).then(() => {
      if (type === 'video') {
        watches = watches.filter((w) => w.id !== id);
      } else if (type === 'search') {
        searches = searches.filter((s) => s.id !== id);
      }
    });
  }

  /**
   * Delete all history entries of a given type with confirmation.
   *
   * @param type - The type of the history entries to delete.
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
        }
      }
    });
  }

  /**
   * Play a media item from the watch history in the video player.
   *
   * @param w - The watch history entry.
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
        startTime: w.position
      });
    });
  }

  /**
   * Navigate to the search page for the given history entry.
   *
   * @param s - The search history entry.
   */
  function gotoSearch(s: SearchHistory) {
    const keyword = s.keyword;
    if (!s.keyword) {
      return;
    }
    if (!s.graph) {
      goto(`/websearch/global?restore=false&keyword=${encodeURIComponent(keyword)}`);
    } else {
      goto(`/websearch/${s.rel_id}?restore=false&keyword=${encodeURIComponent(keyword)}`);
    }
  }

  // load the user histories
  $effect(() => {
    if (loadWatches || loadSearches) {
      untrack(loadHistories);
    }
  });

  // load the board resources
  onMount(async () => {
    boards = {};
    for (const graph of data.graphs) {
      await load(graph);
    }
    restorePosition(window);
  });
</script>

<Container padding="1rem 0" maxWidth="max(150vh,72rem)">
  <!-- recent histories -->
  {#if showSearches || showWatches}
    <div
      class="mb-10 grid gap-4 {showWatches && showSearches ? 'lg:grid-cols-2' : ''}"
      transition:fade={{ duration: 200 }}
    >
      <!-- recent searches -->
      {#if showSearches}
        <section class="min-w-0">
          <div class="flex h-8 items-center justify-between px-2">
            <span class="text-xl font-bold opacity-80">{$_('preference.dashboard.search')}</span>
            <Button
              icon={icons.clear}
              text={$_('action.clear', $_('preference.dashboard.search'))}
              iconClass="opacity-80"
              onclick={() => deleteAllHistories('search')}
            />
          </div>
          <div class="divider mt-0 mb-1"></div>
          <ul class="history-list">
            {#each searches as item (item.id)}
              <li
                class="group history-list-item p-3 pr-10 transition-colors max-sm:w-[min(20rem,85vw)]"
                animate:flip={{ duration: 200 }}
              >
                <!-- svelte-ignore a11y_click_events_have_key_events -->
                <div
                  tabindex="0"
                  role="button"
                  class="min-w-0 cursor-pointer"
                  onclick={() => gotoSearch(item)}
                  title={item.keyword}
                >
                  <div class="flex min-w-0 flex-col gap-1">
                    <span class="truncate text-sm font-medium group-hover:text-primary">
                      {item.keyword}
                    </span>
                    <div class="flex min-w-0 items-center gap-2 text-xs">
                      <span class="truncate opacity-70">{item.graph?.name ?? $_('nav.websearch.global.search')}</span>
                      <span class="shrink-0 opacity-50">·</span>
                      <span class="shrink-0 opacity-40">{$dateTime(item.updated_at)}</span>
                    </div>
                  </div>
                </div>
                <button
                  type="button"
                  class="history-delete-button"
                  aria-label={$_('action.delete')}
                  onclick={() => deleteHistory(item.id, 'search')}
                >
                  <iconify-icon icon={icons.dismiss} width="1rem"></iconify-icon>
                </button>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      <!-- recent watches -->
      {#if showWatches}
        <section class="min-w-0">
          <div class="flex h-8 items-center justify-between px-2">
            <span class="text-xl font-bold opacity-80">{$_('preference.dashboard.watch')}</span>
            <Button
              icon={icons.clear}
              text={$_('action.clear', $_('preference.dashboard.watch'))}
              iconClass="opacity-80"
              onclick={() => deleteAllHistories('video')}
            />
          </div>
          <div class="divider mt-0 mb-1"></div>
          <ul class="history-list">
            {#each watches as item (item.id)}
              {@const media = item.media}
              {@const parent = media?.parent}
              <li
                class="group history-list-item p-2 pr-10 pb-2.5 transition-colors max-sm:w-[min(20rem,85vw)]"
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
                  <Image proxy="store" src={parent?.poster ?? media?.poster} width="3rem" ratio="2/3" />
                  <div class="flex min-w-0 flex-col gap-1">
                    {#if parent}
                      <span class="truncate text-sm font-medium group-hover:text-primary">
                        {parent.title ?? parent.name}
                      </span>
                      <span class="truncate text-xs opacity-70">{mediaTitle(media)}</span>
                      <span class="truncate text-xs opacity-40">{$dateTime(item.updated_at)}</span>
                    {:else}
                      <span class="truncate text-sm font-medium group-hover:text-primary">
                        {mediaTitle(media)}
                      </span>
                      <span class="text-xs opacity-40">{$dateTime(item.updated_at)}</span>
                    {/if}
                  </div>
                </div>
                <button
                  type="button"
                  class="history-delete-button"
                  aria-label={$_('action.delete')}
                  onclick={() => deleteHistory(item.id, 'video')}
                >
                  <iconify-icon icon={icons.dismiss} width="1rem"></iconify-icon>
                </button>
                {#if item.percentage !== null}
                  <div class="absolute bottom-0 left-0 h-0.75 w-full bg-base-200">
                    <div class="h-full bg-primary/50" style="width: {item.percentage}%"></div>
                  </div>
                {/if}
              </li>
            {/each}
          </ul>
        </section>
      {/if}
    </div>
  {/if}

  <!-- dashboard panels -->
  {#each Object.values(boards) as board (board.id)}
    <div class="flex h-8 items-center justify-between px-2" transition:fade={{ duration: 200 }}>
      <span class="flex-center gap-2 text-xl font-bold opacity-80">
        {#if board.icon}
          <Image src={board.icon} width="1.5rem" />
        {/if}
        {board.name}
      </span>
      {#if board.loading}
        <span class="loading mx-1.5 loading-sm loading-spinner"></span>
      {:else if board.resources.length > 0}
        <ViewSwitcher modes={board.viewModes} bind:mode={board.viewMode} />
      {/if}
    </div>
    <div class="divider my-0"></div>
    {#if board.resources.length > 0}
      <div class="tabs-border mb-10 tabs" transition:fade>
        {#each board.resources as rsrcs, index (index)}
          <label class="tab {board.resources.length === 1 ? 'hidden' : ''}">
            <input type="radio" checked={board.activeId === index} onclick={() => (board.activeId = index)} />
            <span class="font-semibold {board.activeId === index ? 'text-shadow-xs' : ''}">{rsrcs.title}</span>
          </label>
          {#if board.activeId === index}
            <div class="tab-content mt-2" transition:fade>
              <DataView
                data={rsrcs.items}
                mode={board.viewMode}
                loading={board.loading}
                class="px-0!"
                tableClass="[&_thead_tr]:border-0"
                gridClass="grid-cols-2 {aspectRatio(board.coverRatio) > 1 ? 'grid-cols-sparse' : 'grid-cols-medium'}"
                itemClass="rounded-sm bg-base-100 shadow-sm lg:hover:shadow-lg lg:mb-4"
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
    {/if}
  {/each}
</Container>

<!-- player overlay -->
{#if playing}
  <div class="fixed inset-0 layer-1 max-sm:bottom-(--ks-dock-h)">
    <VideoPlayer bind:this={player} />
  </div>
{/if}

<style>
  .history-list {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 16rem), 1fr));
    gap: 0.5rem;
    padding: 0.5rem;
    overflow-y: auto;
    max-height: 19rem;
  }

  .history-list-item {
    position: relative;
    min-width: 0;
    min-height: 4.0625rem;
    overflow: hidden;
    box-shadow: var(--shadow-sm);
    border-radius: var(--radius-box);
    background-color: var(--color-base-100);

    &:hover {
      background-color: var(--color-base-200);

      .history-delete-button {
        opacity: 0.7;

        &:is(:hover, :focus-visible) {
          opacity: 1;
        }
      }
    }
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
    }

    .history-list {
      display: flex;
      max-height: none;
      flex-wrap: nowrap;
      overflow-x: auto;
      overflow-y: hidden;
      padding-bottom: 0.75rem;
    }
  }
</style>
