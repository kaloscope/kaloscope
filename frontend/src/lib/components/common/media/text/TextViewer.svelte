<script lang="ts" module>
  import { persisted } from '$lib/stores';
  import type { Chapter } from '$lib/types';

  export type ReaderTheme = 'white' | 'sepia' | 'dark' | 'black';
  export type ReaderFont = 'system' | 'serif' | 'sans' | 'kai' | 'mono';

  export type ReaderSettings = {
    theme: ReaderTheme;
    font: ReaderFont;
    fontSize: number;
    lineHeight: number;
    paraSpacing: number;
    margin: number;
  };

  export type TextViewerOptions = {
    text: string;
    title?: string | null;
    chapters?: Chapter[];
    chapterId?: string | null;
    chapterChange?: (chapter: Chapter) => void;
  };

  type ChapterGroup = {
    volume: string | null;
    chapters: Chapter[];
  };

  const settings = persisted<ReaderSettings>('reader', {
    theme: 'white',
    font: 'system',
    fontSize: 16,
    lineHeight: 1.8,
    paraSpacing: 1,
    margin: 2
  });

  const THEMES: Record<ReaderTheme, { bg: string; text: string; muted: string; label: string }> = {
    white: { bg: '#f5f5f0', text: '#333', muted: '#999', label: '白色' },
    sepia: { bg: '#f4ecd8', text: '#5b4636', muted: '#a08b76', label: '护眼' },
    dark: { bg: '#2b2b2b', text: '#ccc', muted: '#666', label: '深色' },
    black: { bg: '#000', text: '#aaa', muted: '#444', label: '夜间' }
  };

  const FONTS: Record<ReaderFont, { family: string; label: string }> = {
    system: { family: 'inherit', label: '系统默认' },
    serif: { family: '"Noto Serif SC", "Songti SC", "STSong", serif', label: '宋体' },
    sans: { family: '"Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif', label: '黑体' },
    kai: { family: '"KaiTi", "STKaiti", "楷体", serif', label: '楷体' },
    mono: { family: '"Noto Sans Mono SC", "SF Mono", "Cascadia Code", monospace', label: '等宽' }
  };

  const RANGES: Record<string, [number, number, number]> = {
    fontSize: [12, 28, 1],
    lineHeight: [1.4, 3.0, 0.2],
    paraSpacing: [0, 2, 0.5],
    margin: [0, 4, 0.5]
  };

  const PANEL_COLORS: Record<ReaderTheme, { panel: string; topBar: string }> = {
    white: { panel: '#fff', topBar: 'rgba(0,0,0,0.08)' },
    sepia: { panel: '#fff', topBar: 'rgba(0,0,0,0.08)' },
    dark: { panel: '#222', topBar: 'rgba(0,0,0,0.5)' },
    black: { panel: '#1a1a1a', topBar: 'rgba(0,0,0,0.5)' }
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
  let colors = $derived(PANEL_COLORS[$settings?.theme ?? 'white']);
  let currentTitle = $derived(chapters.find((c) => c.id === chapterId)?.title ?? title);
  let chapterGroups = $derived(groupChapters(chapters));
  let paragraphs = $derived(content.split(/\n{2,}/).map((para) => para.trim()));

  export function mount(options: TextViewerOptions) {
    if (!options) return;
    content = options.text ?? '';
    title = options.title ?? '';
    chapters = options.chapters ?? [];
    chapterId = options.chapterId ?? null;
    chapterChange = options.chapterChange;
    resetTimer();
  }

  function selectChapter(chapter: Chapter) {
    chapterOpen = false;
    chapterChange?.(chapter);
  }

  function clamp(key: 'fontSize' | 'lineHeight' | 'paraSpacing' | 'margin', delta: number) {
    if ($settings === null) {
      return;
    }
    const [min, max, step] = RANGES[key];
    const value = $settings[key] + delta;
    $settings[key] = Math.max(min, Math.min(max, Math.round(value / step) * step));
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

<svelte:window onmousemove={resetTimer} />

<div class="flex size-full flex-col transition-colors duration-300" style:background-color={t.bg}>
  <!-- Top bar -->
  {#if visible}
    <div
      class="absolute top-0 left-0 right-0 z-10 grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 px-2 py-1.5 backdrop-blur-sm transition-colors duration-300"
      style:background-color={colors.topBar}
      style:color={t.muted}
      transition:fade={{ duration: 200 }}
    >
      <div class="flex items-center gap-1">
        <button
          class="btn btn-xs btn-ghost border-0 shadow-none"
          style:color={t.muted}
          onclick={() => historyBack()}
          aria-label="返回"
        >
          <iconify-icon icon={icons.back} width="1.25rem"></iconify-icon>
        </button>
        {#if chapters.length > 1}
          <button
            class="btn btn-xs btn-ghost border-0 shadow-none"
            style:color={t.muted}
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

      <span class="min-w-0 truncate text-center text-sm">{currentTitle}</span>

      <button
        class="btn btn-xs btn-ghost justify-self-end border-0 shadow-none"
        style:color={t.muted}
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
      class="fixed inset-0 z-10 bg-black/20"
      aria-label="关闭章节列表"
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
          aria-label="关闭"
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
      style:padding="0 {$settings.margin}rem"
    >
      {#if content}
        <div
          class="mx-auto min-w-0 max-w-3xl break-words transition-all duration-300 [word-break:normal]"
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
      aria-label="关闭设置"
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
        <button class="btn btn-xs border-0 bg-transparent shadow-none" aria-label="关闭" onclick={() => (open = false)}>
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
          {@render slider('页边距', 'margin', 'rem', 0.5, '−', '+')}
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

{#snippet slider(
  label: string,
  key: 'fontSize' | 'lineHeight' | 'paraSpacing' | 'margin',
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
