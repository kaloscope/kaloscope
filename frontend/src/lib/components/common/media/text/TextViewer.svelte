<script lang="ts" module>
  import { persisted } from '$lib/stores';
  import type { Chapter, ChapterGroup } from '$lib/types';

  /** Delay in ms before auto-hiding the overlay controls. */
  const CONTROLS_HIDE_DELAY = 3000;

  /** Options passed to the text viewer mount function. */
  export type TextViewerOptions = {
    text: string;
    title?: string | null;
    chapters?: Chapter[];
    chapterId?: string | null;
    chapterChange?: (chapter: Chapter) => void;
  };

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
  };

  const settings = persisted<TextViewerSettings>('text-viewer', {
    theme: 'white',
    font: 'system',
    fontSize: 16,
    lineHeight: 1.8,
    paraSpacing: 1,
    paddingX: 2
  });

  const THEMES: Record<Theme, { label: string; bg: string; text: string; muted: string; panel: string; bar: string }> =
    {
      white: {
        label: '纯白',
        bg: '#fafaf5',
        text: '#333333',
        muted: '#999999',
        panel: '#ffffff',
        bar: 'rgba(0,0,0,0.06)'
      },
      cream: {
        label: '奶油',
        bg: '#fdf6e3',
        text: '#5c4b3a',
        muted: '#9a8978',
        panel: '#ffffff',
        bar: 'rgba(0,0,0,0.08)'
      },
      sepia: {
        label: '护眼',
        bg: '#f4ecd8',
        text: '#5b4636',
        muted: '#a08b76',
        panel: '#ffffff',
        bar: 'rgba(0,0,0,0.08)'
      },
      light: {
        label: '浅灰',
        bg: '#e6e6e6',
        text: '#444444',
        muted: '#888888',
        panel: '#ffffff',
        bar: 'rgba(0,0,0,0.08)'
      },
      green: {
        label: '豆绿',
        bg: '#dce8d8',
        text: '#3a4a3a',
        muted: '#6b7b6b',
        panel: '#ffffff',
        bar: 'rgba(0,0,0,0.08)'
      },
      dark: {
        label: '深色',
        bg: '#2b2b2b',
        text: '#cccccc',
        muted: '#666666',
        panel: '#222222',
        bar: 'rgba(0,0,0,0.5)'
      },
      slate: {
        label: '蓝灰',
        bg: '#1a2128',
        text: '#b0bec5',
        muted: '#546e7a',
        panel: '#1e242c',
        bar: 'rgba(0,0,0,0.5)'
      },
      black: {
        label: '夜间',
        bg: '#000000',
        text: '#aaaaaa',
        muted: '#444444',
        panel: '#1a1a1a',
        bar: 'rgba(0,0,0,0.5)'
      }
    };

  const FONTS: Record<Font, { label: string; family: string }> = {
    system: {
      label: '系统',
      family: 'var(--font-sans)'
    },
    sans: {
      label: '黑体',
      family: '"Noto Sans SC", "Noto Sans CJK SC", "Noto Sans", "PingFang SC", "Microsoft YaHei", sans-serif'
    },
    serif: {
      label: '宋体',
      family: '"Noto Serif SC", "Noto Serif CJK SC", "Noto Serif", "Songti SC", "STSong", serif'
    },
    kai: {
      label: '楷体',
      family: '"Kaiti SC", "KaiTi", "STKaiti", "ST Kaiti", "楷体", "楷体_GB2312", serif'
    },
    mono: {
      label: '等宽',
      family: '"Noto Sans Mono CJK SC", "Noto Sans Mono", "SF Mono", "Cascadia Code", monospace'
    }
  };

  const SLIDER_CONFIGS = {
    fontSize: { label: '字号', min: 12, max: 28, step: 1, unit: 'px' },
    lineHeight: { label: '行间距', min: 1.4, max: 3.0, step: 0.2, unit: '' },
    paraSpacing: { label: '段间距', min: 0, max: 2, step: 0.5, unit: 'em' },
    paddingX: { label: '左右边距', min: 0, max: 4, step: 0.5, unit: 'rem' }
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
  import { icons } from '$lib/icons';
  import { freeze, historyBack } from '$lib/stores';
  import { onMount } from 'svelte';
  import { fade, fly } from 'svelte/transition';

  // resource title
  let title = $state('');
  // text content of the current chapter
  let content = $state('');
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

  // current theme colors
  let colors = $derived(THEMES[$settings?.theme ?? 'white']);
  // content split into paragraphs
  let paragraphs = $derived(content.split(/\n{2,}/).map((para) => para.trim()));

  /**
   * Mount the text viewer with the given text resource.
   *
   * @param options - The text viewer options.
   */
  export function mount(options: TextViewerOptions) {
    if (!options || !options.text) {
      return;
    }
    title = options.title ?? '';
    content = options.text ?? '';
    chapters = options.chapters ?? [];
    chapterId = options.chapterId ?? null;
    chapterChange = options.chapterChange;
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
      freeze.set(false);
      clearTimeout(hideTimer);
    };
  });
</script>

<svelte:window onmousemove={showControls} />

<div
  role="application"
  aria-label="Text viewer"
  class="fixed inset-0 flex flex-col transition-colors duration-300"
  style:background-color={colors.bg}
>
  <!-- top bar -->
  {#if controlsVisible}
    <div
      class="absolute top-0 inset-x-0 z-1 grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 px-2 py-1.5 backdrop-blur-sm transition-colors duration-300"
      style:color={colors.muted}
      style:background-color={colors.bar}
      transition:fade={{ duration: 200 }}
    >
      <div class="flex items-center gap-1">
        <button
          class="btn btn-xs btn-ghost border-0 shadow-none"
          style:color={colors.muted}
          onclick={() => historyBack()}
          aria-label="Back"
        >
          <iconify-icon icon={icons.back} width="1.25rem"></iconify-icon>
        </button>
        {#if chapters.length > 1}
          <button
            class="btn btn-xs btn-ghost border-0 shadow-none"
            style:color={colors.muted}
            aria-label="Chapters"
            onclick={() => {
              chaptersOpen = true;
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
        style:color={colors.muted}
        aria-label="Reading settings"
        onclick={() => (settingsOpen = !settingsOpen)}
      >
        <iconify-icon icon={icons.settings} width="1.125rem"></iconify-icon>
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
      class="fixed left-0 top-0 z-3 flex h-full w-72 flex-col overflow-y-auto shadow-xl sm:w-80"
      style:background-color={colors.panel}
      style:color={colors.text}
      transition:fly={{ x: -300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 class="text-base font-bold">章节</h3>
        <button
          class="btn btn-xs border-0 bg-transparent shadow-none"
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
      class="min-w-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-none transition-all duration-300"
      style:padding="2rem {$settings.paddingX}rem 0"
    >
      {#if content}
        <div
          class="mx-auto min-w-0 max-w-3xl wrap-break-word transition-all duration-300 [word-break:normal]"
          style:font-family={FONTS[$settings.font].family}
          style:font-size="{$settings.fontSize}px"
          style:line-height={$settings.lineHeight}
          style:color={colors.text}
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

  <!-- settings panel -->
  {#if settingsOpen}
    <button
      class="fixed inset-0 z-2 bg-black/20"
      aria-label="Close settings"
      onclick={() => (settingsOpen = false)}
      transition:fade={{ duration: 150 }}
    ></button>
    <div
      class="fixed right-0 top-0 z-3 flex h-full w-72 flex-col overflow-y-auto shadow-xl sm:w-80"
      style:background-color={colors.panel}
      style:color={colors.text}
      transition:fly={{ x: 300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 class="text-base font-bold">阅读设置</h3>
        <button
          class="btn btn-xs border-0 bg-transparent shadow-none"
          aria-label="Close"
          onclick={() => (settingsOpen = false)}
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
      class="absolute bottom-0 inset-x-0 z-1 flex justify-center gap-6 p-2 backdrop-blur-sm transition-colors duration-300"
      style:background-color={colors.bar}
      style:color={colors.muted}
      transition:fade={{ duration: 200 }}
    >
      <button
        class="btn btn-xs btn-ghost border-0 shadow-none disabled:opacity-20"
        style:color={colors.muted}
        aria-label="Previous chapter"
        disabled={!previousChapter}
        onclick={() => previousChapter && selectChapter(previousChapter)}
      >
        <iconify-icon icon={icons.arrowPrevious} width="1.25rem"></iconify-icon>
      </button>
      <button
        class="btn btn-xs btn-ghost border-0 shadow-none disabled:opacity-20"
        style:color={colors.muted}
        aria-label="Next chapter"
        disabled={!nextChapter}
        onclick={() => nextChapter && selectChapter(nextChapter)}
      >
        <iconify-icon icon={icons.arrowNext} width="1.25rem"></iconify-icon>
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

{#snippet slider(key: keyof typeof SLIDER_CONFIGS)}
  {@const { label, min, max, step, unit } = SLIDER_CONFIGS[key]}
  <div>
    <span class="mb-1.5 flex items-center justify-between text-sm font-semibold opacity-60">
      <span>{label}</span>
      <span class="tabular-nums">{$settings?.[key]}{unit}</span>
    </span>
    <div class="flex items-center gap-2">
      <button class="btn btn-xs text-sm font-mono border opacity-50" onclick={() => clamp(key, -step)}>-</button>
      {#if $settings !== null}
        <input type="range" class="range range-xs flex-1" {min} {max} {step} bind:value={$settings[key]} />
      {/if}
      <button class="btn btn-xs text-sm font-mono border opacity-50" onclick={() => clamp(key, step)}>+</button>
    </div>
  </div>
{/snippet}
