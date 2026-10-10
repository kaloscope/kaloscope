<script lang="ts" module>
  import { persisted } from '$lib/stores';
  import type { Chapter, ChapterGroup } from '$lib/types';

  /** Delay in ms before auto-hiding the overlay controls. */
  const CONTROLS_HIDE_DELAY = 3000;
  /** Click-zone threshold ratio in paged mode. The complementary zone is `1 - threshold`. */
  const CLICK_ZONE_THRESHOLD = 0.3;
  /** Distance from either end of the scroll container (px) to load more images. */
  const SCROLL_LOAD_THRESHOLD = 400;
  /** Maximum retries for a failed image request. */
  const MAX_IMAGE_RETRY = 3;
  /** Number of images requested in each batch. */
  const IMAGE_BATCH_SIZE = 20;

  /** A batch of images, with an optional explicit continuation offset. */
  export type ImagePage = {
    images: string[];
    /** Absolute index of the first image; omitted for a batch starting at zero. */
    offset?: number;
    image_count?: number | null;
    /** Omit to use the end of the loaded range, or pass null at the end. */
    next_offset?: number | null;
  };

  /** The current image and its relative vertical position. */
  export type ImagePosition = { index: number; url: string; offset: number; percentage: number };

  /** Options passed to the image viewer mount function. */
  export type ImageViewerOptions = ImagePage & {
    title?: string | null;
    chapters?: Chapter[];
    chapterId?: string | null;
    chapterChange?: (chapter: Chapter) => void;
    back?: () => void;
    version?: string;
    position?: Pick<ImagePosition, 'index' | 'offset'>;
    progress?: (position: ImagePosition) => void;
    /** Cancels pagination when the caller leaves or replaces the resource. */
    signal?: AbortSignal;
    loadImages?: (query: {
      offset: number;
      limit: number;
      version?: string;
      signal: AbortSignal;
    }) => Promise<ImagePage>;
  };

  /** Reading mode. */
  export type ReadMode = 'scroll' | 'paged';
  /** Zoom mode for images. */
  export type ZoomMode = 'auto' | 'width' | 'height';
  /** Page-turning direction in paged reading mode. */
  export type PageDirection = 'right' | 'left' | 'bottom';

  /** Persisted settings. */
  export type ImageViewerSettings = {
    readMode: ReadMode;
    zoomMode: ZoomMode;
    pageDirection: PageDirection;
    sortDesc: boolean;
  };

  const settings = persisted<ImageViewerSettings>('image-viewer', {
    readMode: 'scroll',
    zoomMode: 'auto',
    pageDirection: 'right',
    sortDesc: false
  });

  const ZOOM_MODES: Record<ZoomMode, { class: string }> = {
    auto: { class: 'max-h-full max-w-full object-contain mx-auto' },
    width: { class: 'w-full h-auto' },
    height: { class: 'h-full w-auto max-w-none mx-auto' }
  };

  /**
   * Check whether two chapter ids refer to the same chapter.
   *
   * @param left - The first chapter id.
   * @param right - The second chapter id.
   * @returns Whether the ids match.
   */
  function matchChapterId(left: string | null | undefined, right: string | null | undefined) {
    return !!left && left === right;
  }

  /**
   * Group chapters by volume when every chapter has a volume.
   *
   * @param chapters - The chapters to group.
   * @returns Ordered chapter groups.
   */
  function groupChapters(chapters: Chapter[]): ChapterGroup[] {
    const grouped = chapters.length > 0 && chapters.every((chapter) => !!chapter.volume?.trim());
    if (!grouped) {
      return [{ volume: null, chapters }];
    }
    return chapters.reduce<ChapterGroup[]>((groups, chapter) => {
      const volume = chapter.volume!.trim();
      const group = groups.find((group) => group.volume === volume);
      if (group) {
        group.chapters.push(chapter);
      } else {
        groups.push({ volume, chapters: [chapter] });
      }
      return groups;
    }, []);
  }
</script>

<script lang="ts">
  import { proxyImage } from '$lib/api';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { freeze, historyBack } from '$lib/stores';
  import { onMount, tick } from 'svelte';
  import { fade, fly } from 'svelte/transition';

  // resource title
  let title = $state('');
  // loaded image urls
  let images = $state<string[]>([]);
  // available chapters
  let chapters = $state<Chapter[]>([]);
  // chapters grouped by volume in source order
  let chapterGroups = $derived(groupChapters(chapters));
  // currently active chapter id from url or selection
  let chapterId = $state<string | null>(null);
  // index of the current chapter within the chapters array
  let chapterIndex = $derived(chapters.findIndex((c) => matchChapterId(c.id, chapterId)));
  // previous chapter in sequence, or null if at the first
  let previousChapter = $derived(chapterIndex > 0 ? chapters[chapterIndex - 1] : null);
  // next chapter in sequence, or null if at the last
  let nextChapter = $derived(
    chapterIndex >= 0 && chapterIndex < chapters.length - 1 ? chapters[chapterIndex + 1] : null
  );
  // callback to notify parent of chapter change
  let chapterChange = $state<((c: Chapter) => void) | undefined>(undefined);
  // callback to return from a local reading overlay
  let back = $state<(() => void) | undefined>(undefined);
  // display title, resource title or current chapter title
  let currentTitle = $derived(title || (chapterIndex >= 0 ? chapters[chapterIndex] : null)?.title);
  // whether the chapter list is displayed in descending order
  let sortDesc = $derived($settings?.sortDesc ?? false);

  // whether the settings panel is open
  let settingsOpen = $state(false);
  // whether the chapters menu is open
  let chaptersOpen = $state(false);
  // whether the overlay controls are visible
  let controlsVisible = $state(true);

  // the scroll container element
  let scrollEl = $state<HTMLDivElement | undefined>(undefined);
  let contentEl = $state<HTMLDivElement | undefined>(undefined);
  let pagedImage = $state<HTMLImageElement | undefined>(undefined);
  // image elements used to track the scroll position
  let imageEls = $state<HTMLImageElement[]>([]);
  // absolute image indices, including pages before the loaded range
  let startOffset = $state(0);
  let imageIndex = $state(0);
  let anchor: ImageViewerOptions['position'];
  let progress: ImageViewerOptions['progress'];
  let restoring = false;
  let progressFrame = 0;
  let restoreFrame = 0;
  // total number of images available from the api
  let imageCount = $state(0);
  // continuation offset supplied by the source or derived from loaded images
  let nextOffset = $state<number | null>(null);
  // source-specific pagination supplied by the caller
  let loadImages = $state<ImageViewerOptions['loadImages']>(undefined);
  // version and cancellation shared by the current resource's requests
  let version: string | undefined;
  let readController: AbortController | undefined;
  let readSignal: AbortSignal | undefined;
  // recreate image elements when replacing the current resource
  let mountId = $state(0);
  // whether more images can be loaded
  let hasMore = $derived(!!loadImages && nextOffset !== null);
  let hasPrevious = $derived(!!loadImages && startOffset > 0);
  // whether a network request is in progress
  let loading = $state(false);
  // whether pagination needs an explicit retry
  let loadFailed = $state(false);
  let failedBefore = false;
  // whether an image is currently loading
  let imageLoading = $state(false);

  // css class for the current zoom mode
  let zoomClass = $derived(ZOOM_MODES[$settings?.zoomMode ?? 'width'].class);
  // direction of the page turn animation, forward or backward
  let animForward = $state(true);
  // fly transition parameters for the current page direction
  let flyParams = $derived.by(() => {
    const direction = $settings?.pageDirection ?? 'right';
    if (direction === 'bottom') {
      return { y: animForward ? 200 : -200, duration: 200 };
    }
    const fromRight = direction === 'right' ? animForward : !animForward;
    return { x: fromRight ? 200 : -200, duration: 200 };
  });

  /**
   * Mount the image viewer with the given image resource.
   *
   * @param options - The image viewer options.
   */
  export async function mount(options: ImageViewerOptions) {
    if (!options) {
      return;
    }
    readController?.abort();
    const current = ++mountId;
    cancelAnimationFrame(progressFrame);
    cancelAnimationFrame(restoreFrame);
    progressFrame = 0;
    restoring = true;
    progress = options.progress;
    readController = new AbortController();
    readSignal = options.signal ? AbortSignal.any([readController.signal, options.signal]) : readController.signal;
    version = options.version;
    loadImages = options.loadImages;
    title = options.title ?? '';
    startOffset = options.offset ?? 0;
    images = [];
    appendImages(options.images);
    const end = startOffset + images.length;
    imageCount = Math.max(options.image_count ?? 0, end);
    nextOffset = options.next_offset === undefined ? (end < imageCount ? end : null) : options.next_offset;
    if (!images.length || !loadImages || nextOffset === null) {
      nextOffset = null;
      imageCount = end;
    }
    chapters = options.chapters ?? [];
    chapterId = options.chapterId ?? null;
    chapterChange = options.chapterChange;
    back = options.back;
    imageIndex = Math.max(startOffset, Math.min(options.position?.index ?? startOffset, end - 1));
    anchor = { index: imageIndex, offset: options.position?.offset ?? 0 };
    imageEls = [];
    imageLoading = images.length > 0;
    loading = false;
    loadFailed = false;
    scrollEl?.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    showControls();
    await tick();
    if (current === mountId) restorePosition();
  }

  /**
   * Get the visible image and its relative position within the current chapter.
   *
   * @returns The current position, or null while the viewer is unavailable.
   */
  export function getPosition(): ImagePosition | null {
    if (!scrollEl?.clientHeight || restoring || readSignal?.aborted) return null;
    const paged = $settings?.readMode === 'paged';
    if (paged && imageLoading) return null;
    if (!paged) updateImageIndex();
    const image = paged ? pagedImage : imageEls[imageIndex - startOffset];
    const url = images[imageIndex - startOffset];
    if (!image || !url) return null;
    const offset = Math.max(0, Math.min(1, (scrollEl.scrollTop - image.offsetTop) / Math.max(1, image.offsetHeight)));
    const end = scrollEl.scrollHeight - scrollEl.clientHeight;
    const finished = nextOffset === null && scrollEl.scrollTop >= end - 1;
    const percentage = !paged && finished ? 100 : ((imageIndex + (paged ? 1 : offset)) / imageCount) * 100;
    return { index: imageIndex, url, offset: Math.round(offset * 1e6) / 1e6, percentage };
  }

  /** Preserve the current page when images load, the viewport resizes or modes change. */
  function restorePosition() {
    const image = $settings?.readMode === 'paged' ? pagedImage : anchor && imageEls[anchor.index - startOffset];
    if (!scrollEl || !image || !anchor) return;
    restoring = true;
    cancelAnimationFrame(progressFrame);
    progressFrame = 0;
    cancelAnimationFrame(restoreFrame);
    scrollEl.scrollTo({
      top: image.offsetTop + image.offsetHeight * anchor.offset,
      behavior: 'instant'
    });
    restoreFrame = requestAnimationFrame(() => {
      restoring = false;
      handleImageScroll();
    });
  }

  /** Report at most one position per frame; persistence belongs to the caller. */
  function reportProgress() {
    if (restoring || progressFrame) return;
    progressFrame = requestAnimationFrame(() => {
      progressFrame = 0;
      const position = getPosition();
      if (!position) return;
      anchor = position;
      progress?.(position);
    });
  }

  $effect(() => {
    if (!contentEl || !scrollEl) return;
    const observer = new ResizeObserver(restorePosition);
    observer.observe(contentEl);
    observer.observe(scrollEl);
    if (pagedImage) observer.observe(pagedImage);
    return () => observer.disconnect();
  });

  /**
   * Select a chapter and notify the parent page.
   *
   * @param chapter - The chapter to select.
   */
  function selectChapter(chapter: Chapter) {
    chaptersOpen = false;
    chapterId = chapter.id ?? null;
    chapterChange?.(chapter);
    readController?.abort();
  }

  /**
   * Toggle the persisted chapter list display order.
   */
  function toggleSort() {
    if ($settings !== null) {
      $settings.sortDesc = !($settings.sortDesc ?? false);
    }
  }

  /**
   * Move to the previous loaded image.
   */
  async function prev() {
    const signal = readSignal;
    if (!signal || signal.aborted || loading || imageLoading) return;
    if (imageIndex === startOffset && hasPrevious && !(await loadMore(true))) return;
    if (imageIndex > startOffset && !signal.aborted) {
      animForward = false;
      imageIndex--;
      anchor = { index: imageIndex, offset: 0 };
      imageLoading = true;
      scrollEl?.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    }
  }

  /**
   * Move to the next image, loading more images when needed.
   */
  async function next() {
    const signal = readSignal;
    if (!signal || signal.aborted || loading || imageLoading) {
      return;
    }
    if (imageIndex >= startOffset + images.length - 1) {
      const currentIndex = imageIndex;
      if (!(await loadMore()) || signal.aborted || imageIndex !== currentIndex) {
        return;
      }
    }
    if (imageIndex < startOffset + images.length - 1) {
      animForward = true;
      imageIndex++;
      anchor = { index: imageIndex, offset: 0 };
      imageLoading = true;
      scrollEl?.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    }
  }

  /**
   * Add image URLs to either end of the loaded range, skipping duplicates.
   *
   * @param urls - The image URLs returned by the content source.
   * @param before - Prepend an earlier batch; defaults to appending.
   * @returns The number of images added.
   */
  function appendImages(urls: string[] | null | undefined, before = false): number {
    const nextImages = (urls ?? []).map((src) => proxyImage(src.trim(), 'auto')).filter((src): src is string => !!src);
    const previousLength = images.length;
    images = [...new Set(before ? [...nextImages, ...images] : [...images, ...nextImages])];
    return images.length - previousLength;
  }

  /**
   * Extend the loaded range through the current source, ignoring cancelled results.
   *
   * @param before - Load the preceding batch; defaults to the next batch.
   * @returns Whether new images were added to the current resource.
   */
  async function loadMore(before = false) {
    const loader = loadImages;
    const signal = readSignal;
    const offset = before ? Math.max(0, startOffset - IMAGE_BATCH_SIZE) : nextOffset;
    if (loading || !loader || !signal || signal.aborted || offset === null) {
      return false;
    }
    loading = true;
    loadFailed = false;
    failedBefore = before;
    try {
      const limit = before ? startOffset - offset : IMAGE_BATCH_SIZE;
      if (limit <= 0) return false;
      const data = await loader({ offset, limit, version, signal });
      if (signal.aborted) {
        return false;
      }
      anchor = getPosition() ?? anchor;
      const appended = appendImages(data.images, before);
      if (before) startOffset = offset;
      const end = startOffset + images.length;
      imageCount = Math.max(data.image_count ?? imageCount, end);
      const next = data.next_offset === undefined ? (end < imageCount ? end : null) : data.next_offset;
      if (!before) nextOffset = appended > 0 && next !== null && next > offset ? next : null;
      if (nextOffset === null) {
        imageCount = end;
      }
      await tick();
      if (!signal.aborted) restorePosition();
      return appended > 0;
    } catch {
      if (!signal.aborted) {
        loadFailed = true;
      }
      return false;
    } finally {
      if (signal === readSignal) {
        loading = false;
      }
    }
  }

  /**
   * Update the current image index according to scroll position.
   */
  function updateImageIndex() {
    const el = scrollEl;
    if (!el || !$settings || $settings.readMode !== 'scroll') {
      return;
    }
    for (let i = 0; i < imageEls.length; i++) {
      const imgEl = imageEls[i];
      if (!imgEl) {
        continue;
      }
      if (imgEl.offsetTop + imgEl.offsetHeight > el.scrollTop) {
        imageIndex = startOffset + i;
        return;
      }
    }
  }

  /**
   * Restore the selected image after it finishes loading.
   *
   * @param event - The load event from the currently mounted paged image.
   */
  function handleImageLoad(event: Event) {
    if (event.currentTarget !== pagedImage || readSignal?.aborted) return;
    imageLoading = false;
    restorePosition();
  }

  /**
   * Retry failed images while preserving local asset query parameters.
   *
   * @param e - The image error event.
   */
  function handleImageError(e: Event) {
    const img = e.target as HTMLImageElement;
    if (!img.isConnected || readSignal?.aborted) {
      return;
    }
    const retry = parseInt(img.dataset.retry || '0');
    if (retry < MAX_IMAGE_RETRY) {
      img.dataset.retry = String(retry + 1);
      const url = new URL(img.src);
      if (url.origin !== window.location.origin || !url.pathname.startsWith('/_api/media/')) {
        url.searchParams.set('_r', String(retry + 1));
      }
      img.src = url.toString();
    } else {
      imageLoading = false;
    }
  }

  /**
   * Track scroll position and request more images near either end.
   */
  function handleImageScroll() {
    if (restoring) return;
    updateImageIndex();
    reportProgress();
    const el = scrollEl;
    if ($settings?.readMode !== 'scroll' || !el || loading || loadFailed) {
      return;
    }
    if (hasPrevious && el.scrollTop < SCROLL_LOAD_THRESHOLD) {
      loadMore(true);
    } else if (hasMore && el.scrollTop + el.clientHeight >= el.scrollHeight - SCROLL_LOAD_THRESHOLD) {
      loadMore();
    }
  }

  /**
   * Handle keyboard navigation in paged mode.
   *
   * @param e - The keyboard event.
   */
  function handleKeyDown(e: KeyboardEvent) {
    if (settingsOpen || chaptersOpen || $settings === null) {
      return;
    }
    if (e.key === 'ArrowUp' || e.key === 'PageUp') {
      prev();
    } else if (e.key === 'ArrowDown' || e.key === 'PageDown') {
      next();
    } else {
      return;
    }
    showControls();
  }

  /**
   * Handle paged reader click zones.
   *
   * @param e - The click event.
   */
  function handleClick(e: MouseEvent) {
    if (settingsOpen || chaptersOpen || $settings?.readMode !== 'paged') {
      return;
    }
    if (e.target instanceof Element && e.target.closest('button, input, label')) {
      return;
    }
    const direction = $settings.pageDirection;
    if (direction === 'bottom') {
      const y = e.clientY / window.innerHeight;
      if (y < CLICK_ZONE_THRESHOLD) {
        prev();
      } else if (y > 1 - CLICK_ZONE_THRESHOLD) {
        next();
      } else {
        showControls();
      }
    } else {
      const x = e.clientX / window.innerWidth;
      const isLeft = direction === 'left';
      if (isLeft ? x > 1 - CLICK_ZONE_THRESHOLD : x < CLICK_ZONE_THRESHOLD) {
        prev();
      } else if (isLeft ? x < CLICK_ZONE_THRESHOLD : x > 1 - CLICK_ZONE_THRESHOLD) {
        next();
      } else {
        showControls();
      }
    }
  }

  // auto-hide timer for controls
  let hideTimer: ReturnType<typeof setTimeout>;

  /**
   * Show transient controls and restart the auto-hide timer.
   */
  function showControls() {
    controlsVisible = true;
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => (controlsVisible = false), CONTROLS_HIDE_DELAY);
  }

  onMount(() => {
    freeze.set(true);
    showControls();
    return () => {
      readController?.abort();
      mountId++;
      cancelAnimationFrame(progressFrame);
      cancelAnimationFrame(restoreFrame);
      freeze.set(false);
      clearTimeout(hideTimer);
    };
  });
</script>

<svelte:window onkeydown={handleKeyDown} onmousemove={showControls} />

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
  role="application"
  aria-label="Image viewer"
  data-theme="dark"
  class="fixed inset-0 flex flex-col bg-black"
  onclick={handleClick}
>
  <!-- top bar -->
  {#if controlsVisible}
    <div
      class="absolute inset-x-0 top-0 z-1 grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 bg-black/50 px-2 py-1.5 text-white/80 backdrop-blur-sm"
      transition:fade={{ duration: 200 }}
    >
      <div class="flex items-center gap-1">
        <button
          class="btn border-0 btn-ghost shadow-none btn-xs"
          onclick={() => {
            (back ?? historyBack)();
            readController?.abort();
          }}
          aria-label="Back"
        >
          <iconify-icon icon={icons.backSolid} width="1.25rem"></iconify-icon>
        </button>
        {#if chapters.length > 1}
          <button
            class="btn border-0 btn-ghost shadow-none btn-xs"
            aria-label="Chapters"
            onclick={() => {
              chaptersOpen = true;
              clearTimeout(hideTimer);
            }}
          >
            <iconify-icon icon={icons.menuFoldSolid} width="1.5rem"></iconify-icon>
          </button>
        {/if}
      </div>

      <span class="flex-center min-w-0 text-sm">
        <span class="truncate" class:mr-2={imageCount > 0}>{currentTitle}</span>
        {#if imageCount > 0}
          <span class="shrink-0 tabular-nums opacity-60">{imageIndex + 1} / {imageCount}</span>
        {/if}
      </span>

      <button
        class="btn border-0 btn-ghost shadow-none btn-xs"
        aria-label="Reading settings"
        onclick={() => (settingsOpen = !settingsOpen)}
      >
        <iconify-icon icon={icons.settingsFilled} width="1.25rem"></iconify-icon>
      </button>
    </div>
  {/if}

  <!-- chapter panel -->
  {#if chaptersOpen}
    <button
      class="fixed inset-0 z-2 bg-black/20"
      aria-label="Close chapter list"
      onclick={() => (chaptersOpen = false)}
      transition:fade={{ duration: 150 }}
    ></button>
    <div
      class="fixed top-0 left-0 z-3 flex h-full w-72 flex-col overflow-y-auto bg-[#1a1a1a] text-[#ccc] shadow-xl sm:w-80"
      transition:fly={{ x: -300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <div class="flex items-center gap-1">
          <h3 class="text-base font-bold">{$_('media.image.chapters')}</h3>
          <!-- svelte-ignore a11y_consider_explicit_label -->
          <button class="btn border-0 bg-transparent text-white/80 shadow-none btn-xs" onclick={toggleSort}>
            <iconify-icon icon={sortDesc ? icons.arrowSortDownLines : icons.arrowSortUpLines} width="1rem">
            </iconify-icon>
          </button>
        </div>
        <button
          class="btn border-0 bg-transparent text-white/80 shadow-none btn-xs"
          aria-label="Close"
          onclick={() => (chaptersOpen = false)}
        >
          <iconify-icon icon={icons.dismiss} width="1.125rem"></iconify-icon>
        </button>
      </div>
      {@render chapterMenu()}
    </div>
  {/if}

  <!-- reading area -->
  {#if $settings?.readMode === 'scroll'}
    <div
      bind:this={scrollEl}
      class="relative min-w-0 flex-1 overflow-x-auto overflow-y-auto overscroll-none [overflow-anchor:none]"
      onscroll={handleImageScroll}
    >
      <div bind:this={contentEl}>
        {#each images as src, i (`${mountId}:${startOffset + i}`)}
          <img
            bind:this={imageEls[i]}
            {src}
            alt=""
            class="scroll-image {zoomClass}"
            loading={Math.abs(startOffset + i - imageIndex) < 2 ? 'eager' : 'lazy'}
            draggable="false"
            onload={(e) => {
              anchor = getPosition() ?? anchor;
              e.currentTarget.setAttribute('data-loaded', 'true');
              restorePosition();
            }}
            onerror={handleImageError}
          />
        {/each}
      </div>
      {#if loading}
        <div class="flex-center pt-6 pb-12 text-white/40">
          <span class="loading loading-md loading-spinner"></span>
        </div>
      {/if}
    </div>
  {:else}
    <div
      bind:this={scrollEl}
      class="relative min-w-0 flex-1 overflow-auto overscroll-none [overflow-anchor:none]"
      onscroll={handleImageScroll}
    >
      <div bind:this={contentEl} class="flex h-full w-min min-w-full justify-center">
        {#if images[imageIndex - startOffset]}
          {#key `${mountId}:${imageIndex}`}
            <img
              bind:this={pagedImage}
              src={images[imageIndex - startOffset]}
              alt=""
              class="my-auto {zoomClass}"
              draggable="false"
              in:fly={flyParams}
              onload={handleImageLoad}
              onerror={handleImageError}
            />
          {/key}
        {/if}
      </div>
    </div>
    {#if loading || imageLoading}
      <div class="pointer-events-none fixed inset-0 z-1 flex items-center justify-center text-white/60">
        <span class="loading loading-xl loading-spinner"></span>
      </div>
    {/if}
  {/if}

  {#if loadFailed}
    <div
      role="status"
      class="absolute inset-x-0 bottom-12 z-1 mx-auto flex w-fit max-w-full items-center gap-3 rounded-field bg-black/80 px-4 py-2 text-sm text-white/80"
    >
      <span>{$_('media.image.load_failed')}</span>
      <button class="btn border-0 btn-ghost shadow-none btn-sm" onclick={() => loadMore(failedBefore)}>
        {$_('action.retry')}
      </button>
    </div>
  {/if}

  <!-- settings panel -->
  {#if settingsOpen}
    <button
      class="fixed inset-0 z-2 bg-black/20"
      aria-label="Close settings"
      onclick={() => (settingsOpen = false)}
      transition:fade={{ duration: 150 }}
    ></button>
    <div
      class="fixed top-0 right-0 z-3 flex h-full w-72 flex-col overflow-y-auto bg-[#1a1a1a] text-[#ccc] shadow-xl sm:w-80"
      transition:fly={{ x: 300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 class="text-base font-bold">{$_('media.image.settings')}</h3>
        <button
          class="btn border-0 bg-transparent text-white/80 shadow-none btn-xs"
          aria-label="Close"
          onclick={() => (settingsOpen = false)}
        >
          <iconify-icon icon={icons.dismiss} width="1.125rem"></iconify-icon>
        </button>
      </div>
      <div class="flex-1 space-y-5 p-4">
        {#if $settings !== null}
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">{$_('media.image.read_mode')}</span>
            <div class="grid grid-cols-2 gap-2">
              {@render readModeBtn('scroll')}
              {@render readModeBtn('paged')}
            </div>
          </div>
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">{$_('media.image.zoom_mode')}</span>
            <div class="grid grid-cols-3 gap-2">
              {@render zoomModeBtn('auto')}
              {@render zoomModeBtn('width')}
              {@render zoomModeBtn('height')}
            </div>
          </div>
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">{$_('media.image.page_direction')}</span>
            <div class="grid grid-cols-3 gap-2">
              {@render directionBtn('right')}
              {@render directionBtn('left')}
              {@render directionBtn('bottom')}
            </div>
          </div>
        {/if}
      </div>
    </div>
  {/if}

  <!-- bottom bar -->
  {#if controlsVisible && chapters.length > 1}
    <div
      class="absolute inset-x-0 bottom-0 z-1 flex justify-center gap-6 bg-black/50 p-2 text-white/80 backdrop-blur-sm"
      transition:fade={{ duration: 200 }}
    >
      <button
        class="btn border-0 btn-ghost shadow-none btn-xs disabled:text-white/20"
        aria-label="Previous chapter"
        disabled={!previousChapter}
        onclick={() => previousChapter && selectChapter(previousChapter)}
      >
        <iconify-icon icon={icons.arrowPreviousFilled} width="1.25rem"></iconify-icon>
      </button>
      <button
        class="btn border-0 btn-ghost shadow-none btn-xs disabled:text-white/20"
        aria-label="Next chapter"
        disabled={!nextChapter}
        onclick={() => nextChapter && selectChapter(nextChapter)}
      >
        <iconify-icon icon={icons.arrowNextFilled} width="1.25rem"></iconify-icon>
      </button>
    </div>
  {/if}
</div>

{#snippet chapterMenu()}
  <ul class="menu w-full px-2 pb-6 text-sm">
    {#each sortDesc ? [...chapterGroups].reverse() : chapterGroups as group, groupIndex (group.volume ?? groupIndex)}
      {#if group.volume}
        <li>
          <h2 class="menu-title text-neutral-content/40">{group.volume}</h2>
          <ul>
            {#each group.chapters as chapter, chapterIndex (chapter.id ?? chapterIndex)}
              {@render chapterItem(chapter)}
            {/each}
          </ul>
        </li>
      {:else}
        {#each sortDesc ? [...group.chapters].reverse() : group.chapters as chapter, chapterIndex (chapter.id ?? chapterIndex)}
          {@render chapterItem(chapter)}
        {/each}
      {/if}
    {/each}
  </ul>
{/snippet}

{#snippet chapterItem(chapter: Chapter)}
  <li class="mb-0.75">
    <button
      class="h-auto min-h-0 py-2 whitespace-normal {matchChapterId(chapter.id, chapterId) ? 'menu-active' : ''}"
      title={chapter.title}
      onclick={() => selectChapter(chapter)}
    >
      <span class="min-w-0 text-left wrap-break-word">{chapter.title}</span>
    </button>
  </li>
{/snippet}

{#snippet readModeBtn(mode: ReadMode)}
  <label
    class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-opacity
    {$settings?.readMode === mode ? 'bg-primary/15 outline' : 'opacity-50 hover:opacity-80'}"
  >
    {#if $settings !== null}
      <input type="radio" class="hidden" value={mode} bind:group={$settings.readMode} />
    {/if}
    {$_(`media.image.read_mode_options.${mode}`)}
  </label>
{/snippet}

{#snippet zoomModeBtn(mode: ZoomMode)}
  <label
    class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-opacity
    {$settings?.zoomMode === mode ? 'bg-primary/15 outline' : 'opacity-50 hover:opacity-80'}"
  >
    {#if $settings !== null}
      <input type="radio" class="hidden" value={mode} bind:group={$settings.zoomMode} />
    {/if}
    {$_(`media.image.zoom_mode_options.${mode}`)}
  </label>
{/snippet}

{#snippet directionBtn(direction: PageDirection)}
  <label
    class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-opacity
    {$settings?.pageDirection === direction ? 'bg-primary/15 outline' : 'opacity-50 hover:opacity-80'}"
  >
    {#if $settings !== null}
      <input type="radio" class="hidden" value={direction} bind:group={$settings.pageDirection} />
    {/if}
    {$_(`media.image.page_direction_options.${direction}`)}
  </label>
{/snippet}

<style>
  /* reserve space until the browser knows the image dimensions */
  .scroll-image:not([data-loaded]) {
    width: 100%;
    height: 100dvh;
  }

  .btn-ghost {
    &:not(*:disabled) {
      color: color-mix(in oklab, #fff 80%, transparent);
      &:is(:hover, :focus-visible) {
        color: color-mix(in oklab, #fff 90%, transparent);
        background-color: color-mix(in oklab, #fff 20%, transparent);
      }
    }
  }
</style>
