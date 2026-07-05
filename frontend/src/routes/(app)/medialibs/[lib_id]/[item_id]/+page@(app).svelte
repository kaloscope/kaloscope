<script lang="ts">
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import { Backdrop, Container, Image, MediaActions, mediaTitle, Rating, VideoPlayer } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { historyBack, user } from '$lib/stores';
  import type { MediaItem, MediaMeta, Resp } from '$lib/types';
  import { buildStreamUrl } from '$lib/utils';
  import { onMount, tick } from 'svelte';

  // the loading state
  const loading = createLoading();

  // the parent media item and its metadata
  let media: MediaItem | null = $state(null);
  let meta: MediaMeta | null = $state(null);

  // the selected child media item and its metadata
  let _media: MediaItem | null = $state(null);
  let _meta: MediaMeta | null = $state(null);

  // the player instance and playing state
  let player: VideoPlayer | null = $state(null);
  let playing = $state(false);

  // the sorted child media items
  let parts: MediaItem[] = $derived.by(() => {
    const items = media?.children;
    if (!items || items.length === 0) {
      return [];
    }
    return items
      .filter((i) => i.visible)
      .sort((a, b) => {
        if (a.season !== b.season) {
          return (a.season ?? 0) - (b.season ?? 0);
        }
        if (a.episode !== b.episode) {
          return (a.episode ?? 0) - (b.episode ?? 0);
        }
        return (a.title ?? a.name).localeCompare(b.title ?? b.name, undefined, {
          numeric: true,
          sensitivity: 'base'
        });
      });
  });

  /**
   * Start playing the selected media item.
   */
  function play() {
    const target = _media ?? media;
    if (!target) {
      return;
    }
    playing = true;
    tick().then(() => {
      const chapters = [];
      if (parts.length) {
        for (const part of parts) {
          chapters.push({
            url: buildStreamUrl(part.path),
            title: mediaTitle(part)
          });
        }
      }
      player?.mount({
        url: buildStreamUrl(target.path),
        back: () => (playing = false),
        title: mediaTitle(target),
        chapters: chapters
      });
    });
  }

  /**
   * Get the media item details by ID.
   *
   * @param id - The media item ID.
   * @return The media item details.
   */
  async function getDetails(id: number): Promise<MediaItem> {
    const resp = await api.get(`media/${id}`).json<Resp<MediaItem>>();
    return resp.data;
  }

  /**
   * Select a child media item and load its details.
   *
   * @param item - The child media item.
   */
  async function selectMedia(item: MediaItem) {
    if (_media?.id === item.id) {
      return;
    }
    try {
      const data = await getDetails(item.id);
      _media = data;
      _meta = data.metadata ?? null;
    } catch (error) {
      console.error(error);
    }
  }

  // load the parent media item details on mount
  onMount(() => {
    loading.start();
    getDetails(Number(page.params.item_id))
      .then((data) => {
        media = data;
        meta = data.metadata ?? null;
      })
      .finally(() => {
        loading.end();
      });
  });
</script>

<svelte:document
  onclick={(event) => {
    // clear the selected child media item when clicking outside
    if (!(event.target as Element).closest('.media-part')) {
      _media = null;
      _meta = null;
    }
  }}
/>

<Container class="pull-to-refresh history-back navbar-hidden" loading={$loading}>
  {#if media}
    <!-- backdrop -->
    <Backdrop
      proxy="store"
      opacity="0.3"
      src={_media?.backdrop ?? media?.backdrop ?? _media?.poster ?? media?.poster}
    />

    <!-- back button -->
    <button
      class="btn absolute top-2 left-2 z-1 btn-circle size-10 bg-blur-80 btn-ghost"
      aria-label="Back"
      onclick={historyBack}
    >
      <iconify-icon icon={icons.backSolid} width="1.25rem" class="opacity-80"></iconify-icon>
    </button>

    <!-- main content -->
    <div class="mx-auto w-full max-w-5xl px-4 py-6 sm:px-6">
      <div class="flex flex-col gap-6 sm:flex-row">
        <!-- poster -->
        <div class="relative self-center sm:self-start">
          <Image proxy="store" src={media?.poster} width="14rem" ratio="2/3" class="shadow-lg" />
          {#if !media.children || media.children.length === 0}
            <div class="absolute inset-0 flex-center">
              <button
                class="group btn btn-circle size-20 btn-enlarge bg-black/30 text-white/60"
                aria-label="Play"
                onclick={play}
              >
                <iconify-icon icon={icons.play} width="2.5rem"> </iconify-icon>
              </button>
            </div>
          {/if}
        </div>

        <div class="flex min-w-0 flex-1 flex-col gap-3">
          <!-- titles -->
          <h1 class="text-2xl font-bold sm:text-3xl">{media?.title ?? media?.name}</h1>
          {#if meta?.originaltitle && meta.originaltitle !== meta.title}
            <h4 class="text-sm opacity-60">{meta.originaltitle}</h4>
          {/if}

          <!-- badges -->
          <div class="flex flex-wrap gap-2">
            {#if media.year}
              <span class="badge badge-outline">{media.year}</span>
            {/if}
            {#if meta?.mpaa}
              <span class="badge badge-outline">{meta.mpaa}</span>
            {/if}
            {#if meta?.country}
              <span class="badge badge-outline">{meta.country}</span>
            {/if}
            <Rating score={media.rating} class="h-6 border" />
          </div>

          <!-- tagline -->
          {#if meta?.tagline}
            <p class="text-sm italic opacity-70">{meta.tagline}</p>
          {/if}

          <!-- genres -->
          {#if meta?.genres?.length}
            <div class="flex flex-wrap gap-1.5">
              {#each meta.genres as genre, i (i)}
                <span class="badge badge-sm opacity-80 badge-primary">{genre}</span>
              {/each}
            </div>
          {/if}

          <!-- plot -->
          {#if _media && _meta?.plot}
            <div class="mt-2 font-semibold text-surface">
              {mediaTitle(_media)}
            </div>
          {/if}
          <p class="mt-1 text-sm leading-relaxed opacity-80">{_meta?.plot ?? meta?.plot}</p>
        </div>
      </div>

      <!-- staff -->
      {#if meta?.directors?.length || meta?.writers?.length || meta?.studios?.length}
        {@const cols = [meta?.directors, meta?.writers, meta?.studios].filter((arr) => arr?.length).length}
        <div class="mt-6 grid gap-3 max-sm:grid-cols-1!" style="grid-template-columns: repeat({cols}, minmax(0, 1fr))">
          {#if meta?.directors?.length}
            <div>
              <span class="font-semibold text-primary/80">{$_('media.director')}</span>
              <p class="text-sm opacity-70">{meta.directors.join(', ')}</p>
            </div>
          {/if}
          {#if meta?.writers?.length}
            <div>
              <span class="font-semibold text-primary/80">{$_('media.writer')}</span>
              <p class="text-sm opacity-70">{meta.writers.join(', ')}</p>
            </div>
          {/if}
          {#if meta?.studios?.length}
            <div>
              <span class="font-semibold text-primary/80">{$_('media.studio')}</span>
              <p class="text-sm opacity-70">{meta.studios.join(', ')}</p>
            </div>
          {/if}
        </div>
      {/if}

      <!-- actors -->
      {#if meta?.actors?.length}
        <div class="mt-6">
          <h2 class="mb-3 text-lg font-semibold">{$_('media.cast')}</h2>
          <div
            class="flex gap-3 overflow-x-auto pb-3"
            onwheel={(event) => {
              event.preventDefault();
              event.currentTarget.scrollLeft += event.deltaY;
            }}
          >
            {#each meta.actors as actor, i (i)}
              <div class="flex w-24 shrink-0 flex-col items-center gap-1 text-center">
                <Image proxy="store" src={actor.thumb} text={actor.name} width="4.5rem" circle />
                <div class="line-clamp-1 text-xs font-medium" title={actor.name}>{actor.name}</div>
                <div class="line-clamp-1 text-xs opacity-50" title={actor.role}>{actor.role}</div>
              </div>
            {/each}
          </div>
        </div>
      {/if}

      <!-- parts -->
      {#if parts.length}
        <div class="mt-6">
          <h2 class="mb-3 text-lg font-semibold">
            {media.lib?.lib_type === 'tv_show' ? $_('media.episodes') : $_('media.parts')}
          </h2>
          <div class="flex max-h-144 flex-col gap-2 overflow-y-scroll px-2 py-3">
            {#each parts as part (part.id)}
              {@const active = _media?.id === part.id}
              {@const activeClass = active ? 'bg-primary/15' : 'bg-gradient hover:bg-base-content/15'}
              {@const transClass = 'transition-colors duration-300'}
              <button
                class="media-part flex items-center rounded-lg px-3 py-2 text-left {transClass} {activeClass}"
                onclick={() => selectMedia(part)}
              >
                <Image proxy="store" src={part.poster} text={part.name} width="5rem" ratio="16/9" />
                <div class="flex min-w-0 flex-1 flex-col gap-0.5 px-3">
                  <span class="truncate text-sm font-medium {transClass}" class:text-primary={active}>
                    {mediaTitle(part)}
                  </span>
                  <span class="text-xs opacity-50">{part.aired}</span>
                </div>
                <!-- svelte-ignore a11y_click_events_have_key_events -->
                <div
                  tabindex="0"
                  role="button"
                  class="btn btn-circle btn-enlarge shadow-sm btn-sm {transClass}"
                  class:btn-active={active}
                  class:btn-subtle={!active}
                  onclick={(event) => {
                    event.stopPropagation();
                    selectMedia(part).then(play);
                  }}
                >
                  <iconify-icon icon={icons.play} width="1.25rem"></iconify-icon>
                </div>
                {#if $user?.role === 'admin'}
                  <MediaActions
                    item={part}
                    class="dropdown-end ml-1"
                    triggerClass="opacity-70"
                    onclick={() => {
                      selectMedia(part);
                    }}
                    ondelete={() => {
                      // refresh the parent media details to update the parts list
                      getDetails(media!.id).then((data) => {
                        media = data;
                      });
                    }}
                  />
                {/if}
              </button>
            {/each}
          </div>
        </div>
      {/if}

      <!-- tags -->
      {#if meta?.tags?.length}
        <div class="mt-6">
          <h2 class="mb-3 text-lg font-semibold">{$_('media.tags')}</h2>
          <div class="flex flex-wrap gap-1.5">
            {#each meta.tags as tag, i (i)}
              <span class="badge badge-soft badge-sm text-base-content/70">{tag}</span>
            {/each}
          </div>
        </div>
      {/if}
    </div>
  {/if}
</Container>

<!-- player overlay -->
{#if playing}
  <div class="fixed inset-0 layer-1 max-sm:bottom-(--ks-dock-h)">
    <VideoPlayer bind:this={player} />
  </div>
{/if}
