<script lang="ts" module>
  import type { Chapter } from '$lib/types';
  import type { Snippet } from 'svelte';
  import type { MouseEventHandler } from 'svelte/elements';

  /** Colors of the reader frame, as CSS color values. */
  export type ReaderColors = {
    /** The page background. */
    bg: string;
    /** Text in the chapter and settings panels. */
    text: string;
    /** Icons and the title in the top and bottom bars. */
    muted: string;
    /** The chapter and settings panel background. */
    panel: string;
    /** The translucent top and bottom bar background. */
    bar: string;
  };

  export type ReaderFrameProps = {
    /** The accessible name of the reader. */
    label: string;
    /** The daisyUI theme used inside the reader. */
    theme: 'light' | 'dark';
    /** The frame colors. */
    colors: ReaderColors;
    /** The title shown in the top bar. */
    title?: string | null;
    /** Extra content shown after the title, such as a page counter. */
    titleExtra?: Snippet;
    /** The chapters of the resource; the chapter list and bottom bar need more than one. */
    chapters: Chapter[];
    /** The id of the current chapter. */
    chapterId: string | null;
    /** Whether the chapter list is in descending order. */
    sortDesc: boolean;
    /** Whether the top and bottom bars are visible. */
    controlsVisible: boolean;
    /** Whether the chapter panel is open. */
    chaptersOpen?: boolean;
    /** Whether the settings panel is open. */
    settingsOpen?: boolean;
    /** The reading area and any overlays of the reader. */
    children: Snippet;
    /** The body of the settings panel. */
    settingsPanel: Snippet;
    /** Leave the reader. */
    onback: () => void;
    /** Open a chapter picked from the list or the bottom bar. */
    onselect: (chapter: Chapter) => void;
    /** Toggle the chapter list order. */
    onsort: () => void;
    /** React to the chapter panel opening, for example to keep the bars shown. */
    onchaptersopen?: () => void;
    /** The click event handler of the reader. */
    onclick?: MouseEventHandler<HTMLDivElement>;
  };
</script>

<script lang="ts">
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { groupChapters, matchChapterId } from '$lib/utils/chapter';
  import { fade, fly } from 'svelte/transition';

  let {
    label,
    theme,
    colors,
    title,
    titleExtra,
    chapters,
    chapterId,
    sortDesc,
    controlsVisible,
    chaptersOpen = $bindable(false),
    settingsOpen = $bindable(false),
    children,
    settingsPanel,
    onback,
    onselect,
    onsort,
    onchaptersopen,
    onclick
  }: ReaderFrameProps = $props();

  // chapters grouped by volume in source order
  let chapterGroups = $derived(groupChapters(chapters));
  // index of the current chapter within the chapters array
  let chapterIndex = $derived(chapters.findIndex((c) => matchChapterId(c.id, chapterId)));
  // previous chapter in sequence, or null if at the first
  let previousChapter = $derived(chapterIndex > 0 ? chapters[chapterIndex - 1] : null);
  // next chapter in sequence, or null if at the last
  let nextChapter = $derived(
    chapterIndex >= 0 && chapterIndex < chapters.length - 1 ? chapters[chapterIndex + 1] : null
  );

  /**
   * Close the chapter panel and open a chapter.
   *
   * @param chapter - Chapter to open.
   */
  function select(chapter: Chapter) {
    chaptersOpen = false;
    onselect(chapter);
  }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
  role="application"
  aria-label={label}
  data-theme={theme}
  class="fixed inset-0 flex flex-col transition-colors duration-300"
  style:background-color={colors.bg}
  style:--reader-muted={colors.muted}
  {onclick}
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
          class="reader-button btn border-0 btn-ghost shadow-none btn-xs"
          aria-label={$_('action.back')}
          onclick={onback}
        >
          <iconify-icon icon={icons.backSolid} width="1.25rem"></iconify-icon>
        </button>
        {#if chapters.length > 1}
          <button
            class="reader-button btn border-0 btn-ghost shadow-none btn-xs"
            aria-label={$_('media.chapters')}
            onclick={() => {
              chaptersOpen = true;
              onchaptersopen?.();
            }}
          >
            <iconify-icon icon={icons.menuFoldSolid} width="1.5rem"></iconify-icon>
          </button>
        {/if}
      </div>

      <span class="flex-center min-w-0 text-sm">
        <span class="truncate" class:mr-2={!!titleExtra}>{title}</span>
        {@render titleExtra?.()}
      </span>

      <button
        class="reader-button btn border-0 btn-ghost shadow-none btn-xs"
        aria-label={$_('media.reader_settings')}
        onclick={() => (settingsOpen = !settingsOpen)}
      >
        <iconify-icon icon={icons.settingsFilled} width="1.25rem"></iconify-icon>
      </button>
    </div>
  {/if}

  <!-- chapter panel -->
  {#if chaptersOpen}
    {@render scrim($_('action.close', $_('media.chapters')), () => (chaptersOpen = false))}
    <div
      class="fixed top-0 left-0 z-3 flex h-full w-72 flex-col overflow-y-auto shadow-xl sm:w-80"
      style:color={colors.text}
      style:background-color={colors.panel}
      transition:fly={{ x: -300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <div class="flex items-center gap-1">
          <h3 class="text-base font-bold">{$_('media.chapters')}</h3>
          <button
            class="btn border-0 bg-transparent shadow-none btn-xs"
            style:color={colors.text}
            aria-label={$_('media.sort_chapters')}
            aria-pressed={sortDesc}
            onclick={onsort}
          >
            <iconify-icon icon={sortDesc ? icons.arrowSortDownLines : icons.arrowSortUpLines} width="1rem">
            </iconify-icon>
          </button>
        </div>
        {@render closeButton(() => (chaptersOpen = false))}
      </div>
      <ul class="menu w-full px-2 pb-6 text-sm">
        {#each sortDesc ? [...chapterGroups].reverse() : chapterGroups as group, groupIndex (group.volume ?? groupIndex)}
          {#if group.volume}
            <li>
              <h2 class="menu-title opacity-40" style:color={colors.text}>{group.volume}</h2>
              <ul>
                {#each group.chapters as chapter, index (chapter.id ?? index)}
                  {@render chapterItem(chapter)}
                {/each}
              </ul>
            </li>
          {:else}
            {#each sortDesc ? [...group.chapters].reverse() : group.chapters as chapter, index (chapter.id ?? index)}
              {@render chapterItem(chapter)}
            {/each}
          {/if}
        {/each}
      </ul>
    </div>
  {/if}

  {@render children()}

  <!-- settings panel -->
  {#if settingsOpen}
    {@render scrim($_('action.close', $_('media.reader_settings')), () => (settingsOpen = false))}
    <div
      class="fixed top-0 right-0 z-3 flex h-full w-72 flex-col overflow-y-auto shadow-xl sm:w-80"
      style:color={colors.text}
      style:background-color={colors.panel}
      transition:fly={{ x: 300, duration: 200 }}
    >
      <div class="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 class="text-base font-bold">{$_('media.reader_settings')}</h3>
        {@render closeButton(() => (settingsOpen = false))}
      </div>
      <div class="flex-1 space-y-5 p-4">
        {@render settingsPanel()}
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
        class="reader-button btn border-0 btn-ghost shadow-none btn-xs"
        aria-label={$_('media.previous_chapter')}
        disabled={!previousChapter}
        onclick={() => previousChapter && select(previousChapter)}
      >
        <iconify-icon icon={icons.arrowPreviousFilled} width="1.25rem"></iconify-icon>
      </button>
      <button
        class="reader-button btn border-0 btn-ghost shadow-none btn-xs"
        aria-label={$_('media.next_chapter')}
        disabled={!nextChapter}
        onclick={() => nextChapter && select(nextChapter)}
      >
        <iconify-icon icon={icons.arrowNextFilled} width="1.25rem"></iconify-icon>
      </button>
    </div>
  {/if}
</div>

<!-- the dimmed backdrop behind a side panel, closing it when clicked -->
{#snippet scrim(text: string, close: () => void)}
  <button class="fixed inset-0 z-2 bg-black/20" aria-label={text} onclick={close} transition:fade={{ duration: 150 }}
  ></button>
{/snippet}

{#snippet closeButton(close: () => void)}
  <button
    class="btn border-0 bg-transparent shadow-none btn-xs"
    style:color={colors.text}
    aria-label={$_('action.close')}
    onclick={close}
  >
    <iconify-icon icon={icons.dismiss} width="1.125rem"></iconify-icon>
  </button>
{/snippet}

{#snippet chapterItem(chapter: Chapter)}
  <li class="mb-0.75">
    <button
      class="h-auto min-h-0 py-2 whitespace-normal {matchChapterId(chapter.id, chapterId) ? 'menu-active' : ''}"
      title={chapter.title}
      onclick={() => select(chapter)}
    >
      <span class="min-w-0 text-left wrap-break-word">{chapter.title}</span>
    </button>
  </li>
{/snippet}

<style>
  /* bar buttons take the bar's muted color, and a tint of it on hover */
  .reader-button {
    color: var(--reader-muted);

    &:disabled {
      background-color: transparent;
      color: var(--reader-muted);
      opacity: 0.2;
    }

    &:not(:disabled):is(:hover, :focus-visible) {
      background-color: color-mix(in oklab, var(--reader-muted) 20%, transparent);
    }
  }
</style>
