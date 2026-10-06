<script lang="ts">
  import { beforeNavigate } from '$app/navigation';
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import {
    Backdrop,
    Container,
    Image,
    ImageViewer,
    MediaActions,
    mediaTitle,
    Rating,
    TextViewer,
    VideoPlayer
  } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { historyBack, user } from '$lib/stores';
  import type {
    BaseResp,
    Chapter,
    ContentChapter,
    MediaContent,
    MediaContentQuery,
    MediaItem,
    MediaMeta,
    Resp
  } from '$lib/types';
  import { buildStreamUrl } from '$lib/utils';
  import { isHTTPError } from 'ky';
  import { onDestroy, onMount, tick } from 'svelte';

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

  // local reading uses the parent entry to retain the comic chapter directory
  let reading = $state(false);
  let readerDialog: HTMLDialogElement | undefined = $state();
  let textViewer: TextViewer | undefined = $state();
  let imageViewer: ImageViewer | undefined = $state();
  let readingLoading = $state(false);
  let readingError = $state<string | null>(null);
  let readingChapterId: string | undefined;
  let readingController: AbortController | undefined;
  const mediaType = $derived.by(() => media?.media_type ?? 'video');

  // novel sections belong to the content index, not child media records
  let textChapters = $state<ContentChapter[]>([]);
  let textVersion = $state<string | undefined>();
  let chaptersLoading = $state(false);
  let chaptersError = $state<string | null>(null);
  let chaptersController: AbortController | undefined;
  const hasTextChapters = $derived(textChapters.length > 1 || textChapters.some((chapter) => !!chapter.title));

  /**
   * Open the local reader at the selected chapter or the first available chapter.
   *
   * @param chapterId - The content chapter ID; omitted for the first chapter.
   * @param version - The novel directory version; omitted for a fresh reading request.
   */
  function read(chapterId?: string, version?: string) {
    if (!media || mediaType === 'video') return;
    reading = true;
    loadContent(chapterId, version);
  }

  /**
   * Format the same section label in the detail page and reader directory.
   *
   * @param chapter - The published section label and split-part number.
   * @param index - The zero-based section position used for missing titles.
   * @returns The localized title with an optional split-part suffix.
   */
  function chapterTitle(chapter: ContentChapter, index: number): string {
    return (
      (chapter.title || $_('media.reader.chapter', index + 1)) +
      (chapter.part > 1 ? ` · ${$_('media.reader.part', chapter.part)}` : '')
    );
  }

  /** Load the novel directory without mounting a reader or retaining its body. */
  async function loadTextChapters() {
    if (!media || mediaType !== 'text') return;
    chaptersController?.abort();
    chaptersController = new AbortController();
    const { signal } = chaptersController;
    chaptersLoading = true;
    chaptersError = null;
    try {
      const data = await getContent(media.id, {}, signal);
      if (signal.aborted) return;
      textChapters = data.chapters;
      textVersion = data.version;
    } catch (error) {
      if (!signal.aborted) chaptersError = contentError(error);
    } finally {
      if (!signal.aborted) chaptersLoading = false;
    }
  }

  /**
   * Extract a localized content error, including a fallback for network failures.
   *
   * @param error - The failed content request.
   * @returns The API error code or the generic reading failure code.
   */
  function contentError(error: unknown): string {
    const response = isHTTPError(error) ? (error.data as BaseResp | undefined) : undefined;
    return response?.message || 'resource_load_failed';
  }

  /** Clear the old content and keep the local return action available. */
  function clearContent() {
    textViewer?.mount({ text: [], back: closeReader });
    imageViewer?.mount({ images: [], back: closeReader });
  }

  /** Cancel pending requests before closing the reading overlay. */
  function closeReader() {
    readingController?.abort();
    reading = false;
  }

  /**
   * Read content through the shared API without a duplicate error notification.
   *
   * @param id - The parent work or standalone reading source ID.
   * @param query - The selected chapter, expected version and optional image range.
   * @param signal - Cancellation for this reading session or image request.
   * @returns The validated kind of content for the current reader.
   */
  async function getContent(id: number, query: MediaContentQuery, signal: AbortSignal): Promise<MediaContent> {
    const { data } = await api
      .get(`media/${id}/content`, {
        searchParams: query,
        signal,
        retry: 0,
        context: { silentErrors: true }
      })
      .json<Resp<MediaContent>>();
    if (data.media_type !== mediaType) {
      throw new Error('Unexpected media type');
    }
    return data;
  }

  /**
   * Replace the chapter and its directory, ignoring responses from cancelled loads.
   *
   * @param selectedId - The requested chapter, or undefined for the first chapter.
   * @param version - The expected novel version; omitted when switching comic sources.
   * @param refresh - Allow one automatic refresh after a content change; defaults to true.
   */
  async function loadContent(selectedId?: string, version?: string, refresh = true): Promise<void> {
    const item = media;
    if (!reading || !item) return;
    readingController?.abort();
    readingController = new AbortController();
    const { signal } = readingController;
    readingChapterId = selectedId;
    readingLoading = true;
    readingError = null;
    clearContent();
    // wait for the conditional viewers before loading the first chapter
    await tick();
    if (signal.aborted) return;
    if (readerDialog && !readerDialog.open) readerDialog.showModal();
    try {
      const data = await getContent(item.id, { chapter_id: selectedId, version }, signal);
      if (signal.aborted) return;
      readingChapterId = data.chapter_id;
      if (data.media_type === 'text') {
        // a fresh reading response supersedes an older directory request
        chaptersController?.abort();
        chaptersLoading = false;
        chaptersError = null;
        textChapters = data.chapters;
        textVersion = data.version;
      }
      const chapters = data.chapters.map((chapter, index) => ({
        id: chapter.id,
        title: chapterTitle(chapter, index),
        volume: chapter.volume
      }));
      const options = {
        title: data.title,
        chapters,
        chapterId: data.chapter_id,
        chapterChange: (chapter: Chapter) =>
          loadContent(chapter.id ?? undefined, data.media_type === 'text' ? data.version : undefined),
        back: closeReader
      };
      if (data.content_type === 'images') {
        imageViewer?.mount({
          ...options,
          images: data.images,
          image_count: data.image_count,
          next_offset: data.next_offset,
          version: data.version,
          signal,
          loadImages: async ({ offset, limit, version, signal }) => {
            try {
              const next = await getContent(item.id, { chapter_id: data.chapter_id, offset, limit, version }, signal);
              if (next.content_type !== 'images') throw new Error('Unexpected content type');
              return next;
            } catch (error) {
              if (!signal.aborted && contentError(error) === 'content_changed') {
                if (refresh) {
                  void loadContent(data.chapter_id, undefined, false);
                } else {
                  readingController?.abort();
                  clearContent();
                  readingError = 'content_changed';
                }
              }
              throw error;
            }
          }
        });
      } else if (data.content_type === 'blocks') {
        textViewer?.mount({ ...options, blocks: data.blocks });
      } else {
        textViewer?.mount({ ...options, text: data.text });
      }
    } catch (error) {
      if (signal.aborted) return;
      if (refresh && contentError(error) === 'content_changed') {
        // novel chapter ids may change with the index; comic item ids stay stable
        await loadContent(mediaType === 'image' ? selectedId : undefined, undefined, false);
      } else {
        readingError = contentError(error);
      }
    } finally {
      if (!signal.aborted) readingLoading = false;
    }
  }

  // the sorted child media items
  let parts: MediaItem[] = $derived.by(() => {
    const items = media?.children;
    if (!items || items.length === 0) {
      return [];
    }
    return items
      .filter((i) => i.visible)
      .sort((a, b) => {
        if (mediaType === 'image') {
          return a.dir.localeCompare(b.dir, undefined, { numeric: true, sensitivity: 'base' }) || a.id - b.id;
        }
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
        chapters: chapters,
        danmakuServer: target.lib?.danmaku_server
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

  beforeNavigate(({ from, to }) => {
    if (from && (from.url.origin !== to?.url.origin || from.url.pathname !== to?.url.pathname)) {
      chaptersController?.abort();
      closeReader();
    }
  });
  onDestroy(closeReader);

  // load the parent media item details on mount
  onMount(() => {
    let active = true;
    loading.start();
    getDetails(Number(page.params.item_id))
      .then((data) => {
        if (!active) return;
        media = data;
        meta = data.metadata ?? null;
        if (data.media_type === 'text') void loadTextChapters();
      })
      .finally(() => {
        loading.end();
      });
    return () => {
      active = false;
      chaptersController?.abort();
    };
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
          {#if !parts.length && !hasTextChapters}
            <div class="absolute inset-0 flex-center">
              <button
                class="group btn btn-circle size-20 btn-enlarge bg-black/30 text-white/60"
                aria-label={$_(mediaType === 'video' ? 'media.play' : 'media.read')}
                onclick={() => (mediaType === 'video' ? play() : read())}
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
            {#if mediaType === 'image'}
              {$_('media.image.chapters')}
            {:else if media.lib?.lib_type === 'tv_show'}
              {$_('media.episodes')}
            {:else}
              {$_('media.parts')}
            {/if}
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
                    {mediaType === 'video' ? mediaTitle(part) : (part.title ?? part.name)}
                  </span>
                  <span class="text-xs opacity-50">{part.aired}</span>
                </div>
                <div
                  tabindex="0"
                  role="button"
                  aria-label={$_(mediaType === 'video' ? 'media.play' : 'media.read')}
                  class="btn btn-circle btn-enlarge shadow-sm btn-sm {transClass}"
                  class:btn-active={active}
                  class:btn-subtle={!active}
                  onclick={(event) => {
                    event.stopPropagation();
                    if (mediaType === 'video') selectMedia(part).then(play);
                    else read(`item:${part.id}`);
                  }}
                  onkeydown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      event.currentTarget.click();
                    }
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

      <!-- novel chapters -->
      {#if mediaType === 'text' && (chaptersLoading || chaptersError || hasTextChapters)}
        <div class="mt-6">
          <h2 class="mb-3 text-lg font-semibold">{$_('media.text.chapters')}</h2>
          {#if chaptersLoading}
            <div class="flex-center gap-3 py-6" role="status">
              <span class="loading loading-sm loading-spinner" aria-hidden="true"></span>
              <span class="text-sm opacity-60">{$_('media.reader.loading_chapters')}</span>
            </div>
          {:else if chaptersError}
            <div class="flex-center flex-col gap-3 py-6">
              <p class="text-sm" role="alert">
                {$_(`alert.${chaptersError}`, { default: $_('alert.resource_load_failed') })}
              </p>
              <button class="btn btn-sm" onclick={loadTextChapters}>{$_('action.retry')}</button>
            </div>
          {:else}
            <div class="flex max-h-144 flex-col gap-2 overflow-y-auto px-2 py-3">
              {#each textChapters as chapter, index (chapter.id)}
                <button
                  class="flex items-center gap-3 rounded-lg bg-gradient px-3 py-3 text-left transition-colors hover:bg-base-content/15"
                  title={chapterTitle(chapter, index)}
                  onclick={() => read(chapter.id, textVersion)}
                >
                  <span class="w-8 shrink-0 text-center text-sm tabular-nums opacity-50">{index + 1}</span>
                  <span class="min-w-0 flex-1 truncate text-sm font-medium">{chapterTitle(chapter, index)}</span>
                  <span class="btn btn-circle btn-enlarge btn-subtle shadow-sm transition-colors duration-300 btn-sm">
                    <iconify-icon icon={icons.play} width="1.25rem" aria-hidden="true"></iconify-icon>
                  </span>
                </button>
              {/each}
            </div>
          {/if}
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

<!-- reader overlay -->
{#if reading && media && (mediaType === 'text' || mediaType === 'image')}
  <dialog
    bind:this={readerDialog}
    class="fixed inset-0 m-0 h-dvh max-h-none w-screen max-w-none border-0 p-0"
    aria-label={$_('media.read')}
    oncancel={(event) => {
      event.preventDefault();
      closeReader();
    }}
  >
    <div
      inert={readingLoading || !!readingError}
      aria-hidden={readingLoading || !!readingError}
      class:hidden={readingLoading || !!readingError}
    >
      {#if mediaType === 'text'}
        <TextViewer bind:this={textViewer} />
      {:else}
        <ImageViewer bind:this={imageViewer} />
      {/if}
    </div>
    {#if readingLoading || readingError}
      <div class="absolute inset-0 layer-3 flex-center flex-col gap-5 bg-base-100 p-6 text-center">
        <button class="btn absolute top-2 left-2 btn-circle btn-ghost" aria-label="Back" onclick={closeReader}>
          <iconify-icon icon={icons.backSolid} width="1.25rem"></iconify-icon>
        </button>
        {#if readingLoading}
          <span class="loading loading-lg loading-bars" role="status" aria-label={$_('media.reader.loading')}></span>
        {:else if readingError}
          <p role="alert">{$_(`alert.${readingError}`, { default: $_('alert.resource_load_failed') })}</p>
          <button class="btn btn-primary" onclick={() => loadContent(readingChapterId)}>{$_('action.retry')}</button>
        {/if}
      </div>
    {/if}
  </dialog>
{/if}
