<script lang="ts" module>
  // retain request order across detail page instances
  let historyWrite = Promise.resolve();
  const PROGRESS_SAVE_DELAY = 5000;
</script>

<script lang="ts">
  import { beforeNavigate } from '$app/navigation';
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import {
    alert,
    Alerts,
    Backdrop,
    Container,
    Image,
    ImageViewer,
    MediaActions,
    mediaTitle,
    Rating,
    TextViewer,
    VideoPlayer,
    type ImageViewerOptions,
    type TextViewerOptions
  } from '$lib/components';
  import { LibType } from '$lib/enums';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { historyBack, user } from '$lib/stores';
  import type {
    BaseResp,
    Chapter,
    ContentChapter,
    ImageLocator,
    MediaContent,
    MediaContentQuery,
    MediaItem,
    MediaMeta,
    Page,
    ReadingEntry,
    ReadingHistory,
    Resp,
    TextLocator,
    WatchHistory
  } from '$lib/types';
  import { buildStreamUrl } from '$lib/utils';
  import { isHTTPError } from 'ky';
  import { onDestroy, onMount, tick } from 'svelte';
  import { get } from 'svelte/store';

  // the loading state
  const loading = createLoading();

  // the parent media item and its metadata
  let media: MediaItem | null = $state(null);
  let meta: MediaMeta | null = $state(null);
  const posterIcon = $derived.by(() => (media?.lib ? LibType[media.lib.lib_type].icon : undefined));

  // the selected child media item and its metadata
  let _media: MediaItem | null = $state(null);
  let _meta: MediaMeta | null = $state(null);

  // the player instance and playing state
  let player: VideoPlayer | null = $state(null);
  let playing = $state(false);
  let watchHistory = $state<WatchHistory | null>(null);
  let watchLoading = $state(false);
  let watchController: AbortController | undefined;
  let active = false;

  // local reading uses the parent entry to retain the comic chapter directory
  let reading = $state(false);
  let readerDialog: HTMLDialogElement | undefined = $state();
  let textViewer: TextViewer | undefined = $state();
  let imageViewer: ImageViewer | undefined = $state();
  let readingLoading = $state(false);
  let readingError = $state<string | null>(null);
  let hasReadingHistory = $state(false);
  let readingChapterId: string | undefined;
  let readingResume = false;
  let readingController: AbortController | undefined;
  let progressTimer: ReturnType<typeof setTimeout> | undefined;
  let pendingProgress: ReadingEntry | undefined;
  let progressUserId: number | undefined;
  let lastQueuedProgress = '';
  let captureProgress: (() => void) | undefined;
  const mediaType = $derived.by(() => media?.media_type ?? 'video');

  // novel sections belong to the content index, not child media records
  let textChapters = $state<ContentChapter[]>([]);
  let textVersion = $state<string | undefined>();
  let chaptersLoading = $state(false);
  let chaptersError = $state<string | null>(null);
  let chaptersController: AbortController | undefined;
  const hasTextChapters = $derived(textChapters.length > 1 || textChapters.some((chapter) => !!chapter.title));

  /**
   * Open the local reader, resuming saved progress only in the selected chapter.
   *
   * @param chapterId - The requested chapter; omitted to continue the last chapter.
   * @param version - The novel directory version; omitted for a fresh reading request.
   */
  function read(chapterId?: string, version?: string) {
    if (!media || mediaType === 'video') return;
    reading = true;
    loadContent(chapterId, version, { resume: true });
  }

  /** Save the latest position in request order, including before leaving the page. */
  function saveProgress() {
    captureProgress?.();
    clearTimeout(progressTimer);
    progressTimer = undefined;
    const entry = pendingProgress;
    pendingProgress = undefined;
    if (!entry) return;
    const owner = progressUserId;
    const key = JSON.stringify(entry);
    lastQueuedProgress = key;
    historyWrite = historyWrite.then(async () => {
      const current = get(user);
      if (!owner || current?.id !== owner || current.preferences?.read_records === 0) return;
      try {
        await api.post('user/history/record', {
          json: entry,
          retry: 0,
          keepalive: true,
          context: { silentErrors: true }
        });
        if (media?.id === entry.rel_id) {
          hasReadingHistory = true;
        }
      } catch {
        if (lastQueuedProgress === key) lastQueuedProgress = '';
        if (get(user)?.id !== owner) return;
        alert({ level: 'warning', message: 'reading_progress_save_failed', unique: true });
      }
    });
  }

  /**
   * Read retained progress through the shared user history endpoint.
   *
   * @param id - The novel or comic's top-level media ID.
   * @param signal - Cancellation for the directory or reading request.
   * @returns The current account's work history, or null without a saved record.
   */
  async function getReadingHistory(id: number, signal: AbortSignal): Promise<ReadingHistory | null> {
    if (!$user || $user.preferences?.read_records === 0 || mediaType === 'video') return null;
    const { data } = await api
      .get('user/history/list', {
        searchParams: { rel_type: mediaType, rel_id: id },
        signal,
        retry: 0,
        context: { silentErrors: true }
      })
      .json<Resp<Page<ReadingHistory>>>();
    return data.items[0] ?? null;
  }

  /**
   * Refresh the latest retained video or episode through the shared history API.
   *
   * @returns The latest accessible entry, or null when unavailable.
   */
  async function loadWatchHistory(): Promise<WatchHistory | null> {
    watchController?.abort();
    const item = media;
    const owner = get(user)?.id;
    if (!active || !item || mediaType !== 'video' || !owner || get(user)?.preferences?.watch_records === 0) {
      watchHistory = null;
      watchLoading = false;
      return null;
    }
    const controller = new AbortController();
    watchController = controller;
    watchLoading = true;
    try {
      const { data } = await api
        .get('user/history/list', {
          searchParams: {
            rel_type: 'video',
            ...(parts.length ? { parent_id: item.id } : { rel_id: item.id }),
            page_size: 1,
            ordering: '-updated_at'
          },
          signal: controller.signal,
          retry: 0
        })
        .json<Resp<Page<WatchHistory>>>();
      if (controller.signal.aborted || !active || get(user)?.id !== owner) return null;
      watchHistory = data.items[0] ?? null;
      return watchHistory;
    } catch (error) {
      if (!controller.signal.aborted) console.error(error);
      return null;
    } finally {
      if (watchController === controller) watchLoading = false;
    }
  }

  /** Resume the latest accessible video after refreshing its saved position. */
  async function continuePlay() {
    const history = await loadWatchHistory();
    if (history?.media) play(history.media, history.position ?? 0);
  }

  /**
   * Format a publication date up to its available precision.
   *
   * @param metadata - The current work or chapter metadata.
   * @returns The date through the last consecutive part, or null without a year.
   */
  function publicationDate(metadata: MediaMeta): string | null {
    if (!metadata.year) return null;
    let value = String(metadata.year).padStart(4, '0');
    if (metadata.month) {
      value += `-${String(metadata.month).padStart(2, '0')}`;
      if (metadata.day) value += `-${String(metadata.day).padStart(2, '0')}`;
    }
    return value;
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
      (chapter.title || $_('media.chapter', index + 1)) +
      (chapter.part > 1 ? ` · ${$_('media.part', chapter.part)}` : '')
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
    saveProgress();
    captureProgress = undefined;
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
   * @param options - Allow one content refresh by default, and optionally resume saved progress.
   */
  async function loadContent(
    selectedId?: string,
    version?: string,
    { refresh = true, resume = false }: { refresh?: boolean; resume?: boolean } = {}
  ): Promise<void> {
    const item = media;
    const readerUserId = get(user)?.id;
    if (!reading || !item) return;
    saveProgress();
    captureProgress = undefined;
    readingController?.abort();
    readingController = new AbortController();
    const { signal } = readingController;
    readingChapterId = selectedId;
    readingResume = resume;
    readingLoading = true;
    readingError = null;
    clearContent();
    // wait for the conditional viewers before loading the first chapter
    await tick();
    if (signal.aborted) return;
    if (readerDialog && !readerDialog.open) readerDialog.showModal();
    let locator: TextLocator | ImageLocator | undefined;
    let positionReset = false;
    try {
      if (resume) {
        // reopening immediately after exit must wait for that exit's save
        await historyWrite;
        if (signal.aborted) return;
        try {
          const history = await getReadingHistory(item.id, signal);
          if (history?.rel_type === mediaType) {
            const saved = history.locator;
            const savedChapter = saved
              ? 'chapter_id' in saved
                ? saved.chapter_id
                : `item:${saved.chapter_item_id ?? item.id}`
              : undefined;
            if (saved && (!selectedId || selectedId === savedChapter)) {
              locator = saved;
              positionReset = history.percentage === null;
            } else if (!saved && !selectedId) {
              positionReset = true;
            }
          }
        } catch {
          if (!signal.aborted) alert({ level: 'warning', message: 'reading_history_load_failed', unique: true });
        }
      }
      if (signal.aborted) return;
      const textLocator = locator && 'chapter_id' in locator ? locator : undefined;
      const imageLocator = locator && 'page_id' in locator ? locator : undefined;
      const data = await getContent(
        item.id,
        {
          chapter_id:
            textLocator?.chapter_id ?? (imageLocator ? `item:${imageLocator.chapter_item_id ?? item.id}` : selectedId),
          version: locator?.version ?? version,
          page_id: imageLocator?.page_id
        },
        signal
      );
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
      const queueProgress = (entry: ReadingEntry) => {
        const current = get(user);
        if (signal.aborted || !reading || current?.id !== readerUserId || current?.preferences?.read_records === 0)
          return;
        pendingProgress = JSON.stringify(entry) === lastQueuedProgress ? undefined : entry;
        if (pendingProgress && progressTimer === undefined) {
          progressTimer = setTimeout(saveProgress, PROGRESS_SAVE_DELAY);
        }
      };
      // restore only after the dialog's reading area is visible and measurable
      readingLoading = false;
      await tick();
      if (signal.aborted) return;
      progressUserId = readerUserId;
      if (data.content_type === 'images') {
        const progress: NonNullable<ImageViewerOptions['progress']> = (position) => {
          // local content URLs carry the indexed page ID in the final path segment
          const pageId = new URL(position.url, window.location.origin).pathname.split('/').at(-1)!;
          queueProgress({
            rel_type: 'image',
            rel_id: item.id,
            percentage: Math.floor(position.percentage),
            locator: {
              version: data.version,
              chapter_item_id: data.source_item_id === item.id ? undefined : data.source_item_id,
              page_id: pageId,
              offset: position.offset
            }
          });
        };
        await imageViewer?.mount({
          ...options,
          images: data.images,
          offset: data.offset,
          image_count: data.image_count,
          next_offset: data.next_offset,
          version: data.version,
          position: { index: data.offset, offset: imageLocator?.offset ?? 0 },
          progress,
          signal,
          loadImages: async ({ offset, limit, version, signal }) => {
            try {
              const next = await getContent(item.id, { chapter_id: data.chapter_id, offset, limit, version }, signal);
              if (next.content_type !== 'images') throw new Error('Unexpected content type');
              return next;
            } catch (error) {
              if (!signal.aborted && contentError(error) === 'content_changed') {
                if (refresh) {
                  void loadContent(data.chapter_id, undefined, { refresh: false, resume: true });
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
        if (signal.aborted) return;
        captureProgress = () => {
          const position = imageViewer?.getPosition();
          if (position) progress(position);
        };
      } else {
        const index = textLocator
          ? data.content_type === 'blocks'
            ? data.blocks.findIndex((block) => block.id === textLocator.block_id)
            : (textLocator.paragraph ?? -1)
          : -1;
        const progress: NonNullable<TextViewerOptions['progress']> = (position) => {
          const chapterIndex = data.chapters.findIndex((chapter) => chapter.id === data.chapter_id);
          const blockId = data.content_type === 'blocks' ? data.blocks[position.index]?.id : undefined;
          if (data.content_type === 'blocks' && !blockId) return;
          const base = { version: data.version, chapter_id: data.chapter_id, offset: position.offset };
          const anchor: TextLocator =
            data.content_type === 'blocks' ? { ...base, block_id: blockId! } : { ...base, paragraph: position.index };
          queueProgress({
            rel_type: 'text',
            rel_id: item.id,
            percentage: Math.floor(((chapterIndex + position.percentage / 100) / data.chapters.length) * 100),
            locator: anchor
          });
        };
        await textViewer?.mount({
          ...options,
          ...(data.content_type === 'blocks' ? { blocks: data.blocks } : { text: data.text }),
          position: index >= 0 ? { index, offset: textLocator?.offset ?? 0 } : undefined,
          progress
        });
        if (signal.aborted) return;
        captureProgress = () => {
          const position = textViewer?.getPosition();
          if (position) progress(position);
        };
      }
      if (positionReset) alert({ level: 'info', message: 'reading_position_reset', unique: true });
    } catch (error) {
      if (signal.aborted) return;
      if (refresh && contentError(error) === 'content_changed') {
        // novel chapter ids may change with the index; comic item ids stay stable
        await loadContent(mediaType === 'image' ? selectedId : undefined, undefined, {
          refresh: false,
          resume: !!locator
        });
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
   *
   * @param target - The video to open; defaults to the selected item or the work.
   * @param startTime - The saved position in seconds; omitted to play from the start.
   */
  function play(target = _media ?? media, startTime?: number) {
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
        back: () => {
          watchLoading = true;
          playing = false;
        },
        title: mediaTitle(target),
        startTime,
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

  /**
   * Reload the work and the saved comic chapter after scraping.
   *
   * @param id - The media item whose metadata was saved.
   */
  async function refreshMetadata(id: number) {
    if (!media) return;
    const rootId = media.id;
    const [root, selected] = await Promise.all([
      getDetails(rootId),
      id === rootId ? Promise.resolve(null) : getDetails(id)
    ]);
    if (media?.id !== rootId) return;
    media = root;
    meta = root.metadata ?? null;
    _media = selected;
    _meta = selected?.metadata ?? null;
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
    active = true;
    const historyController = new AbortController();
    loading.start();
    getDetails(Number(page.params.item_id))
      .then((data) => {
        if (!active) return;
        media = data;
        meta = data.metadata ?? null;
        if (data.media_type === 'video') {
          void loadWatchHistory();
        } else {
          void getReadingHistory(data.id, historyController.signal)
            .then((history) => {
              if (active) hasReadingHistory = !!history;
            })
            .catch(() => {});
        }
        if (data.media_type === 'text') void loadTextChapters();
      })
      .finally(() => {
        loading.end();
      });
    return () => {
      active = false;
      watchController?.abort();
      historyController.abort();
      chaptersController?.abort();
    };
  });
</script>

<svelte:document
  onvisibilitychange={() => {
    if (document.visibilityState === 'hidden') saveProgress();
  }}
  onclick={(event) => {
    // clear the selected child media item when clicking outside
    if (!(event.target as Element).closest('.media-part')) {
      _media = null;
      _meta = null;
    }
  }}
/>

<svelte:window onpagehide={saveProgress} />

<Container class="pull-to-refresh history-back navbar-hidden" loading={$loading}>
  {#if media}
    <!-- selected chapter details already include server-side inheritance -->
    {@const readingMeta = mediaType === 'video' ? null : _media ? _meta : meta}

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
          <Image proxy="store" src={media?.poster} icon={posterIcon} width="14rem" ratio="2/3" class="shadow-lg" />
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
          <div class="flex items-start gap-2">
            <h1 class="min-w-0 flex-1 text-2xl font-bold sm:text-3xl">{media?.title ?? media?.name}</h1>
            {#if $user?.role === 'admin' && (mediaType !== 'video' || !media.parent)}
              <MediaActions item={media} class="dropdown-end" onscrape={() => refreshMetadata(media!.id)} />
            {/if}
          </div>
          {#if readingMeta && (readingMeta.series || readingMeta.volume || readingMeta.number)}
            <p class="font-medium wrap-break-word opacity-70">
              {#if readingMeta.series}{readingMeta.series}{/if}
              {#if readingMeta.volume}
                {#if readingMeta.series}
                  ·
                {/if}
                {$_('metadata.volume', readingMeta.volume)}
              {/if}
              {#if readingMeta.number}
                {#if readingMeta.series || readingMeta.volume}
                  ·
                {/if}
                {#if mediaType === 'text'}
                  {$_('metadata.series_index', readingMeta.number)}
                {:else if Number.isFinite(Number(readingMeta.number))}
                  {$_('metadata.issue', readingMeta.number)}
                {:else}
                  {readingMeta.number}
                {/if}
              {/if}
            </p>
          {/if}
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

          <!-- continue playback or reading -->
          {#if mediaType === 'video' ? watchHistory?.media : hasReadingHistory}
            <button
              class="btn mt-2 h-9 w-full gap-1.5 rounded-full px-4 font-medium shadow-sm btn-primary sm:w-fit"
              disabled={watchLoading}
              onclick={() => (mediaType === 'video' ? continuePlay() : read())}
            >
              <iconify-icon icon={icons.playFilled} width="1rem" aria-hidden="true"></iconify-icon>
              {$_(mediaType === 'video' ? 'media.continue_play' : 'media.continue_read')}
            </button>
          {/if}
        </div>
      </div>

      <!-- reading metadata -->
      {#if readingMeta}
        {@const authors = readingMeta.authors?.join(', ')}
        {@const illustrators = readingMeta.illustrators?.join(', ')}
        {@const cols = [authors, illustrators, readingMeta.publisher].filter(Boolean).length}
        {@const published = publicationDate(readingMeta)}
        {@const facts = [
          published ? $_('metadata.published', published) : null,
          readingMeta.language,
          mediaType === 'image' && readingMeta.page_count != null
            ? $_('metadata.page_count', readingMeta.page_count)
            : null,
          mediaType === 'image' && readingMeta.black_and_white != null
            ? $_(readingMeta.black_and_white ? 'metadata.black_and_white' : 'metadata.full_color')
            : null
        ]
          .filter(Boolean)
          .join(' · ')}
        {#if cols || facts || readingMeta.isbn}
          <div class="mt-6 space-y-3">
            {#if cols}
              <dl class="grid gap-3 max-sm:grid-cols-1!" style="grid-template-columns: repeat({cols}, minmax(0, 1fr))">
                {#if authors}
                  <div class="min-w-0">
                    <dt class="font-semibold text-primary/80">{$_('metadata.fields.authors')}</dt>
                    <dd class="text-sm wrap-break-word whitespace-pre-wrap opacity-70">{authors}</dd>
                  </div>
                {/if}
                {#if illustrators}
                  <div class="min-w-0">
                    <dt class="font-semibold text-primary/80">{$_('metadata.fields.illustrators')}</dt>
                    <dd class="text-sm wrap-break-word whitespace-pre-wrap opacity-70">{illustrators}</dd>
                  </div>
                {/if}
                {#if readingMeta.publisher}
                  <div class="min-w-0">
                    <dt class="font-semibold text-primary/80">{$_('metadata.fields.publisher')}</dt>
                    <dd class="text-sm wrap-break-word whitespace-pre-wrap opacity-70">{readingMeta.publisher}</dd>
                  </div>
                {/if}
              </dl>
            {/if}
            {#if facts}
              <p class="text-sm leading-relaxed wrap-break-word opacity-60">{facts}</p>
            {/if}
            {#if readingMeta.isbn}
              <p class="text-xs wrap-break-word opacity-50">{$_('metadata.fields.isbn')} {readingMeta.isbn}</p>
            {/if}
          </div>
        {/if}
      {/if}

      <!-- staff -->
      {#if meta?.directors?.length || meta?.writers?.length || meta?.studios?.length}
        {@const cols = [meta?.directors, meta?.writers, meta?.studios].filter((arr) => arr?.length).length}
        <div class="mt-6 grid gap-3 max-sm:grid-cols-1!" style="grid-template-columns: repeat({cols}, minmax(0, 1fr))">
          {#if meta?.directors?.length}
            <div>
              <span class="font-semibold text-primary/80">{$_('metadata.fields.directors')}</span>
              <p class="text-sm opacity-70">{meta.directors.join(', ')}</p>
            </div>
          {/if}
          {#if meta?.writers?.length}
            <div>
              <span class="font-semibold text-primary/80">{$_('metadata.fields.writers')}</span>
              <p class="text-sm opacity-70">{meta.writers.join(', ')}</p>
            </div>
          {/if}
          {#if meta?.studios?.length}
            <div>
              <span class="font-semibold text-primary/80">{$_('metadata.fields.studios')}</span>
              <p class="text-sm opacity-70">{meta.studios.join(', ')}</p>
            </div>
          {/if}
        </div>
      {/if}

      <!-- actors -->
      {#if meta?.actors?.length}
        <div class="mt-6">
          <h2 class="mb-3 text-lg font-semibold">{$_('metadata.fields.actors')}</h2>
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
                <Image proxy="store" src={part.poster} icon={posterIcon} width="5rem" ratio="16/9" />
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
                    if (mediaType === 'video') selectMedia(part).then(() => play());
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
                    onscrape={mediaType === 'image' ? () => refreshMetadata(part.id) : undefined}
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
              <span class="text-sm opacity-60">{$_('media.loading_chapters')}</span>
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
          <h2 class="mb-3 text-lg font-semibold">{$_('metadata.fields.tags')}</h2>
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
    <VideoPlayer bind:this={player} onhistory={() => void loadWatchHistory()} />
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
          <span class="loading loading-lg loading-bars" role="status" aria-label={$_('media.loading')}></span>
        {:else if readingError}
          <p role="alert">{$_(`alert.${readingError}`, { default: $_('alert.resource_load_failed') })}</p>
          <button
            class="btn btn-primary"
            onclick={() => loadContent(readingChapterId, undefined, { resume: readingResume })}
          >
            {$_('action.retry')}
          </button>
        {/if}
      </div>
    {/if}
    <Alerts dialog />
  </dialog>
{/if}
