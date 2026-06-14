<script lang="ts" module>
  import { persisted } from '$lib/stores';
  import type { Chapter, Resource } from '$lib/types';

  export type ReadMode = 'scroll' | 'paged';
  export type ZoomMode = 'width' | 'height' | 'auto';
  export type Direction = 'ltr' | 'rtl';

  export type ImageReaderSettings = {
    readMode: ReadMode;
    zoomMode: ZoomMode;
    direction: Direction;
  };

  export type ImageViewerOptions = {
    images: string[];
    image_count?: number | null;
    index?: number;
    title?: string | null;
    chapters?: Chapter[];
    chapterId?: string | null;
    chapterChange?: (chapter: Chapter) => void;
  };

  type ChapterGroup = {
    volume: string | null;
    chapters: Chapter[];
  };

  const settings = persisted<ImageReaderSettings>('image-reader', {
    readMode: 'scroll',
    zoomMode: 'width',
    direction: 'ltr'
  });

  const ZOOM: Record<ZoomMode, string> = {
    width: 'w-full h-auto',
    height: 'h-full w-auto max-w-none mx-auto',
    auto: 'max-h-full max-w-full object-contain mx-auto'
  };

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
  import type { Resp } from '$lib/types';
  import { onMount } from 'svelte';
  import { fade, fly } from 'svelte/transition';

  let images = $state<string[]>([]);
  let title = $state('');
  let chapters = $state<Chapter[]>([]);
  let chapterId = $state<string | null>(null);
  let chapterChange = $state<((c: Chapter) => void) | undefined>(undefined);
  let scrollEl = $state<HTMLDivElement | undefined>(undefined);
  let imageEls: HTMLImageElement[] = [];
  let pageIndex = $state(0);
  let totalCount = $state<number | null>(null);
  let open = $state(false);
  let chapterOpen = $state(false);
  let visible = $state(true);
  let loading = $state(false);
  let imageLoading = $state(false);
  let exhausted = $state(false);
  let total = $derived(totalCount ?? images.length);
  let hasMore = $derived(images.length < total && !exhausted);

  let zoomClass = $derived(ZOOM[$settings?.zoomMode ?? 'width']);
  let currentTitle = $derived(chapters.find((c) => c.id === chapterId)?.title ?? title);
  let chapterGroups = $derived(groupChapters(chapters));

  export function mount(options: ImageViewerOptions) {
    if (!options?.images?.length) return;
    const nextImages = options.images.map((src) => proxyImage(src, 'auto')).filter((src): src is string => !!src);
    const nextTotal = options.image_count && options.image_count > 0 ? options.image_count : nextImages.length;
    images = nextImages;
    totalCount = nextTotal;
    title = options.title ?? '';
    chapters = options.chapters ?? [];
    chapterId = options.chapterId ?? null;
    chapterChange = options.chapterChange;
    pageIndex = Math.max(0, Math.min(options.index ?? 0, nextImages.length - 1));
    imageLoading = true;
    exhausted = nextImages.length >= nextTotal;
    resetTimer();
  }

  function selectChapter(chapter: Chapter) {
    chapterOpen = false;
    chapterChange?.(chapter);
  }

  function prev() {
    if (pageIndex > 0) {
      pageIndex--;
      imageLoading = true;
      scrollEl?.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    }
  }
  async function next() {
    if (pageIndex >= total - 1 || loading || imageLoading) {
      return;
    }
    if (pageIndex >= images.length - 1) {
      await loadMore();
    }
    if (pageIndex < images.length - 1) {
      pageIndex++;
      imageLoading = true;
      scrollEl?.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    }
  }

  function appendImages(urls: string[] | null | undefined) {
    const nextImages = (urls ?? []).map((src) => proxyImage(src, 'auto')).filter((src): src is string => !!src);
    const appended = nextImages.filter((src) => !images.includes(src));
    if (appended.length > 0) {
      images = [...images, ...appended];
    }
    return appended.length;
  }

  function currentChapterId() {
    const id = route.url.searchParams.get('chapter_id') ?? chapterId;
    if (!id) {
      return id;
    }
    try {
      return decodeURIComponent(id);
    } catch {
      return id;
    }
  }

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
            chapter_id: currentChapterId(),
            page: images.length + 1
          }
        })
        .json<Resp<Resource | null>>();
      const data = resp.data;
      const nextTotal = data?.image_count;
      totalCount = nextTotal && nextTotal > 0 ? nextTotal : totalCount;
      exhausted = appendImages(data?.images) === 0;
    } finally {
      loading = false;
    }
  }

  function updatePageIndex() {
    const el = scrollEl;
    if (!$settings || $settings.readMode !== 'scroll' || !el) return;

    const containerTop = el.getBoundingClientRect().top;
    for (let i = 0; i < imageEls.length; i++) {
      const imgEl = imageEls[i];
      if (!imgEl) continue;
      const rect = imgEl.getBoundingClientRect();
      if (rect.bottom > containerTop) {
        pageIndex = i;
        return;
      }
    }
  }

  const MAX_IMAGE_RETRY = 3;

  function handleImageLoad() {
    imageLoading = false;
  }

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

  function handleScroll() {
    updatePageIndex();
    const el = scrollEl;
    if ($settings?.readMode !== 'scroll' || !el || loading || !hasMore) {
      return;
    }
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 400) {
      loadMore();
    }
  }

  function handleClick(e: MouseEvent) {
    if (open || chapterOpen || $settings?.readMode !== 'paged') return;
    const x = e.clientX / window.innerWidth;
    const [prevZone, nextZone] = $settings.direction === 'rtl' ? [0.7, 0.3] : [0.3, 0.7];
    if (x < prevZone) prev();
    else if (x > nextZone) void next();
    else resetTimer();
  }

  async function handleKey(e: KeyboardEvent) {
    if (open || $settings === null) return;
    if (e.key === 'ArrowLeft') $settings.direction === 'rtl' ? await next() : prev();
    else if (e.key === 'ArrowRight') $settings.direction === 'rtl' ? prev() : await next();
    else return;
    resetTimer();
  }

  let hideTimer: ReturnType<typeof setTimeout>;
  function resetTimer() {
    visible = true;
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => (visible = false), 3000);
  }

  onMount(() => {
    freeze.set(true);
    resetTimer();
    return () => {
      freeze.set(false);
      clearTimeout(hideTimer);
    };
  });
</script>

<svelte:window onkeydown={handleKey} onmousemove={resetTimer} />

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
  class="fixed inset-0 flex flex-col bg-black"
  onclick={handleClick}
  onkeydown={handleKey}
  role="application"
  aria-label="图片阅读器"
>
  <!-- Top bar -->
  {#if visible}
    <div
      class="absolute top-0 left-0 right-0 z-10 grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 bg-black/50 px-2 py-1.5 text-white/80 backdrop-blur-sm"
      transition:fade={{ duration: 200 }}
    >
      <div class="flex items-center gap-1">
        <button
          class="btn btn-xs btn-ghost border-0 shadow-none text-white/70"
          onclick={() => historyBack()}
          aria-label="返回"
        >
          <iconify-icon icon={icons.back} width="1.25rem"></iconify-icon>
        </button>
        {#if chapters.length > 1}
          <button
            class="btn btn-xs btn-ghost border-0 shadow-none text-white/70"
            aria-label="章节"
            onclick={() => {
              chapterOpen = true;
              clearTimeout(hideTimer);
            }}
          >
            <iconify-icon icon={icons.arrowSortDownLines} width="1.125rem"></iconify-icon>
          </button>
        {/if}
      </div>

      <span class="min-w-0 truncate text-center text-sm">
        {currentTitle}
        {#if total > 0}
          <span class="ml-2 tabular-nums opacity-60">{pageIndex + 1} / {total}</span>
        {/if}
      </span>

      <button
        class="btn btn-xs btn-ghost justify-self-end border-0 shadow-none text-white/70"
        aria-label="阅读设置"
        onclick={() => (open = !open)}
      >
        <iconify-icon icon={icons.settings} width="1.125rem"></iconify-icon>
      </button>
    </div>
  {/if}

  <!-- Chapter panel -->
  {#if chapterOpen}
    <button
      class="fixed inset-0 z-20 bg-black/20"
      aria-label="关闭章节列表"
      onclick={() => (chapterOpen = false)}
      transition:fade={{ duration: 150 }}
    ></button>
    <div
      class="fixed left-0 top-0 z-30 flex h-full w-72 flex-col overflow-y-auto bg-[#1a1a1a] text-[#ccc] shadow-xl sm:w-80"
      transition:fly={{ x: -300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 class="text-base font-bold">章节</h3>
        <button
          class="btn btn-xs border-0 bg-transparent shadow-none"
          aria-label="关闭"
          onclick={() => (chapterOpen = false)}
        >
          <iconify-icon icon={icons.dismiss} width="1.125rem"></iconify-icon>
        </button>
      </div>
      {@render chapterMenu()}
    </div>
  {/if}

  <!-- Content -->
  {#if $settings?.readMode === 'scroll'}
    <div
      bind:this={scrollEl}
      class="min-w-0 flex-1 overflow-x-auto overflow-y-auto overscroll-none"
      onscroll={handleScroll}
    >
      {#each images as src, i (i)}
        <img
          bind:this={imageEls[i]}
          {src}
          alt=""
          class={zoomClass}
          loading={i < 3 ? 'eager' : 'lazy'}
          draggable="false"
          onload={handleScroll}
          onerror={handleImageError}
        />
      {/each}
      {#if loading}
        <div class="flex-center py-6 text-white/40">
          <span class="loading loading-spinner loading-md"></span>
        </div>
      {/if}
    </div>
  {:else}
    <div bind:this={scrollEl} class="min-w-0 flex-1 overflow-auto overscroll-none">
      <div class="flex-center h-full w-min min-w-full">
        {#if total > 0}
          {#key pageIndex}
            <img
              src={images[pageIndex]}
              alt=""
              class={zoomClass}
              draggable="false"
              transition:fade={{ duration: 150 }}
              onload={handleImageLoad}
              onerror={handleImageError}
            />
          {/key}
        {/if}
      </div>
    </div>

    {#if loading || imageLoading}
      <div class="pointer-events-none fixed inset-0 z-10 flex items-center justify-center text-white/60">
        <span class="loading loading-spinner loading-md"></span>
      </div>
    {/if}
  {/if}

  <!-- Settings panel -->
  {#if open}
    <button
      class="fixed inset-0 z-20 bg-black/20"
      aria-label="关闭设置"
      onclick={() => (open = false)}
      transition:fade={{ duration: 150 }}
    ></button>
    <div
      class="fixed right-0 top-0 z-30 flex h-full w-72 flex-col overflow-y-auto bg-[#1a1a1a] text-[#ccc] shadow-xl sm:w-80"
      transition:fly={{ x: 300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 class="text-base font-bold">阅读设置</h3>
        <button class="btn btn-xs border-0 bg-transparent shadow-none" aria-label="关闭" onclick={() => (open = false)}>
          <iconify-icon icon={icons.dismiss} width="1.125rem"></iconify-icon>
        </button>
      </div>
      <div class="flex-1 space-y-5 px-4 pb-6">
        {#if $settings !== null}
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">阅读模式</span>
            <div class="grid grid-cols-2 gap-2">
              <label
                class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-all {$settings.readMode ===
                'scroll'
                  ? 'bg-primary/15 text-primary'
                  : 'opacity-50 hover:opacity-80'}"
              >
                <input type="radio" class="hidden" value="scroll" bind:group={$settings.readMode} />
                滚动
              </label>
              <label
                class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-all {$settings.readMode ===
                'paged'
                  ? 'bg-primary/15 text-primary'
                  : 'opacity-50 hover:opacity-80'}"
              >
                <input type="radio" class="hidden" value="paged" bind:group={$settings.readMode} />
                翻页
              </label>
            </div>
          </div>
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">缩放模式</span>
            <div class="grid grid-cols-3 gap-2">
              {@render zoomBtn('width', '适应宽度')}
              {@render zoomBtn('height', '适应高度')}
              {@render zoomBtn('auto', '自动')}
            </div>
          </div>
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">翻页方向</span>
            <div class="grid grid-cols-2 gap-2">
              <label
                class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-all {$settings.direction ===
                'ltr'
                  ? 'bg-primary/15 text-primary'
                  : 'opacity-50 hover:opacity-80'}"
              >
                <input type="radio" class="hidden" value="ltr" bind:group={$settings.direction} />
                左翻右
              </label>
              <label
                class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-all {$settings.direction ===
                'rtl'
                  ? 'bg-primary/15 text-primary'
                  : 'opacity-50 hover:opacity-80'}"
              >
                <input type="radio" class="hidden" value="rtl" bind:group={$settings.direction} />
                右翻左
              </label>
            </div>
          </div>
        {/if}
      </div>
    </div>
  {/if}
</div>

{#snippet chapterMenu()}
  <ul class="menu w-full px-2 pb-6 text-sm">
    {#each chapterGroups as group, index (group.volume ?? index)}
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
      class={chapter.id === chapterId ? 'active' : ''}
      title={chapter.title}
      onclick={() => selectChapter(chapter)}
    >
      {chapter.title}
    </button>
  </li>
{/snippet}

{#snippet zoomBtn(mode: ZoomMode, label: string)}
  <label
    class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-all {$settings?.zoomMode ===
    mode
      ? 'bg-primary/15 text-primary'
      : 'opacity-50 hover:opacity-80'}"
  >
    {#if $settings !== null}
      <input type="radio" class="hidden" value={mode} bind:group={$settings.zoomMode} />
    {/if}
    {label}
  </label>
{/snippet}
