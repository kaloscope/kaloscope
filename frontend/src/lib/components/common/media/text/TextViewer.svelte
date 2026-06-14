<script lang="ts" module>
  import { persisted } from '$lib/stores';
  import type { Chapter } from '$lib/types';

  /** Color theme for the text viewer. */
  export type TextTheme = 'white' | 'cream' | 'sepia' | 'light' | 'green' | 'dark' | 'slate' | 'black';
  /** Font family for the text viewer. */
  export type TextFont = 'system' | 'serif' | 'sans' | 'kai' | 'mono';

  /** Persisted settings for the text viewer. */
  export type TextViewerSettings = {
    theme: TextTheme;
    font: TextFont;
    fontSize: number;
    lineHeight: number;
    paraSpacing: number;
    paddingX: number;
  };

  /** Options passed to the text viewer mount function. */
  export type TextViewerOptions = {
    text: string;
    title?: string | null;
    chapters?: Chapter[];
    chapterId?: string | null;
    chapterChange?: (chapter: Chapter) => void;
  };

  /** Group of chapters keyed by volume name. */
  type ChapterGroup = {
    volume: string | null;
    chapters: Chapter[];
  };

  const settings = persisted<TextViewerSettings>('text-viewer', {
    theme: 'white',
    font: 'system',
    fontSize: 16,
    lineHeight: 1.8,
    paraSpacing: 1,
    paddingX: 2
  });

  const THEMES: Record<
    TextTheme,
    { bg: string; text: string; muted: string; panel: string; bar: string; label: string }
  > = {
    white: {
      bg: '#fafaf5',
      text: '#333333',
      muted: '#999999',
      panel: '#ffffff',
      bar: 'rgba(0,0,0,0.06)',
      label: '纯白'
    },
    cream: {
      bg: '#fdf6e3',
      text: '#5c4b3a',
      muted: '#9a8978',
      panel: '#ffffff',
      bar: 'rgba(0,0,0,0.08)',
      label: '奶油'
    },
    sepia: {
      bg: '#f4ecd8',
      text: '#5b4636',
      muted: '#a08b76',
      panel: '#ffffff',
      bar: 'rgba(0,0,0,0.08)',
      label: '护眼'
    },
    light: {
      bg: '#e6e6e6',
      text: '#444444',
      muted: '#888888',
      panel: '#ffffff',
      bar: 'rgba(0,0,0,0.08)',
      label: '浅灰'
    },
    green: {
      bg: '#dce8d8',
      text: '#3a4a3a',
      muted: '#6b7b6b',
      panel: '#ffffff',
      bar: 'rgba(0,0,0,0.08)',
      label: '豆绿'
    },
    dark: {
      bg: '#2b2b2b',
      text: '#cccccc',
      muted: '#666666',
      panel: '#222222',
      bar: 'rgba(0,0,0,0.5)',
      label: '深色'
    },
    slate: {
      bg: '#1a2128',
      text: '#b0bec5',
      muted: '#546e7a',
      panel: '#1e242c',
      bar: 'rgba(0,0,0,0.5)',
      label: '蓝灰'
    },
    black: {
      bg: '#000000',
      text: '#aaaaaa',
      muted: '#444444',
      panel: '#1a1a1a',
      bar: 'rgba(0,0,0,0.5)',
      label: '夜间'
    }
  };

  const FONTS: Record<TextFont, { family: string; label: string }> = {
    system: {
      family: 'var(--font-sans)',
      label: '系统'
    },
    sans: {
      family: '"Noto Sans SC", "Noto Sans CJK SC", "Noto Sans", "PingFang SC", "Microsoft YaHei", sans-serif',
      label: '黑体'
    },
    serif: {
      family: '"Noto Serif SC", "Noto Serif CJK SC", "Noto Serif", "Songti SC", "STSong", serif',
      label: '宋体'
    },
    kai: {
      family: '"Kaiti SC", "KaiTi", "STKaiti", "ST Kaiti", "楷体", "楷体_GB2312", serif',
      label: '楷体'
    },
    mono: {
      family: '"Noto Sans Mono CJK SC", "Noto Sans Mono", "SF Mono", "Cascadia Code", monospace',
      label: '等宽'
    }
  };

  const RANGES: Record<string, [number, number, number]> = {
    fontSize: [12, 28, 1],
    lineHeight: [1.4, 3.0, 0.2],
    paraSpacing: [0, 2, 0.5],
    paddingX: [0, 4, 0.5]
  };

  /**
   * Normalize chapter ids before comparing them with ids read from the URL.
   *
   * @param id - The chapter id to normalize.
   * @returns The decoded chapter id, or null when it is missing.
   */
  function normalizeChapterId(id: string | null | undefined) {
    if (!id) {
      return null;
    }
    try {
      return decodeURIComponent(id);
    } catch {
      return id;
    }
  }

  /**
   * Check whether two chapter ids refer to the same chapter.
   *
   * @param left - The first chapter id.
   * @param right - The second chapter id.
   * @returns Whether the normalized ids match.
   */
  function matchChapterId(left: string | null | undefined, right: string | null | undefined) {
    const normalized = normalizeChapterId(left);
    return !!normalized && normalized === normalizeChapterId(right);
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
  import { icons } from '$lib/icons';
  import { freeze, historyBack } from '$lib/stores';
  import { onMount } from 'svelte';
  import { fade, fly } from 'svelte/transition';

  let content = $state('');
  let title = $state('');
  let chapters = $state<Chapter[]>([]);
  let chapterId = $state<string | null>(null);
  let chapterChange = $state<((c: Chapter) => void) | undefined>(undefined);
  let open = $state(false);
  let chapterOpen = $state(false);
  let visible = $state(true);

  let t = $derived(THEMES[$settings?.theme ?? 'white']);
  let colors = $derived(THEMES[$settings?.theme ?? 'white']);
  let chapterGroups = $derived(groupChapters(chapters));
  let currentChapterIndex = $derived.by(() => chapters.findIndex((chapter) => matchChapterId(chapter.id, chapterId)));
  let currentChapter = $derived(currentChapterIndex >= 0 ? chapters[currentChapterIndex] : null);
  let currentTitle = $derived(currentChapter?.title ?? title);
  let previousChapter = $derived(currentChapterIndex > 0 ? chapters[currentChapterIndex - 1] : null);
  let nextChapter = $derived(
    currentChapterIndex >= 0 && currentChapterIndex < chapters.length - 1 ? chapters[currentChapterIndex + 1] : null
  );
  let paragraphs = $derived(content.split(/\n{2,}/).map((para) => para.trim()));

  /**
   * Mount the text viewer with the given text resource.
   *
   * @param options - The text viewer options.
   */
  export function mount(options: TextViewerOptions) {
    if (!options) return;
    content = options.text ?? '';
    title = options.title ?? '';
    chapters = options.chapters ?? [];
    chapterId = options.chapterId ?? null;
    chapterChange = options.chapterChange;
    resetTimer();
  }

  /**
   * Select a chapter and notify the parent page.
   *
   * @param chapter - The chapter to select.
   */
  function selectChapter(chapter: Chapter) {
    chapterOpen = false;
    chapterId = chapter.id ?? null;
    chapterChange?.(chapter);
  }

  /**
   * Select the previous chapter when one is available.
   */
  function selectPreviousChapter() {
    if (previousChapter) {
      selectChapter(previousChapter);
    }
  }

  /**
   * Select the next chapter when one is available.
   */
  function selectNextChapter() {
    if (nextChapter) {
      selectChapter(nextChapter);
    }
  }

  /**
   * Adjust a numeric reader setting within its configured range.
   *
   * @param key - The setting key to adjust.
   * @param delta - The amount to add to the current value.
   */
  function clamp(key: 'fontSize' | 'lineHeight' | 'paraSpacing' | 'paddingX', delta: number) {
    if ($settings === null) {
      return;
    }
    const [min, max, step] = RANGES[key];
    const value = $settings[key] + delta;
    $settings[key] = Math.max(min, Math.min(max, Math.round(value / step) * step));
  }

  let hideTimer: ReturnType<typeof setTimeout>;
  /**
   * Show transient controls and restart the auto-hide timer.
   */
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

<svelte:window onmousemove={resetTimer} />

<div
  role="application"
  aria-label="Text viewer"
  class="fixed inset-0 flex flex-col transition-colors duration-300"
  style:background-color={t.bg}
>
  <!-- Top bar -->
  {#if visible}
    <div
      class="absolute top-0 left-0 right-0 z-10 grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 px-2 py-1.5 backdrop-blur-sm transition-colors duration-300"
      style:background-color={colors.bar}
      style:color={t.muted}
      transition:fade={{ duration: 200 }}
    >
      <div class="flex items-center gap-1">
        <button
          class="btn btn-xs btn-ghost border-0 shadow-none"
          style:color={t.muted}
          onclick={() => historyBack()}
          aria-label="Back"
        >
          <iconify-icon icon={icons.back} width="1.25rem"></iconify-icon>
        </button>
        {#if chapters.length > 1}
          <button
            class="btn btn-xs btn-ghost border-0 shadow-none"
            style:color={t.muted}
            aria-label="Chapters"
            onclick={() => {
              chapterOpen = true;
              clearTimeout(hideTimer);
            }}
          >
            <iconify-icon icon={icons.arrowSortDownLines} width="1.125rem"></iconify-icon>
          </button>
        {/if}
      </div>

      <span class="min-w-0 truncate text-center text-sm">{currentTitle}</span>

      <button
        class="btn btn-xs btn-ghost justify-self-end border-0 shadow-none"
        style:color={t.muted}
        aria-label="Reading settings"
        onclick={() => (open = !open)}
      >
        <iconify-icon icon={icons.settings} width="1.125rem"></iconify-icon>
      </button>
    </div>
  {/if}

  <!-- Chapter panel -->
  {#if chapterOpen}
    <button
      class="fixed inset-0 z-10 bg-black/20"
      aria-label="Close chapter list"
      onclick={() => (chapterOpen = false)}
      transition:fade={{ duration: 150 }}
    ></button>
    <div
      class="fixed left-0 top-0 z-20 flex h-full w-72 flex-col overflow-y-auto shadow-xl sm:w-80"
      style:background-color={colors.panel}
      style:color={t.text}
      transition:fly={{ x: -300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 class="text-base font-bold">章节</h3>
        <button
          class="btn btn-xs border-0 bg-transparent shadow-none"
          aria-label="Close"
          onclick={() => (chapterOpen = false)}
        >
          <iconify-icon icon={icons.dismiss} width="1.125rem"></iconify-icon>
        </button>
      </div>
      {@render chapterMenu()}
    </div>
  {/if}

  <!-- Reading area -->
  {#if $settings !== null}
    <article
      class="min-w-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-none transition-all duration-300"
      style:padding="2rem {$settings.paddingX}rem 0"
    >
      {#if content}
        <div
          class="mx-auto min-w-0 max-w-3xl wrap-break-word transition-all duration-300 [word-break:normal]"
          style:font-family={FONTS[$settings.font].family}
          style:font-size="{$settings.fontSize}px"
          style:line-height={$settings.lineHeight}
          style:color={t.text}
        >
          {#each paragraphs as para, index (index)}
            <p class="indent-2" style:margin-bottom="{$settings.paraSpacing}em">
              {#if para}
                {para}
              {:else}
                &nbsp;
              {/if}
            </p>
          {/each}
        </div>
      {/if}
    </article>
  {/if}

  <!-- Settings panel -->
  {#if open}
    <button
      class="fixed inset-0 z-10 bg-black/20"
      aria-label="Close settings"
      onclick={() => (open = false)}
      transition:fade={{ duration: 150 }}
    ></button>
    <div
      class="fixed right-0 top-0 z-20 flex h-full w-72 flex-col overflow-y-auto shadow-xl sm:w-80"
      style:background-color={colors.panel}
      style:color={t.text}
      transition:fly={{ x: 300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 class="text-base font-bold">阅读设置</h3>
        <button
          class="btn btn-xs border-0 bg-transparent shadow-none"
          aria-label="Close"
          onclick={() => (open = false)}
        >
          <iconify-icon icon={icons.dismiss} width="1.125rem"></iconify-icon>
        </button>
      </div>
      <div class="flex-1 space-y-5 px-4 pb-6">
        {#if $settings !== null}
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">背景主题</span>
            <div class="grid grid-cols-4 gap-2">
              {#each Object.entries(THEMES) as [key, th] (key)}
                <label
                  class="flex cursor-pointer flex-col items-center gap-1.5 rounded-field py-2 text-xs transition-all {$settings.theme ===
                  key
                    ? 'ring-2 ring-primary'
                    : 'opacity-60 hover:opacity-100'}"
                >
                  <input type="radio" class="hidden" value={key} bind:group={$settings.theme} />
                  <span
                    class="size-5 rounded-full border shadow-sm"
                    style:background-color={th.bg}
                    style:border-color={th.bg === '#f5f5f0' || th.bg === '#f4ecd8' ? '#00000020' : '#ffffff30'}
                  ></span>
                  <span>{th.label}</span>
                </label>
              {/each}
            </div>
          </div>
          <div>
            <span class="mb-1.5 block text-sm font-semibold opacity-60">字体</span>
            <div class="grid grid-cols-3 gap-2">
              {#each Object.entries(FONTS) as [key, f] (key)}
                <label
                  class="cursor-pointer rounded-field py-2 text-center text-xs font-medium transition-all {$settings.font ===
                  key
                    ? 'bg-primary/15 text-primary'
                    : 'opacity-50 hover:opacity-80'}"
                >
                  <input type="radio" class="hidden" value={key} bind:group={$settings.font} />
                  {f.label}
                </label>
              {/each}
            </div>
          </div>
          {@render slider('字号', 'fontSize', 'px', 1, 'A−', 'A+')}
          {@render slider('行间距', 'lineHeight', '', 0.2, '−', '+')}
          {@render slider('段间距', 'paraSpacing', 'em', 0.5, '−', '+')}
          {@render slider('左右边距', 'paddingX', 'rem', 0.5, '−', '+')}
        {/if}
      </div>
    </div>
  {/if}

  <!-- Bottom bar -->
  {#if visible && chapters.length > 1}
    <div
      class="absolute bottom-0 left-0 right-0 z-10 flex justify-center gap-4 px-2 py-2 backdrop-blur-sm transition-colors duration-300"
      style:background-color={colors.bar}
      style:color={t.muted}
      transition:fade={{ duration: 200 }}
    >
      <button
        class="btn btn-xs btn-ghost border-0 shadow-none disabled:opacity-20"
        style:color={t.muted}
        aria-label="Previous chapter"
        disabled={!previousChapter}
        onclick={() => selectPreviousChapter()}
      >
        <iconify-icon icon={icons.arrowUp} width="1.125rem"></iconify-icon>
      </button>
      <button
        class="btn btn-xs btn-ghost border-0 shadow-none disabled:opacity-20"
        style:color={t.muted}
        aria-label="Next chapter"
        disabled={!nextChapter}
        onclick={() => selectNextChapter()}
      >
        <iconify-icon icon={icons.arrowDown} width="1.125rem"></iconify-icon>
      </button>
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
      class={matchChapterId(chapter.id, chapterId) ? 'menu-active' : ''}
      title={chapter.title}
      onclick={() => selectChapter(chapter)}
    >
      {chapter.title}
    </button>
  </li>
{/snippet}

{#snippet slider(
  label: string,
  key: 'fontSize' | 'lineHeight' | 'paraSpacing' | 'paddingX',
  unit: string,
  step: number,
  left: string,
  right: string
)}
  {@const [min, max] = RANGES[key]}
  <div>
    <span class="mb-1.5 flex items-center justify-between text-sm font-semibold opacity-60">
      <span>{label}</span>
      <span class="tabular-nums">{$settings?.[key]}{unit}</span>
    </span>
    <div class="flex items-center gap-2">
      <button class="btn btn-xs border opacity-60" onclick={() => clamp(key, -step)}>{left}</button>
      {#if $settings !== null}
        <input type="range" class="range range-xs flex-1" {min} {max} {step} bind:value={$settings[key]} />
      {/if}
      <button class="btn btn-xs border opacity-60" onclick={() => clamp(key, step)}>{right}</button>
    </div>
  </div>
{/snippet}
