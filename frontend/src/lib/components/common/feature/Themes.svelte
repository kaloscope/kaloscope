<script lang="ts" module>
  import type { DropdownProps } from '$lib/components';
  import { persisted } from '$lib/stores.svelte';
  import { MediaQuery } from 'svelte/reactivity';

  export type ThemesProps = {
    /** Default dark theme. */
    dark?: string;
    /** Default light theme. */
    light?: string;
    /** The PWA theme color property. */
    pwaThemeColor?: string;
  } & Pick<DropdownProps, 'class' | 'triggerClass' | 'contentClass'>;

  /**
   * List of available themes.
   *
   * https://daisyui.com/docs/themes/
   */
  export const AVAILABLE_THEMES = [
    'light',
    'dark',
    'cupcake',
    'bumblebee',
    'emerald',
    'corporate',
    'synthwave',
    'retro',
    'cyberpunk',
    'valentine',
    'halloween',
    'garden',
    'forest',
    'aqua',
    'lofi',
    'pastel',
    'fantasy',
    'wireframe',
    'black',
    'luxury',
    'dracula',
    'cmyk',
    'autumn',
    'business',
    'acid',
    'lemonade',
    'night',
    'coffee',
    'winter',
    'dim',
    'nord',
    'sunset',
    'caramellatte',
    'abyss',
    'silk'
  ];

  // the persisted theme
  const savedTheme = persisted<string>('theme');

  // the preferred color scheme media query
  const darkMode = new MediaQuery('(prefers-color-scheme: dark)');
</script>

<script lang="ts">
  import { Dropdown, Modal } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { onMount, untrack } from 'svelte';

  let {
    dark = 'dark',
    light = 'light',
    pwaThemeColor = '--color-base-100',
    class: _class,
    triggerClass,
    contentClass
  }: ThemesProps = $props();

  // the modal dialog for the theme switcher
  let modal: Modal;
  export const showModal = () => modal.show();

  /**
   * Switch the daisyUI theme by setting the `data-theme` attribute on the root element.
   *
   * @param theme - Theme to activate.
   */
  function switchTheme(theme: string) {
    savedTheme.current = theme;
    const root = document.documentElement;
    root.setAttribute('data-theme', theme);
    // synchronize the PWA theme color with the specified theme color variable
    const colorElement = document.getElementById('pwa-theme-color');
    const themeColor = colorElement && getComputedStyle(colorElement).backgroundColor;
    const metaTag = document.querySelector('meta[name="theme-color"]');
    if (metaTag && themeColor) {
      // https://caniuse.com/meta-theme-color
      // safari on iOS >= 26.0 doesn't support the `theme-color` meta tag anymore
      metaTag.setAttribute('content', themeColor);
    }
  }

  onMount(() => {
    // a first visit follows the preferred color scheme
    switchTheme(savedTheme.current || (darkMode.current ? dark : light));
  });

  // follow later changes of the preferred color scheme
  let lastMatches: boolean | undefined;
  $effect(() => {
    const matches = darkMode.current;
    untrack(() => {
      if (lastMatches !== undefined && matches !== lastMatches) {
        const theme = savedTheme.current;
        // only switch theme if it's one of the defaults
        if (!theme || [dark, light].includes(theme)) {
          switchTheme(matches ? dark : light);
        }
      }
      lastMatches = matches;
    });
  });
</script>

{#snippet themes()}
  {#each AVAILABLE_THEMES as theme (theme)}
    <li data-theme={theme} class="rounded-field">
      <button
        class="bg-base-100 font-sans shadow-sm hover:bg-base-300 {savedTheme.current === theme ? '[&_svg]:block' : ''}"
        onclick={() => {
          switchTheme(theme);
        }}
      >
        <span class="size-3">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 24 24"
            fill="currentColor"
            class="pointer-events-none hidden size-3 shrink-0"
          >
            <path d="M20.285 2l-11.285 11.567-5.286-5.011-3.714 3.716 9 8.728 15-15.285z"></path>
          </svg>
        </span>
        {theme}
        <span class="flex h-full gap-1">
          <span class="w-2 rounded-selector bg-primary"></span>
          <span class="w-2 rounded-selector bg-secondary"></span>
          <span class="w-2 rounded-selector bg-accent"></span>
          <span class="w-2 rounded-selector bg-neutral"></span>
        </span>
      </button>
    </li>
  {/each}
{/snippet}

<Dropdown triggerIcon={icons.colorSwatch} contentMaxHeight="50vh" class={_class} {triggerClass} {contentClass}>
  <ul class="menu gap-1">
    {@render themes()}
  </ul>
</Dropdown>

<Modal icon={icons.colorSwatch} title={$_('app.switch_theme')} bind:this={modal}>
  <ul class="menu max-h-[50vh] w-full flex-nowrap gap-1 overflow-y-auto rounded-card border">
    {@render themes()}
  </ul>
</Modal>

<!-- this div is used to set the PWA theme color dynamically based on the selected theme -->
<div id="pwa-theme-color" style="background-color: var({pwaThemeColor}); display: none;"></div>
