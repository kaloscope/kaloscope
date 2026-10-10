<script lang="ts" module>
  import { persisted } from '$lib/stores';
  import type { Chapter, ChapterGroup, ContentBlock, ContentRun } from '$lib/types';

  /** Delay in ms before auto-hiding the overlay controls. */
  const CONTROLS_HIDE_DELAY = 3000;

  /** Position within the current paragraph or block, independent of screen size. */
  export type TextPosition = { index: number; offset: number; percentage: number };

  /** Options passed to the text viewer mount function. */
  export type TextViewerOptions = {
    title?: string | null;
    chapters?: Chapter[];
    chapterId?: string | null;
    chapterChange?: (chapter: Chapter) => void;
    back?: () => void;
    position?: Pick<TextPosition, 'index' | 'offset'>;
    progress?: (position: TextPosition) => void;
  } & ({ text: string | string[]; blocks?: never } | { text?: never; blocks: ContentBlock[] });

  /** Color theme. */
  export type Theme = 'white' | 'cream' | 'sepia' | 'light' | 'green' | 'dark' | 'slate' | 'black';
  /** Font family. */
  export type Font = 'system' | 'serif' | 'sans' | 'kai' | 'mono';

  /** Persisted settings. */
  export type TextViewerSettings = {
    theme: Theme;
    font: Font;
    fontSize: number;
    lineHeight: number;
    paraSpacing: number;
    paddingX: number;
    sortDesc: boolean;
  };

  const settings = persisted<TextViewerSettings>('text-viewer', {
    theme: 'white',
    font: 'system',
    fontSize: 16,
    lineHeight: 1.8,
    paraSpacing: 1,
    paddingX: 2,
    sortDesc: false
  });

  const THEMES: Record<Theme, { bg: string; text: string; muted: string; panel: string; bar: string }> = {
    white: {
      bg: '#fafaf5',
      text: '#333333',
      muted: '#999999',
      panel: '#ffffff',
      bar: 'rgba(0,0,0,0.06)'
    },
    cream: {
      bg: '#fdf6e3',
      text: '#5c4b3a',
      muted: '#9a8978',
      panel: '#ffffff',
      bar: 'rgba(0,0,0,0.08)'
    },
    sepia: {
      bg: '#f4ecd8',
      text: '#5b4636',
      muted: '#a08b76',
      panel: '#ffffff',
      bar: 'rgba(0,0,0,0.08)'
    },
    light: {
      bg: '#e6e6e6',
      text: '#444444',
      muted: '#888888',
      panel: '#ffffff',
      bar: 'rgba(0,0,0,0.08)'
    },
    green: {
      bg: '#dce8d8',
      text: '#3a4a3a',
      muted: '#6b7b6b',
      panel: '#ffffff',
      bar: 'rgba(0,0,0,0.08)'
    },
    dark: {
      bg: '#2b2b2b',
      text: '#cccccc',
      muted: '#666666',
      panel: '#222222',
      bar: 'rgba(0,0,0,0.5)'
    },
    slate: {
      bg: '#1a2128',
      text: '#b0bec5',
      muted: '#546e7a',
      panel: '#1e242c',
      bar: 'rgba(0,0,0,0.5)'
    },
    black: {
      bg: '#000000',
      text: '#aaaaaa',
      muted: '#444444',
      panel: '#1a1a1a',
      bar: 'rgba(0,0,0,0.5)'
    }
  };

  const FONTS: Record<Font, string> = {
    system: 'var(--font-sans)',
    sans: '"Noto Sans SC", "Noto Sans CJK SC", "Noto Sans", "PingFang SC", "Microsoft YaHei", sans-serif',
    serif: '"Noto Serif SC", "Noto Serif CJK SC", "Noto Serif", "Songti SC", "STSong", serif',
    kai: '"Kaiti SC", "KaiTi", "STKaiti", "ST Kaiti", "楷体", "楷体_GB2312", serif',
    mono: '"Noto Sans Mono CJK SC", "Noto Sans Mono", "SF Mono", "Cascadia Code", monospace'
  };

  const SLIDER_CONFIGS = {
    fontSize: { i18n: 'media.text.font_size', min: 12, max: 28, step: 1, unit: 'px' },
    lineHeight: { i18n: 'media.text.line_height', min: 1.4, max: 3.0, step: 0.2, unit: '' },
    paraSpacing: { i18n: 'media.text.para_spacing', min: 0, max: 2, step: 0.5, unit: 'em' },
    paddingX: { i18n: 'media.text.padding_x', min: 0, max: 4, step: 0.5, unit: 'rem' }
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

  /**
   * Normalize text payloads into the viewer's paragraph-based rendering model.
   *
   * @param text - The plain-text payload to display.
   * @returns Paragraphs retaining the indices supplied by local content APIs.
   */
  function normalizeTextContent(text: string | string[]): string[] {
    return (Array.isArray(text) ? text : text.split(/\n{2,}/)).map((paragraph) => paragraph.trim());
  }
</script>

<script lang="ts">
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { freeze, historyBack } from '$lib/stores';
  import { onMount, tick } from 'svelte';
  import { fade, fly } from 'svelte/transition';

  // resource title
  let title = $state('');
  // paragraphs of the current chapter
  let paragraphs = $state<string[]>([]);
  // structured content of the current chapter
  let blocks = $state<ContentBlock[]>([]);
  // image failures in the current chapter
  let failedImages = $state<Record<string, boolean>>({});
  // callback to return from a local reading overlay
  let back = $state<(() => void) | undefined>(undefined);
  // the reading area's scroll container
  let scrollEl = $state<HTMLElement | undefined>(undefined);
  let contentEl = $state<HTMLDivElement | undefined>(undefined);
  // rendered anchors shared by paragraphs and structured blocks
  let blockEls = $state<HTMLElement[]>([]);
  let anchor: TextViewerOptions['position'];
  let progress: TextViewerOptions['progress'];
  let restoring = false;
  let mountId = $state(0);
  let progressFrame = 0;
  let restoreFrame = 0;
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

  // current theme colors
  let colors = $derived(THEMES[$settings?.theme ?? 'white']);

  /**
   * Mount the text viewer with plain text or structured blocks.
   *
   * @param options - The text viewer options.
   */
  export async function mount(options: TextViewerOptions) {
    if (!options) {
      return;
    }
    const current = ++mountId;
    restoring = true;
    cancelAnimationFrame(progressFrame);
    cancelAnimationFrame(restoreFrame);
    progressFrame = 0;
    progress = options.progress;
    anchor = options.position ?? { index: 0, offset: 0 };
    blockEls = [];
    title = options.title ?? '';
    paragraphs = options.text === undefined ? [] : normalizeTextContent(options.text);
    blocks = options.blocks ?? [];
    failedImages = {};
    back = options.back;
    chapters = options.chapters ?? [];
    chapterId = options.chapterId ?? null;
    chapterChange = options.chapterChange;
    showControls();
    await tick();
    if (current !== mountId) return;
    restorePosition();
  }

  /**
   * Get the currently visible paragraph or block and relative position inside it.
   *
   * @returns The current position, or null while content is hidden or being restored.
   */
  export function getPosition(): TextPosition | null {
    if (!scrollEl || !scrollEl.clientHeight || restoring) return null;
    const top = scrollEl.getBoundingClientRect().top + parseFloat(getComputedStyle(scrollEl).paddingTop);
    const index = blockEls.findIndex((el) => el && el.getBoundingClientRect().bottom > top);
    const block = blockEls[index];
    if (!block) return null;
    const rect = block.getBoundingClientRect();
    const offset = Math.max(0, Math.min(1, (top - rect.top) / Math.max(1, rect.height)));
    const end = scrollEl.scrollHeight - scrollEl.clientHeight;
    const percentage = end <= 0 || scrollEl.scrollTop >= end - 1 ? 100 : (scrollEl.scrollTop / end) * 100;
    return { index, offset: Math.round(offset * 1e6) / 1e6, percentage };
  }

  /** Keep the same anchor when restoring, resizing or loading EPUB illustrations. */
  function restorePosition() {
    const block = anchor && blockEls[anchor.index];
    if (!scrollEl || !block || !anchor) return;
    restoring = true;
    cancelAnimationFrame(progressFrame);
    progressFrame = 0;
    cancelAnimationFrame(restoreFrame);
    const rect = block.getBoundingClientRect();
    const top = scrollEl.getBoundingClientRect().top + parseFloat(getComputedStyle(scrollEl).paddingTop);
    scrollEl.scrollTo({
      top: scrollEl.scrollTop + rect.top - top + rect.height * anchor.offset,
      behavior: 'instant'
    });
    restoreFrame = requestAnimationFrame(() => {
      restoring = false;
      reportProgress();
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
   * Adjust a numeric reader setting within its configured range.
   *
   * @param key - The setting key to adjust.
   * @param delta - The amount to add to the current value.
   */
  function clamp(key: keyof typeof SLIDER_CONFIGS, delta: number) {
    if ($settings === null) {
      return;
    }
    const { min, max, step } = SLIDER_CONFIGS[key];
    const value = $settings[key] + delta;
    $settings[key] = Math.max(min, Math.min(max, Math.round(value / step) * step));
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
      mountId++;
      cancelAnimationFrame(progressFrame);
      cancelAnimationFrame(restoreFrame);
      freeze.set(false);
      clearTimeout(hideTimer);
    };
  });
</script>

<svelte:window onmousemove={showControls} />

<div
  role="application"
  aria-label="Text viewer"
  data-theme="light"
  class="fixed inset-0 flex flex-col transition-colors duration-300"
  style:background-color={colors.bg}
>
  <!-- top bar -->
  {#if controlsVisible}
    <div
      class="absolute inset-x-0 top-0 z-1 grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 px-2 py-1.5 backdrop-blur-sm transition-colors duration-300"
      style:color={colors.muted}
      style:background-color={colors.bar}
      transition:fade={{ duration: 200 }}
    >
      <div class="flex items-center gap-1">
        <button
          class="btn border-0 btn-ghost shadow-none btn-xs"
          style:color={colors.muted}
          onclick={() => (back ?? historyBack)()}
          aria-label="Back"
        >
          <iconify-icon icon={icons.backSolid} width="1.25rem"></iconify-icon>
        </button>
        {#if chapters.length > 1}
          <button
            class="btn border-0 btn-ghost shadow-none btn-xs"
            style:color={colors.muted}
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

      <span class="min-w-0 truncate text-center text-sm">{currentTitle}</span>

      <button
        class="btn border-0 btn-ghost shadow-none btn-xs"
        style:color={colors.muted}
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
      class="fixed top-0 left-0 z-3 flex h-full w-72 flex-col overflow-y-auto shadow-xl sm:w-80"
      style:color={colors.text}
      style:background-color={colors.panel}
      transition:fly={{ x: -300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <div class="flex items-center gap-1">
          <h3 class="text-base font-bold">{$_('media.text.chapters')}</h3>
          <!-- svelte-ignore a11y_consider_explicit_label -->
          <button class="btn border-0 bg-transparent shadow-none btn-xs" style:color={colors.text} onclick={toggleSort}>
            <iconify-icon icon={sortDesc ? icons.arrowSortDownLines : icons.arrowSortUpLines} width="1rem">
            </iconify-icon>
          </button>
        </div>
        <button
          class="btn border-0 bg-transparent shadow-none btn-xs"
          style:color={colors.text}
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
  {#if $settings !== null}
    <article
      bind:this={scrollEl}
      class="min-w-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-none transition-colors duration-300 [overflow-anchor:none]"
      style:padding="2.5rem {$settings.paddingX}rem"
      onscroll={reportProgress}
    >
      {#key mountId}
        {#if paragraphs.length || blocks.length}
          <div
            bind:this={contentEl}
            class="mx-auto max-w-3xl min-w-0 wrap-break-word [word-break:normal] transition-colors duration-300"
            style:font-family={FONTS[$settings.font]}
            style:font-size="{$settings.fontSize}px"
            style:line-height={$settings.lineHeight}
            style:color={colors.text}
          >
            {#if blocks.length}
              {#each blocks as block, index (block.id)}
                {#if block.type === 'paragraph'}
                  <p
                    bind:this={blockEls[index]}
                    class="whitespace-pre-line"
                    style:margin-bottom="{$settings.paraSpacing}em"
                  >
                    {@render textRuns(block.runs)}
                  </p>
                {:else if block.type === 'heading'}
                  <svelte:element
                    this={`h${block.level ?? 2}`}
                    bind:this={blockEls[index]}
                    class="font-bold whitespace-pre-line"
                    style:font-size={block.level === 1 ? '1.5em' : block.level === 2 ? '1.25em' : '1.125em'}
                    style:margin-bottom="{$settings.paraSpacing}em"
                  >
                    {@render textRuns(block.runs)}
                  </svelte:element>
                {:else if block.type === 'quote'}
                  <blockquote
                    bind:this={blockEls[index]}
                    class="border-s-2 border-current/30 ps-4 whitespace-pre-line"
                    style:margin-bottom="{$settings.paraSpacing}em"
                  >
                    {@render textRuns(block.runs)}
                  </blockquote>
                {:else if block.type === 'list'}
                  <svelte:element
                    this={block.ordered ? 'ol' : 'ul'}
                    bind:this={blockEls[index]}
                    start={block.ordered ? (block.start ?? 1) : undefined}
                    class="space-y-1 ps-8 {block.ordered ? 'list-decimal' : 'list-disc'}"
                    style:margin-bottom="{$settings.paraSpacing}em"
                  >
                    {#each block.items as runs, i (i)}
                      <li class="whitespace-pre-line">{@render textRuns(runs)}</li>
                    {/each}
                  </svelte:element>
                {:else if block.type === 'image'}
                  <figure bind:this={blockEls[index]} style:margin-bottom="{$settings.paraSpacing}em">
                    {#if !block.url || failedImages[block.id]}
                      <div
                        role="status"
                        class="flex min-h-32 flex-col items-center justify-center gap-2 rounded-field border border-current/20 p-4 text-center text-sm"
                      >
                        {#if block.alt}
                          <p>{block.alt}</p>
                        {/if}
                        <p>{$_(block.url ? 'media.text.image_load_failed' : 'media.text.image_unavailable')}</p>
                        {#if block.url}
                          <button
                            class="btn border-0 btn-ghost shadow-none btn-sm"
                            style:color={colors.text}
                            onclick={() => delete failedImages[block.id]}
                          >
                            {$_('action.retry')}
                          </button>
                        {/if}
                      </div>
                    {:else}
                      <img
                        src={block.url}
                        alt={block.alt}
                        class="mx-auto h-auto max-w-full"
                        loading="lazy"
                        decoding="async"
                        onerror={() => (failedImages[block.id] = true)}
                      />
                    {/if}
                  </figure>
                {/if}
              {/each}
            {:else}
              {#each paragraphs as para, i (i)}
                <p bind:this={blockEls[i]} class="indent-2" style:margin-bottom="{$settings.paraSpacing}em">
                  {#if para}
                    {para}
                  {:else}
                    &nbsp;
                  {/if}
                </p>
              {/each}
            {/if}
          </div>
        {/if}
      {/key}
    </article>
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
      class="fixed top-0 right-0 z-3 flex h-full w-72 flex-col overflow-y-auto shadow-xl sm:w-80"
      style:color={colors.text}
      style:background-color={colors.panel}
      transition:fly={{ x: 300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 class="text-base font-bold">{$_('media.text.settings')}</h3>
        <button
          class="btn border-0 bg-transparent shadow-none btn-xs"
          style:color={colors.text}
          aria-label="Close"
          onclick={() => (settingsOpen = false)}
        >
          <iconify-icon icon={icons.dismiss} width="1.125rem"></iconify-icon>
        </button>
      </div>
      <div class="flex-1 space-y-5 p-4">
        {#if $settings !== null}
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">{$_('media.text.theme')}</span>
            <div class="grid grid-cols-4 gap-2">
              {#each Object.entries(THEMES) as [key, t] (key)}
                <label
                  class="flex cursor-pointer flex-col items-center gap-1.5 rounded-field py-2 text-xs transition-all
                  {$settings.theme === key ? 'ring-2 ring-primary' : 'opacity-60 hover:opacity-100'}"
                >
                  <input type="radio" class="hidden" value={key} bind:group={$settings.theme} />
                  <span
                    class="size-5 rounded-full border shadow-sm"
                    style:background-color={t.bg}
                    style:border-color="{t.muted}50 !important"
                  ></span>
                  <span>{$_(`media.text.theme_options.${key}`)}</span>
                </label>
              {/each}
            </div>
          </div>
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">{$_('media.text.font')}</span>
            <div class="grid grid-cols-3 gap-2">
              {#each Object.entries(FONTS) as [key] (key)}
                <label
                  class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-opacity
                  {$settings.font === key ? 'bg-primary/15 outline' : 'opacity-50 hover:opacity-80'}"
                >
                  <input type="radio" class="hidden" value={key} bind:group={$settings.font} />
                  {$_(`media.text.font_options.${key}`)}
                </label>
              {/each}
            </div>
          </div>
          {@render slider('fontSize')}
          {@render slider('lineHeight')}
          {@render slider('paraSpacing')}
          {@render slider('paddingX')}
        {/if}
      </div>
    </div>
  {/if}

  <!-- bottom bar -->
  {#if controlsVisible && chapters.length > 1}
    <div
      class="absolute inset-x-0 bottom-0 z-1 flex justify-center gap-6 p-2 backdrop-blur-sm transition-colors duration-300"
      style:color={colors.muted}
      style:background-color={colors.bar}
      transition:fade={{ duration: 200 }}
    >
      <button
        class="btn border-0 btn-ghost shadow-none btn-xs disabled:opacity-20"
        style:color={colors.muted}
        aria-label="Previous chapter"
        disabled={!previousChapter}
        onclick={() => previousChapter && selectChapter(previousChapter)}
      >
        <iconify-icon icon={icons.arrowPreviousFilled} width="1.25rem"></iconify-icon>
      </button>
      <button
        class="btn border-0 btn-ghost shadow-none btn-xs disabled:opacity-20"
        style:color={colors.muted}
        aria-label="Next chapter"
        disabled={!nextChapter}
        onclick={() => nextChapter && selectChapter(nextChapter)}
      >
        <iconify-icon icon={icons.arrowNextFilled} width="1.25rem"></iconify-icon>
      </button>
    </div>
  {/if}
</div>

{#snippet textRuns(runs: ContentRun[])}
  {#each runs as run, i (i)}
    {#if run.marks.includes('strong') && run.marks.includes('em')}
      <strong><em>{run.text}</em></strong>
    {:else if run.marks.includes('strong')}
      <strong>{run.text}</strong>
    {:else if run.marks.includes('em')}
      <em>{run.text}</em>
    {:else}
      {run.text}
    {/if}
  {/each}
{/snippet}

{#snippet chapterMenu()}
  <ul class="menu w-full px-2 pb-6 text-sm">
    {#each sortDesc ? [...chapterGroups].reverse() : chapterGroups as group, groupIndex (group.volume ?? groupIndex)}
      {#if group.volume}
        <li>
          <h2 class="menu-title opacity-40" style:color={colors.text}>{group.volume}</h2>
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

{#snippet slider(key: keyof typeof SLIDER_CONFIGS)}
  {@const { min, max, step, unit } = SLIDER_CONFIGS[key]}
  <div>
    <span class="mb-1.5 flex items-center justify-between text-sm font-semibold opacity-60">
      <span>{$_(SLIDER_CONFIGS[key].i18n)}</span>
      <span class="tabular-nums">{$settings?.[key]}{unit}</span>
    </span>
    <div class="flex items-center gap-2">
      <button class="btn border! font-mono text-sm opacity-50 shadow-none btn-xs" onclick={() => clamp(key, -step)}>
        -
      </button>
      {#if $settings !== null}
        <input type="range" class="range flex-1 range-xs" {min} {max} {step} bind:value={$settings[key]} />
      {/if}
      <button class="btn border! font-mono text-sm opacity-50 shadow-none btn-xs" onclick={() => clamp(key, step)}>
        +
      </button>
    </div>
  </div>
{/snippet}
