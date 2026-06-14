<script lang="ts" module>
  import { persisted } from '$lib/stores';
  import type { Chapter, ChapterGroup, Resource, Resp } from '$lib/types';

  /** Delay in ms before auto-hiding the overlay controls. */
  const CONTROLS_HIDE_DELAY = 3000;
  /** Click-zone threshold ratio in paged mode. The complementary zone is `1 - threshold`. */
  const CLICK_ZONE_THRESHOLD = 0.3;
  /** Distance from the bottom of the scroll container (px) to trigger loading more images. */
  const SCROLL_LOAD_THRESHOLD = 400;
  /** Maximum retries for a failed image request. */
  const MAX_IMAGE_RETRY = 3;

  /** Options passed to the image viewer mount function. */
  export type ImageViewerOptions = {
    images: string[];
    image_count?: number | null;
    title?: string | null;
    chapters?: Chapter[];
    chapterId?: string | null;
    chapterChange?: (chapter: Chapter) => void;
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
  };

  const settings = persisted<ImageViewerSettings>('image-viewer', {
    readMode: 'scroll',
    zoomMode: 'auto',
    pageDirection: 'right'
  });

  const READ_MODES: Record<ReadMode, { label: string }> = {
    scroll: { label: '滚动' },
    paged: { label: '翻页' }
  };

  const ZOOM_MODES: Record<ZoomMode, { label: string; class: string }> = {
    auto: { label: '自动', class: 'max-h-full max-w-full object-contain mx-auto' },
    width: { label: '适应宽度', class: 'w-full h-auto' },
    height: { label: '适应高度', class: 'h-full w-auto max-w-none mx-auto' }
  };

  const PAGE_DIRECTIONS: Record<PageDirection, { label: string }> = {
    right: { label: '点击右侧' },
    left: { label: '点击左侧' },
    bottom: { label: '点击下方' }
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
  import { page as route } from '$app/state';
  import { api, proxyImage } from '$lib/api';
  import { icons } from '$lib/icons';
  import { freeze, historyBack } from '$lib/stores';
  import { onMount } from 'svelte';
  import { fade, fly } from 'svelte/transition';

  // resource title
  let title = $state('');
  // loaded image urls
  let images = $state<string[]>([]);
  // available chapters
  let chapters = $state<Chapter[]>([]);
  // chapters grouped by volume
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
  // display title, resource title or current chapter title
  let currentTitle = $derived(title || (chapterIndex >= 0 ? chapters[chapterIndex] : null)?.title);

  // whether the settings panel is open
  let settingsOpen = $state(false);
  // whether the chapters menu is open
  let chaptersOpen = $state(false);
  // whether the overlay controls are visible
  let controlsVisible = $state(true);

  // the scroll container element
  let scrollEl = $state<HTMLDivElement | undefined>(undefined);
  // image elements for paged mode observation
  let imageEls: HTMLImageElement[] = [];
  // current image index, 0-based
  let imageIndex = $state(0);
  // total number of images available from the api
  let imageCount = $state(0);
  // whether all available images have been loaded
  let exhausted = $state(false);
  // whether more images can be loaded
  let hasMore = $derived(images.length < imageCount && !exhausted);
  // whether a network request is in progress
  let loading = $state(false);
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
  export function mount(options: ImageViewerOptions) {
    if (!options || !options.images?.length) {
      return;
    }
    title = options.title ?? '';
    images = options.images.map((src) => proxyImage(src, 'auto')).filter((src): src is string => !!src);
    imageCount = options.image_count && options.image_count > 0 ? options.image_count : images.length;
    chapters = options.chapters ?? [];
    chapterId = options.chapterId ?? null;
    chapterChange = options.chapterChange;
    imageIndex = 0;
    imageLoading = true;
    exhausted = images.length >= imageCount;
    showControls();
  }

  /**
   * Select a chapter and notify the parent page.
   *
   * @param chapter - The chapter to select.
   */
  function selectChapter(chapter: Chapter) {
    chaptersOpen = false;
    chapterId = chapter.id ?? null;
    chapterChange?.(chapter);
  }

  /**
   * Move to the previous loaded image.
   */
  function prev() {
    if (imageIndex > 0) {
      animForward = false;
      imageIndex--;
      imageLoading = true;
      scrollEl?.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    }
  }

  /**
   * Move to the next image, loading more images when needed.
   */
  async function next() {
    if (imageIndex >= imageCount - 1 || loading || imageLoading) {
      return;
    }
    if (imageIndex >= images.length - 1) {
      await loadMore();
    }
    if (imageIndex < images.length - 1) {
      animForward = true;
      imageIndex++;
      imageLoading = true;
      scrollEl?.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    }
  }

  /**
   * Append new image URLs to the viewer, skipping duplicates.
   *
   * @param urls - The image URLs returned from the details API.
   * @returns The number of images appended.
   */
  function appendImages(urls: string[] | null | undefined): number {
    const nextImages = (urls ?? []).map((src) => proxyImage(src, 'auto')).filter((src): src is string => !!src);
    const appended = nextImages.filter((src) => !images.includes(src));
    if (appended.length > 0) {
      images = [...images, ...appended];
    }
    return appended.length;
  }

  /**
   * Load the next batch of images from the details API.
   */
  async function loadMore() {
    if (loading || !hasMore) {
      return;
    }
    loading = true;
    try {
      const resp = await api
        .post(`flow/graph/${route.params.indexer_id}/execute`, {
          json: {
            $start: 'details_start',
            id: route.params.rsrc_id,
            chapter_id: route.url.searchParams.get('chapter_id') ?? chapterId,
            page: images.length + 1
          }
        })
        .json<Resp<Resource | null>>();
      const data = resp.data;
      const nextCount = data?.image_count;
      imageCount = nextCount && nextCount > 0 ? nextCount : imageCount;
      exhausted = appendImages(data?.images) === 0;
    } finally {
      loading = false;
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
    const containerTop = el.getBoundingClientRect().top;
    for (let i = 0; i < imageEls.length; i++) {
      const imgEl = imageEls[i];
      if (!imgEl) {
        continue;
      }
      const rect = imgEl.getBoundingClientRect();
      if (rect.bottom > containerTop) {
        imageIndex = i;
        return;
      }
    }
  }

  /**
   * Clear image loading state after an image finishes loading.
   */
  function handleImageLoad() {
    imageLoading = false;
  }

  /**
   * Retry a failed image request with a cache-busting query parameter.
   *
   * @param e - The image error event.
   */
  function handleImageError(e: Event) {
    const img = e.target as HTMLImageElement;
    const retry = parseInt(img.dataset.retry || '0');
    if (retry < MAX_IMAGE_RETRY) {
      img.dataset.retry = String(retry + 1);
      const url = new URL(img.src);
      url.searchParams.set('_r', String(retry + 1));
      img.src = url.toString();
    } else {
      imageLoading = false;
    }
  }

  /**
   * Track scroll position and request more images near the end.
   */
  function handleImageScroll() {
    updateImageIndex();
    const el = scrollEl;
    if ($settings?.readMode !== 'scroll' || !el || !hasMore || loading) {
      return;
    }
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - SCROLL_LOAD_THRESHOLD) {
      loadMore();
    }
  }

  /**
   * Handle keyboard navigation in paged mode.
   *
   * @param e - The keyboard event.
   */
  function handleKeyDown(e: KeyboardEvent) {
    if (settingsOpen || $settings === null) {
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
      freeze.set(false);
      clearTimeout(hideTimer);
    };
  });
</script>

<svelte:window onkeydown={handleKeyDown} onmousemove={showControls} />

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div role="application" aria-label="Image viewer" class="fixed inset-0 flex flex-col bg-black" onclick={handleClick}>
  <!-- top bar -->
  {#if controlsVisible}
    <div
      class="absolute top-0 inset-x-0 z-1 grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 bg-black/50 px-2 py-1.5 text-white/80 backdrop-blur-sm"
      transition:fade={{ duration: 200 }}
    >
      <div class="flex items-center gap-1">
        <button class="btn btn-xs btn-ghost border-0 shadow-none" onclick={() => historyBack()} aria-label="Back">
          <iconify-icon icon={icons.backSolid} width="1.25rem"></iconify-icon>
        </button>
        {#if chapters.length > 1}
          <button
            class="btn btn-xs btn-ghost border-0 shadow-none"
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

      <span class="min-w-0 flex-center text-sm">
        <span class="truncate" class:mr-2={imageCount > 0}>{currentTitle}</span>
        {#if imageCount > 0}
          <span class="shrink-0 tabular-nums opacity-60">{imageIndex + 1} / {imageCount}</span>
        {/if}
      </span>

      <button
        class="btn btn-xs btn-ghost border-0 shadow-none"
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
      class="fixed left-0 top-0 z-3 flex h-full w-72 flex-col overflow-y-auto bg-[#1a1a1a] text-[#ccc] shadow-xl sm:w-80"
      transition:fly={{ x: -300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 class="text-base font-bold">章节</h3>
        <button
          class="btn btn-xs border-0 bg-transparent shadow-none text-white/80"
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
      class="min-w-0 flex-1 overflow-x-auto overflow-y-auto overscroll-none"
      onscroll={handleImageScroll}
    >
      {#each images as src, i (i)}
        <img
          bind:this={imageEls[i]}
          {src}
          alt=""
          class={zoomClass}
          loading={i < 3 ? 'eager' : 'lazy'}
          draggable="false"
          onload={handleImageScroll}
          onerror={handleImageError}
        />
      {/each}
      {#if loading}
        <div class="flex-center pt-6 pb-12 text-white/40">
          <span class="loading loading-spinner loading-md"></span>
        </div>
      {/if}
    </div>
  {:else}
    <div bind:this={scrollEl} class="min-w-0 flex-1 overflow-auto overscroll-none">
      <div class="flex-center h-full w-min min-w-full">
        {#if imageCount > 0}
          {#key imageIndex}
            <img
              src={images[imageIndex]}
              alt=""
              class={zoomClass}
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
        <span class="loading loading-spinner loading-xl"></span>
      </div>
    {/if}
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
      class="fixed right-0 top-0 z-3 flex h-full w-72 flex-col overflow-y-auto bg-[#1a1a1a] text-[#ccc] shadow-xl sm:w-80"
      transition:fly={{ x: 300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 class="text-base font-bold">阅读设置</h3>
        <button
          class="btn btn-xs border-0 bg-transparent shadow-none text-white/80"
          aria-label="Close"
          onclick={() => (settingsOpen = false)}
        >
          <iconify-icon icon={icons.dismiss} width="1.125rem"></iconify-icon>
        </button>
      </div>
      <div class="flex-1 space-y-5 p-4">
        {#if $settings !== null}
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">阅读模式</span>
            <div class="grid grid-cols-2 gap-2">
              {@render readModeBtn('scroll')}
              {@render readModeBtn('paged')}
            </div>
          </div>
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">缩放模式</span>
            <div class="grid grid-cols-3 gap-2">
              {@render zoomModeBtn('auto')}
              {@render zoomModeBtn('width')}
              {@render zoomModeBtn('height')}
            </div>
          </div>
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">翻页方向</span>
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
      class="absolute bottom-0 inset-x-0 z-1 flex justify-center gap-6 bg-black/50 p-2 text-white/80 backdrop-blur-sm"
      transition:fade={{ duration: 200 }}
    >
      <button
        class="btn btn-xs btn-ghost border-0 shadow-none disabled:text-white/20"
        aria-label="Previous chapter"
        disabled={!previousChapter}
        onclick={() => previousChapter && selectChapter(previousChapter)}
      >
        <iconify-icon icon={icons.arrowPreviousFilled} width="1.25rem"></iconify-icon>
      </button>
      <button
        class="btn btn-xs btn-ghost border-0 shadow-none disabled:text-white/20"
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
    {#each chapterGroups as group, groupIndex (group.volume ?? groupIndex)}
      {#if group.volume}
        <li>
          <h2 class="menu-title">{group.volume}</h2>
          <ul>
            {#each group.chapters as chapter, chapterIndex (chapter.id ?? chapterIndex)}
              {@render chapterItem(chapter)}
            {/each}
          </ul>
        </li>
      {:else}
        {#each group.chapters as chapter, chapterIndex (chapter.id ?? chapterIndex)}
          {@render chapterItem(chapter)}
        {/each}
      {/if}
    {/each}
  </ul>
{/snippet}

{#snippet chapterItem(chapter: Chapter)}
  <li>
    <button
      class={matchChapterId(chapter.id, chapterId) ? 'menu-active' : ''}
      title={chapter.title}
      onclick={() => selectChapter(chapter)}
    >
      {chapter.title}
    </button>
  </li>
{/snippet}

{#snippet readModeBtn(mode: ReadMode)}
  <label
    class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-all
    {$settings?.readMode === mode ? 'bg-primary/15 text-primary' : 'opacity-50 hover:opacity-80'}"
  >
    {#if $settings !== null}
      <input type="radio" class="hidden" value={mode} bind:group={$settings.readMode} />
    {/if}
    {READ_MODES[mode].label}
  </label>
{/snippet}

{#snippet zoomModeBtn(mode: ZoomMode)}
  <label
    class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-all
    {$settings?.zoomMode === mode ? 'bg-primary/15 text-primary' : 'opacity-50 hover:opacity-80'}"
  >
    {#if $settings !== null}
      <input type="radio" class="hidden" value={mode} bind:group={$settings.zoomMode} />
    {/if}
    {ZOOM_MODES[mode].label}
  </label>
{/snippet}

{#snippet directionBtn(direction: PageDirection)}
  <label
    class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-all
    {$settings?.pageDirection === direction ? 'bg-primary/15 text-primary' : 'opacity-50 hover:opacity-80'}"
  >
    {#if $settings !== null}
      <input type="radio" class="hidden" value={direction} bind:group={$settings.pageDirection} />
    {/if}
    {PAGE_DIRECTIONS[direction].label}
  </label>
{/snippet}
