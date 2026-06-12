<script lang="ts">
  import { enhance } from '$app/forms';
  import { beforeNavigate, goto } from '$app/navigation';
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import {
    Button,
    confirm,
    Container,
    DataView,
    HCell,
    Label,
    markFavorites,
    Modal,
    Paginator,
    Search,
    SearchHit,
    type PaginatorProps
  } from '$lib/components';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { positions, restorePosition } from '$lib/stores';
  import type { IndexerAuth, Page, Resource, Resp, ViewMode, ViewModes } from '$lib/types';
  import { tick, untrack } from 'svelte';
  import { MediaQuery } from 'svelte/reactivity';
  import { queryParameters, ssp } from 'sveltekit-search-params';
  import type { PageData } from './$types';
  import { aspectRatio } from '$lib/utils';

  // the indexer configurations
  let { data }: { data: PageData } = $props();
  let { display, keyword, filters: querySchema } = $derived(data.config.search ?? {});
  let { login: loginConfig } = $derived(data.config.auth ?? {});

  // the indexer ID
  let indexerId: string = $derived(page.params.indexer_id ?? '');

  // the URL query parameters
  const query = queryParameters(
    {
      view_mode: ssp.string(),
      page_num: ssp.number(0),
      page_size: ssp.number(0),
      keyword: ssp.string(''),
      filters: ssp.string()
    },
    {
      pushHistory: false
    }
  );

  // the data view instance
  let view: DataView<Resource>;
  let viewModes: ViewModes | undefined = $state(undefined);

  // the resources to display
  let resources: Resource[] = $state([]);
  let pagination: Omit<PaginatorProps, 'current' | 'size'> = $state({ onchange: () => search(true) });

  // the login modal and user credentials
  let loginModal: Modal;
  let loggedUser: string | null = $state(null);
  let username: string | null = $state(null);
  let password: string | null = $state(null);
  const loginSchema = createFormSchema(({ text, password }) => ({
    username: text().maxlength(64),
    password: password().maxlength(64)
  }));

  // the loading states
  const outerLoading = createLoading();
  const innerLoading = createLoading();
  const loginLoading = createLoading();
  const authLoading = createLoading();

  // the abort controller
  let abortController: AbortController | null = null;

  // the standalone display mode media query
  const standaloneMode = new MediaQuery('(display-mode: standalone)');

  // capture the scroll position of the current page
  beforeNavigate(({ from, to }) => {
    const fromUrl = from?.url;
    const toUrl = to?.url;
    if (fromUrl && toUrl && fromUrl.pathname !== toUrl.pathname && view) {
      const position = standaloneMode.current ? view.scrollPosition() : { left: window.scrollX, top: window.scrollY };
      positions.set({ ...$positions, [fromUrl.pathname]: position });
    }
  });

  /**
   * Get the auth status.
   */
  async function auth(): Promise<void> {
    if (loginConfig) {
      authLoading.start();
      try {
        const resp = await api.get(`flow/indexer/${indexerId}/auth`).json<Resp<IndexerAuth>>();
        // if the user is logged in, get the user name from the response
        loggedUser = resp.data?.name ?? null;
      } finally {
        authLoading.end();
      }
    }
  }

  /**
   * Login to the indexer with the provided credentials.
   *
   * @param loginForm - The form element.
   */
  function login(loginForm: HTMLFormElement) {
    loginLoading.start();
    api
      .post(`flow/graph/${indexerId}/execute`, {
        json: {
          $start: 'auth_start',
          username: username,
          password: password
        }
      })
      .then(() => {
        loginModal.close();
        auth().then(() => search());
        setTimeout(() => loginForm.reset(), 200);
      })
      .finally(() => {
        loginLoading.end();
      });
  }

  /**
   * Logout the user from the indexer.
   */
  async function logout(): Promise<void> {
    authLoading.start();
    try {
      const resp = await api.post(`flow/indexer/${indexerId}/logout`);
      // if the user is logged out, set the user name to null
      if (resp.ok) {
        loggedUser = null;
      }
    } finally {
      authLoading.end();
    }
  }

  /**
   * Search for resources.
   *
   * @param toTop - Whether to scroll to the top of the page after the search.
   */
  function search(toTop: boolean = false) {
    // abort the previous request
    let aborted = false;
    abortController?.abort();

    // check if the keyword is required but not provided, or the user is not logged in
    if ((keyword?.required && !query.keyword) || (loginConfig?.required && loggedUser === null)) {
      resources = [];
      pagination.total = null;
      pagination.totalPages = null;
      innerLoading.end();
      outerLoading.end();
      return;
    }

    // execute the search request
    abortController = new AbortController();
    innerLoading.start();
    api
      .post(`flow/graph/${indexerId}/execute`, {
        signal: abortController.signal,
        json: {
          $start: 'search_start',
          page_num: query.page_num,
          page_size: query.page_size,
          keyword: query.keyword,
          ...(query.filters ? JSON.parse(query.filters) : {})
        }
      })
      .json<Resp<Page<Resource>>>()
      .then(({ data }) => {
        if (!data || !data.items) {
          resources = [];
          pagination.total = null;
          return;
        }
        resources = data.items;
        markFavorites(indexerId, resources);
        if (data.total === null || data.total === undefined) {
          if (data.totalPages) {
            pagination.totalPages = data.totalPages;
            pagination.simpleMode = false;
          } else {
            pagination.total = resources.length;
            pagination.simpleMode = true;
          }
        } else {
          pagination.total = data.total;
          pagination.simpleMode = false;
        }
      })
      .catch((error) => {
        if (error.name === 'AbortError') {
          aborted = true;
        } else {
          resources = [];
          pagination.total = null;
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

    // record search history
    if (query.keyword && indexerId) {
      api.post('user/history/record', {
        json: { rel_type: 'search', rel_id: indexerId, keyword: query.keyword }
      });
    }
  }

  let _indexerId: string | null = null;
  $effect(() => {
    if (_indexerId !== indexerId) {
      untrack(() => {
        // check if the indexer ID is valid
        if (!data.menus[1]?.routes.some((r) => r.path === `/websearch/${indexerId}`)) {
          const route = data.menus[0]?.routes[0]?.path;
          route && goto(route, { replaceState: true });
          return;
        }
        outerLoading.start();
        // get supported view modes from config
        const modes = display?.view_modes ?? [];
        viewModes = (Array.isArray(modes) && modes.length > 0 ? modes : ['table']) as ViewModes;
        // initialize query parameters
        const params = page.url.searchParams;
        query.keyword = params.get('keyword') || '';
        query.filters = params.get('filters') || '';
        query.page_num = Number(params.get('page_num')) || 1;
        query.page_size = (display?.page_size ?? Number(params.get('page_size'))) || 20;
        let viewMode = params.get('view_mode') as ViewMode | null;
        if (!viewMode || !viewModes.includes(viewMode)) {
          viewMode = viewModes[0];
        }
        query.view_mode = viewMode;
        // search for resources
        if (loginConfig?.required) {
          auth().then(() => search());
        } else {
          auth();
          search();
        }
        _indexerId = indexerId;
      });
    }
  });
</script>

<Container class="pull-to-refresh" loading={$outerLoading}>
  <DataView
    bind:this={view}
    bind:mode={query.view_mode as ViewMode}
    modes={viewModes}
    data={resources}
    loading={$innerLoading}
    hideOnScroll={standaloneMode.current}
    filtersClass="sm:justify-center"
    gridClass="grid-cols-2 {aspectRatio(display?.cover_ratio) > 1 ? 'grid-cols-sparse' : 'grid-cols-compact'}"
    itemClass="rounded-sm bg-base-100 shadow-sm lg:hover:shadow-lg lg:mb-4"
  >
    {#snippet filters()}
      <Search
        manual
        required={keyword?.required}
        label={$_('field.keyword')}
        bind:value={query.keyword}
        onsearch={() => {
          query.page_num = 1;
          search(true);
        }}
        schema={querySchema}
        filters={query.filters}
        onfilter={(value) => {
          query.filters = value;
          search();
        }}
        maxWidth="36rem"
      />
    {/snippet}

    {#snippet actions()}
      {#if loginConfig}
        {#if loggedUser !== null}
          <Button
            square={!loggedUser}
            ghost={false}
            icon={icons.userFill}
            text={loggedUser}
            class="max-w-36 *:last:max-sm:hidden"
            disabled={$authLoading !== null}
            loading={$authLoading}
            onclick={() => {
              confirm({
                icon: icons.user,
                title: $_('app.logout'),
                onconfirm: () => logout().then(() => search())
              });
            }}
          />
        {:else}
          <Button
            square={false}
            icon={icons.user}
            text={$_('app.login')}
            class="max-w-36 bg-base-300 opacity-50!"
            disabled={$authLoading !== null}
            loading={$authLoading}
            onclick={() => loginModal.show()}
          />
        {/if}
      {/if}
    {/snippet}

    <!-- table view -->
    {#snippet header()}
      <HCell width="100%" />
      <HCell width={['6rem', '3rem']} />
    {/snippet}
    {#snippet row(rsrc)}
      <SearchHit mode="table" {rsrc} {indexerId} indexerConfig={data.config} coverRatio={display?.cover_ratio} />
    {/snippet}

    <!-- grid view -->
    {#snippet item(rsrc)}
      <SearchHit mode="grid" {rsrc} {indexerId} indexerConfig={data.config} coverRatio={display?.cover_ratio} />
    {/snippet}

    {#snippet paginator(disabled)}
      <Paginator {disabled} {...pagination} bind:current={query.page_num} size={query.page_size} />
    {/snippet}
  </DataView>
</Container>

<Modal title={$_('app.login')} bind:this={loginModal}>
  <form
    method="post"
    use:enhance={({ formElement, cancel }) => {
      cancel();
      login(formElement);
    }}
  >
    <fieldset class="fieldset">
      <Label small>{$_('field.username')}</Label>
      <label class="input w-full">
        <iconify-icon icon={icons.user}></iconify-icon>
        <input class="grow" bind:value={username} {...loginSchema.username} />
      </label>
      <Label small>{$_('field.password')}</Label>
      <label class="input w-full">
        <iconify-icon icon={icons.key}></iconify-icon>
        <input class="grow" bind:value={password} {...loginSchema.password} />
      </label>
    </fieldset>
    <div class="modal-action">
      <button type="button" class="btn" onclick={() => loginModal.close()}>
        {$_('message.cancel')}
      </button>
      <button type="submit" class="btn btn-submit" disabled={$loginLoading !== null}>
        {$_('app.login')}
        {#if $loginLoading}
          <span class="loading loading-xs loading-dots"></span>
        {/if}
      </button>
    </div>
  </form>
</Modal>
