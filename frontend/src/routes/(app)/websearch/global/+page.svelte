<script lang="ts">
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import { Container, DataView, HCell, Paginator, Search, SearchHit, markFavorites } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { restorePosition } from '$lib/stores';
  import type { IndexerAuth, IndexerConfig, Page, Resource, Resp } from '$lib/types';
  import { onMount } from 'svelte';
  import { fade, fly } from 'svelte/transition';
  import { queryParameters, ssp } from 'sveltekit-search-params';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  // the URL query parameters
  const query = queryParameters(
    {
      keyword: ssp.string(''),
      tab_id: ssp.string(),
      page_num: ssp.number(),
      page_size: ssp.number()
    },
    {
      pushHistory: false,
      showDefaults: false
    }
  );

  type Tab = {
    id: string;
    name: string;
    loading: boolean | null;
    resources: Resource[];
    coverRatio: string;
    total: number | null;
    pageNum: number;
    pageSize: number;
    simpleMode: boolean;
    config: IndexerConfig;
  };
  let tabs: Record<string, Tab> = $state({});
  let isEmpty: boolean = $derived(!query.keyword && Object.keys(tabs).length === 0);
  let activeId: string | null = $state(null);
  let transition: boolean = $state(false);

  /**
   * Search for resources.
   */
  function search() {
    // clear the results
    tabs = {};
    if (!query.keyword) {
      return;
    }
    for (const route of data.menus[1].routes) {
      // '/websearch/id' => 'id'
      const id = route.path?.slice(11);
      if (!id) {
        continue;
      }
      load(id, route.title);
    }
    // record search history
    api.post('user/history/record', {
      json: { rel_type: 'search', rel_id: 0, keyword: query.keyword }
    });
  }

  /**
   * Load tab resources.
   *
   * @param id - The indexer ID.
   * @param name - The indexer name.
   */
  async function load(id: string, name?: string) {
    const reload = !!tabs[id];
    let tab = reload ? tabs[id] : ({} as Tab);
    let restored = false;
    try {
      if (!reload && name) {
        // load the indexer config
        const config = (await api.get(`flow/indexer/${id}/config`).json<Resp<IndexerConfig>>()).data;
        const { login } = config.auth ?? {};
        const { display, keyword } = config.search ?? {};
        // check if the global search is enabled
        if (!keyword?.global) {
          return;
        }
        // check if login is required
        if (login?.required) {
          const resp = await api.get(`flow/indexer/${id}/auth`).json<Resp<IndexerAuth>>();
          if (!resp.data || resp.data.name === null || resp.data.name === undefined) {
            return;
          }
        }
        // create a new tab object
        tab = {
          id: id,
          name: name,
          loading: null,
          resources: [],
          total: null,
          pageNum: 1,
          pageSize: display?.page_size ?? 20,
          simpleMode: false,
          coverRatio: display?.cover_ratio ?? '16/9',
          config: config
        };
        // restore the tab state from the query parameters
        if (query.tab_id === tab.id) {
          activeId = tab.id;
          if (query.page_num) {
            tab.pageNum = query.page_num;
          }
          if (query.page_size) {
            tab.pageSize = query.page_size;
          }
          restored = true;
        }
        tabs[id] = tab;
      }
      // activate the first tab
      if (activeId === null) {
        activeId = id;
      }
      // set the loading state
      tabs[id].loading = false;
      setTimeout(() => {
        if (tabs[id] && tabs[id].loading === false) {
          tabs[id].loading = true;
        }
      }, 500);
      // load the resources
      const resp = await api
        .post(`flow/graph/${id}/execute`, {
          json: {
            $start: 'search_start',
            page_num: tab.pageNum,
            page_size: tab.pageSize,
            keyword: query.keyword
          }
        })
        .json<Resp<Page<Resource>>>();
      if (!resp.data || !resp.data.items) {
        tab.resources = [];
        tab.total = null;
        return;
      }
      tab.resources = await markFavorites(tab.id, resp.data.items);
      if (resp.data.total === null || resp.data.total === undefined) {
        tab.total = tab.resources.length;
        tab.simpleMode = true;
      } else {
        tab.total = resp.data.total;
        tab.simpleMode = false;
      }
      // update the URL query parameters
      if (reload) {
        query.tab_id = tab.id;
        query.page_num = tab.pageNum;
        query.page_size = tab.pageSize;
      }
    } catch (error) {
      console.error(error);
      tab.resources = [];
      tab.total = null;
    } finally {
      if (tabs[id]) {
        tabs[id] = tab;
        tabs[id].loading = null;
      }
      if (restored) {
        restorePosition(window);
      }
    }
  }

  onMount(() => {
    const params = page.url.searchParams;
    query.keyword = params.get('keyword') || '';
    query.tab_id = params.get('tab_id');
    query.page_num = Number(params.get('page_num')) || null;
    query.page_size = Number(params.get('page_size')) || null;
    search();
  });
</script>

<Container rowGap="1rem">
  <div class="flex-center {isEmpty ? 'mt-[25vh]' : 'mt-4'} {transition ? 'duration-500' : ''}">
    {#if isEmpty}
      <span class="app-name text-5xl text-shadow-lg" transition:fly={{ y: -50, duration: transition ? 200 : 0 }}>
        {$_('app.name', { locale: 'en-US' })}
      </span>
    {/if}
  </div>
  <div class="flex-center px-2">
    <Search
      manual
      label={$_('field.keyword')}
      bind:value={query.keyword}
      onsearch={() => {
        if (Object.keys(tabs).length === 0) {
          transition = !!query.keyword;
        } else {
          transition = !query.keyword;
        }
        search();
      }}
      maxWidth="42rem"
      small={false}
    />
  </div>
  {#if !isEmpty}
    <div class="tabs tabs-border" in:fly={{ y: 100, duration: transition ? 200 : 0 }}>
      {#each Object.values(tabs) as tab (tab.id)}
        <label class="tab gap-1.25">
          <input
            type="radio"
            checked={activeId === tab.id}
            onclick={() => {
              activeId = tab.id;
              query.tab_id = activeId;
              query.page_num = tab.pageNum;
              query.page_size = tab.pageSize;
            }}
          />
          <span class="font-semibold {activeId === tab.id ? 'text-shadow-xs' : ''}">{tab.name}</span>
          {#if tab.total}
            <span class="min-w-5 pb-2 text-[10px] italic" in:fade>
              {tab.total > 99 ? '99+' : tab.total}
            </span>
          {:else if tab.loading}
            <span class="loading loading-xs min-w-5 loading-spinner" in:fade></span>
          {/if}
        </label>
        <div class="tab-content mt-2">
          <DataView loading={tab.loading} data={tab.resources}>
            {#snippet header()}
              <HCell width="100%" />
              <HCell width={['6rem', '3rem']} />
            {/snippet}
            {#snippet row(rsrc)}
              <SearchHit
                mode="table"
                {rsrc}
                indexerId={tab.id}
                indexerConfig={tab.config}
                coverRatio={tab.coverRatio}
              />
            {/snippet}
            {#snippet paginator(disabled)}
              <Paginator
                {disabled}
                bind:current={tab.pageNum}
                size={tab.pageSize}
                total={tab.total}
                simpleMode={tab.simpleMode}
                onchange={() => load(tab.id)}
              />
            {/snippet}
          </DataView>
        </div>
      {/each}
    </div>
  {/if}
</Container>
