<script lang="ts" module>
  import { goto } from '$app/navigation';
  import { api } from '$lib/api';
  import type { Favorite, IndexerConfig, Page, Resource, Resp, ViewMode } from '$lib/types';
  import { aspectRatio } from '$lib/utils';

  type SearchHitProps = {
    indexerId?: string | number;
    indexerConfig: IndexerConfig;
    mode: ViewMode;
    rsrc: Resource;
    coverRatio?: string | null;
    searchButton?: boolean;
  };

  /**
   * Calculate the height for a cover image based on the given aspect ratio.
   *
   * @param ratio - A CSS aspect-ratio string, e.g. "16/9", "9/16", "0.75", "auto".
   * @returns The height in rem units, or undefined if the ratio is "auto".
   */
  function coverHeight(ratio: string): string | undefined {
    if (ratio === 'auto') {
      return undefined;
    }
    const r = aspectRatio(ratio);
    return !r || r >= 1 ? '3.5rem' : `${(3.5 / r).toFixed(4)}rem`;
  }

  /**
   * Navigate to the global search page with the given title as the keyword.
   *
   * @param title - The title to search for.
   */
  function globalSearch(title: string | null | undefined) {
    if (title) {
      goto(`/websearch/global?restore=false&keyword=${encodeURIComponent(title)}`);
    }
  }

  /**
   * Mark the resources as favorite or not.
   *
   * @param indexerId - The indexer ID.
   * @param rsrcs - The resources to mark.
   */
  export async function markFavorites(indexerId: string | number, rsrcs: Resource[]) {
    const rsrcIds = rsrcs.map((r) => r.id).filter((id) => !!id);
    if (indexerId && rsrcIds.length > 0) {
      await api
        .post('user/favorite/list', {
          json: {
            page_num: 1,
            page_size: rsrcs.length,
            indexer_id: indexerId,
            rsrc_ids: rsrcIds
          }
        })
        .json<Resp<Page<Favorite>>>()
        .then(({ data }) => {
          if (data && data.items) {
            const favorites = data.items.map((f) => f.rsrc_id);
            // update the resources with the favorite status
            rsrcs.forEach((r) => {
              r.favorite = r.id ? favorites.includes(String(r.id)) : false;
            });
          }
        });
    }
    return rsrcs;
  }
</script>

<script lang="ts">
  import { Badge, Button, Cell, Image, Rating, Ranking, Uploader, downloadPrompt } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { user } from '$lib/stores';

  let { indexerId, indexerConfig, mode, rsrc, coverRatio, searchButton }: SearchHitProps = $props();
  let detailsConfig = $derived(indexerConfig.details);
  let hasRanking = $derived(rsrc.ranking !== null && rsrc.ranking !== undefined);

  /**
   * Mark a resource as favorite.
   *
   * @param rsrc - The resource to mark.
   */
  function favorite(rsrc: Resource) {
    api.post(`flow/indexer/${indexerId}/favorite`, {
      json: {
        rsrc_id: rsrc.id,
        rsrc: rsrc,
        url: detailsRoute(rsrc.id)
      }
    });
    rsrc.favorite = true;
  }

  /**
   * Unmark a resource as favorite.
   *
   * @param rsrc - The resource to unmark.
   */
  function unfavorite(rsrc: Resource) {
    api.post(`flow/indexer/${indexerId}/unfavorite`, {
      json: { rsrc_id: rsrc.id }
    });
    rsrc.favorite = false;
  }

  /**
   * Navigate to the details page.
   *
   * @param rsrc - The clicked resource.
   */
  function gotoDetails(rsrc: Resource) {
    const url = detailsRoute(rsrc.id);
    if (url) goto(url);
  }

  /**
   * Generates a URL for the details route of an indexer resource.
   *
   * @param rsrcId - The resource ID.
   */
  function detailsRoute(rsrcId: string | null | undefined): string | null {
    if (!indexerId || !rsrcId || !detailsConfig) {
      return null;
    }
    // eslint-disable-next-line svelte/prefer-svelte-reactivity
    const searchParams = new URLSearchParams();
    if (detailsConfig && detailsConfig.specific) {
      for (const [key, value] of Object.entries(detailsConfig.specific)) {
        if (value) {
          searchParams.set(key, value);
        }
      }
    }
    const queryString = searchParams.size > 0 ? `?${searchParams.toString()}` : '';
    return `/websearch/${indexerId}/${rsrcId}${queryString}`;
  }
</script>

<!-- table view -->
{#if mode === 'table'}
  <Cell class="group {detailsConfig ? 'cursor-pointer' : ''}" onclick={() => gotoDetails(rsrc)}>
    {#if rsrc.cover}
      {@const ratio = coverRatio ?? '16/9'}
      <div class="relative shrink-0">
        <Image transparent src={rsrc.cover} height={coverHeight(ratio)} {ratio} />
        <div class="absolute inset-0 flex size-full flex-col">
          {#if hasRanking}
            <Ranking compact rank={rsrc.ranking} class="self-start" />
          {:else}
            <Rating compact score={rsrc.rating} class="mt-0.5 ml-0.5 self-start" />
          {/if}
          {#if rsrc.category}
            <span
              class="mt-auto max-w-full self-center truncate px-0.5 text-white opacity-80 text-stroke"
              style="font-size: clamp(0.5rem, calc(1rem - 0.05rem * {rsrc.category?.length || 0}), 0.75rem);"
            >
              {rsrc.category}
            </span>
          {/if}
        </div>
      </div>
    {:else if rsrc.category}
      <Badge class="w-18 shrink-0 justify-center px-1!">
        <span class="line-clamp-2 w-full text-center wrap-break-word">{rsrc.category}</span>
      </Badge>
    {/if}
    <div class="flex w-full min-w-0 flex-col gap-3">
      <div class={detailsConfig ? 'transition-colors group-hover:text-primary' : ''}>
        <span class="text-sm wrap-break-word">{rsrc.title}</span>
        {#if rsrc.misc}
          <span class="italic-text">[{rsrc.misc}]</span>
        {/if}
      </div>
      <Uploader up={rsrc.uploader} at={rsrc.uploaded_at} extra={rsrc.size} />
    </div>
  </Cell>
  <Cell
    actions={[
      {
        condition: !!detailsConfig && rsrc.favorite !== undefined,
        icon: rsrc.favorite ? icons.starFilled : icons.star,
        class: rsrc.favorite ? '[&_iconify-icon]:text-yellow-500' : '',
        text: $_('action.favorite'),
        onclick: () => (rsrc.favorite ? unfavorite(rsrc) : favorite(rsrc))
      },
      {
        condition: !!rsrc.link && $user?.role === 'admin',
        icon: icons.download,
        text: $_('action.download'),
        onclick: () => downloadPrompt(rsrc.link)
      },
      {
        condition: !!searchButton,
        icon: icons.search,
        text: $_('action.search'),
        onclick: () => globalSearch(rsrc.title)
      }
    ]}
  />
{/if}

<!-- grid view -->
{#if mode === 'grid'}
  {@const pointerClass = detailsConfig ? 'cursor-pointer transition-colors hover:text-primary' : ''}
  {@const transClass = 'transition-opacity duration-300'}
  {@const btnClass = 'border-0 bg-black/60 text-white hover:bg-base-300 hover:text-base-content'}
  {@const ratio = coverRatio ?? '16/9'}
  <div class="flex h-full flex-col">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div tabindex="0" role="button" class="group @container relative {pointerClass}" onclick={() => gotoDetails(rsrc)}>
      {#if hasRanking}
        <Ranking rank={rsrc.ranking} class="absolute top-0 left-0 z-1 text-[clamp(0.75rem,7cqw,0.875rem)]" />
      {:else}
        <Rating score={rsrc.rating} class="absolute top-1 left-1 z-1 text-[clamp(0.75rem,7cqw,0.875rem)]" />
      {/if}
      <div class="absolute top-0 right-0 z-1 flex gap-2 p-1 opacity-0 group-hover:opacity-100 {transClass}">
        {#if detailsConfig && rsrc.favorite !== undefined}
          <Button
            icon={rsrc.favorite ? icons.starFilled : icons.star}
            class="{rsrc.favorite ? '[&_iconify-icon]:text-yellow-500' : ''} {btnClass}"
            onclick={(event) => {
              event.stopPropagation();
              rsrc.favorite ? unfavorite(rsrc) : favorite(rsrc);
            }}
          />
        {/if}
        {#if rsrc.link && $user?.role === 'admin'}
          <Button
            icon={icons.download}
            class={btnClass}
            onclick={(event) => {
              event.stopPropagation();
              downloadPrompt(rsrc.link);
            }}
          />
        {/if}
        {#if searchButton}
          <Button
            icon={icons.search}
            class={btnClass}
            onclick={(event) => {
              event.stopPropagation();
              globalSearch(rsrc.title);
            }}
          />
        {/if}
      </div>
      <Image src={rsrc.cover} width="100%" {ratio} class="rounded-b-none group-hover:opacity-80 {transClass}" />
      {#if rsrc.category || rsrc.misc}
        <div class="absolute bottom-0 h-8 w-full bg-linear-to-t from-black/50 to-transparent">
          <div class="flex h-full items-end justify-between gap-2 px-2 pb-1 text-xs text-white">
            <span class="whitespace-nowrap">{rsrc.category}</span>
            <span class="truncate" title={rsrc.misc}>{rsrc.misc}</span>
          </div>
        </div>
      {/if}
    </div>
    <div class="@container flex grow flex-col justify-between gap-2 p-2">
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <div
        tabindex="0"
        role="button"
        class="line-clamp-2 text-[clamp(0.75rem,7cqw,0.875rem)] font-medium {pointerClass}"
        title={rsrc.title}
        onclick={() => gotoDetails(rsrc)}
      >
        {rsrc.title}
      </div>
      <Uploader up={rsrc.uploader} at={rsrc.uploaded_at} extra={rsrc.size} />
    </div>
  </div>
{/if}
