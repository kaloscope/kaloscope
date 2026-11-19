<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import { Cell, DataView, HCell, Image, Paginator, Uploader, confirm, type PaginatorProps } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { restorePosition } from '$lib/stores.svelte';
  import type { Favorite, Page, Resp } from '$lib/types';
  import { onMount } from 'svelte';
  import { queryParameters, ssp } from 'sveltekit-search-params';

  let favorites: Favorite[] = $state([]);

  // the URL query parameters
  const query = queryParameters(
    {
      page_num: ssp.number(0),
      page_size: ssp.number(0)
    },
    {
      pushHistory: false
    }
  );

  let pagination: Omit<PaginatorProps, 'current' | 'size'> = $state({ onchange: () => search(true) });
  const loading = createLoading();

  /**
   * Search for favorites.
   *
   * @param toTop - Whether to scroll to the top of the page after the search.
   */
  function search(toTop: boolean = false) {
    loading.start();
    api
      .post('user/favorite/list', {
        json: {
          page_num: query.page_num,
          page_size: query.page_size
        }
      })
      .json<Resp<Page<Favorite>>>()
      .then(({ data }) => {
        pagination.total = data.total;
        favorites = data.items;
      })
      .finally(() => {
        loading.end();
        restorePosition(window, toTop);
      });
  }

  /**
   * Delete a favorite resource.
   *
   * @param favorite - Favorite resource.
   */
  function del(favorite: Favorite) {
    loading.start();
    api
      .post(`flow/indexer/${favorite.indexer_id}/unfavorite`, {
        json: {
          rsrc_id: favorite.rsrc.id
        }
      })
      .then(() => search())
      .catch(() => loading.end());
  }

  onMount(() => {
    const params = page.url.searchParams;
    query.page_num = Number(params.get('page_num')) || 1;
    query.page_size = Number(params.get('page_size')) || 15;
    search();
  });
</script>

<DataView loading={$loading} data={favorites} skeleton={{ ratio: '1/1' }}>
  {#snippet header()}
    <HCell width="100%" text={$_('entity.favorites')} />
    <HCell actions />
  {/snippet}
  {#snippet row(favorite)}
    {@const rsrc = favorite.rsrc}
    <Cell
      class="group {favorite.url ? 'cursor-pointer' : ''}"
      onclick={(event) => {
        // the title link navigates by itself
        if (favorite.url && !(event.target instanceof Element && event.target.closest('a'))) goto(favorite.url);
      }}
    >
      <Image transparent src={rsrc.cover} width="3.5rem" class="rounded-md!" />
      <div class="flex w-full flex-col gap-4">
        <div class={favorite.url ? 'transition-colors group-hover:text-primary' : ''}>
          <svelte:element this={favorite.url ? 'a' : 'span'} href={favorite.url ?? undefined} class="text-sm">
            {rsrc.title}
          </svelte:element>
          {#if rsrc.misc}
            <span class="italic-text">[{rsrc.misc}]</span>
          {/if}
        </div>
        <Uploader up={rsrc.uploader} extra={$dateTime(favorite.created_at)} />
      </div>
    </Cell>
    <Cell
      actions={[
        {
          icon: icons.deleteDismiss,
          text: $_('action.delete', $_('entity.favorite')),
          onclick: () => {
            confirm({
              icon: icons.deleteDismiss,
              title: $_('action.delete', $_('entity.favorite')),
              onconfirm: () => del(favorite)
            });
          }
        }
      ]}
    />
  {/snippet}
  {#snippet paginator(disabled)}
    <Paginator {disabled} {...pagination} bind:current={query.page_num} size={query.page_size} />
  {/snippet}
</DataView>
