<script lang="ts" module>
  import type { IconifyIcon } from 'iconify-icon';
  import type { MouseEventHandler } from 'svelte/elements';

  export type ImageProps = Partial<{
    /** The key of the preset image. */
    preset: string | null;
    /** Whether to proxy the image through the server. */
    proxy: boolean | 'auto' | 'store';
    /** The source URL of the image. */
    src: string | null;
    /** The fallback text for the image. */
    text: string | null;
    /** The fallback icon for the image. */
    icon: string | IconifyIcon;
    /** Whether to change the image slowly. */
    sluggish: boolean;
    /** The aspect ratio of the image. */
    ratio: string;
    /** The width of the image. */
    width: string;
    /** The height of the image. */
    height: string;
    /** Whether to use a circular image. */
    circle: boolean;
    /** Whether to add a border to the image. */
    border: boolean;
    /** Whether to add a shadow to the image. */
    shadow: boolean;
    /** Whether to use a transparent background. */
    transparent: boolean;
    /** The class names for the image. */
    class: string;
    /** The click event handler. */
    onclick: MouseEventHandler<HTMLDivElement>;
  }>;

  /**
   * Get the whole URL string of the static file.
   *
   * @param url - The relative URL of the static file.
   */
  const resolve = (url: string) => new URL(url, import.meta.url).href;

  /**
   * Some preset image sources.
   */
  const PRESETS: Record<string, string> = {
    edge: resolve('/icons/edge.png'),
    chrome: resolve('/icons/chrome.svg'),
    'mobile chrome': resolve('/icons/chrome.svg'),
    firefox: resolve('/icons/firefox.svg'),
    'mobile firefox': resolve('/icons/firefox.svg'),
    safari: resolve('/icons/safari.svg'),
    'mobile safari': resolve('/icons/safari.svg'),
    aria2: resolve('/icons/aria2.png'),
    qbittorrent: resolve('/icons/qbittorrent.ico'),
    transmission: resolve('/icons/transmission.ico')
  };

  /**
   * Extract the display initial character from the given string.
   *
   * @param str - The first unicode letter or number in the string.
   */
  function extractInitial(str: string): string {
    const match = str.match(/[\p{L}\p{N}]/u);
    return (match ? match[0] : str[0]).toLocaleUpperCase();
  }
</script>

<script lang="ts">
  import { proxyImage } from '$lib/api';
  import { icons } from '$lib/icons';
  import { debounce } from '$lib/utils';
  import { fade } from 'svelte/transition';

  let {
    preset = null,
    proxy = 'auto',
    src: _src = preset ? PRESETS[preset] : null,
    text: _text = null,
    icon = icons.image,
    sluggish = false,
    ratio,
    width,
    height,
    circle = false,
    border = false,
    shadow = false,
    transparent = false,
    class: _class,
    onclick
  }: ImageProps = $props();

  // svelte-ignore state_referenced_locally
  let src: string | null = $state(_src);
  // svelte-ignore state_referenced_locally
  let text: string | null = $state(_text);

  // update the `src` and `text` states slowly to prevent flickering
  const _sluggish = debounce((s: string | null, t: string | null) => {
    src = s;
    text = t;
  });
  $effect(() => {
    if (sluggish) {
      _sluggish(_src, _text);
    } else {
      src = _src;
      text = _text;
    }
  });

  // the default aspect ratio
  let aspectRatio = $derived(ratio || '1/1');

  // the default size of the image
  let size = $derived.by(() => {
    if (aspectRatio === 'auto' || (width && height)) {
      return undefined;
    } else if (!width && !height) {
      return '2.5rem';
    } else if (!ratio) {
      return width || height;
    }
  });

  // the dynamic class names
  let roundedClass = $derived(circle ? 'rounded-full' : 'rounded-sm');
  let btnClass = $derived(onclick ? 'btn-scale' : '');
  let bgClass = $derived(transparent ? 'bg-transparent' : !src ? 'bg-base-300/70' : '');
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div
  tabindex={onclick ? 0 : undefined}
  role={onclick ? 'button' : 'generic'}
  class="flex-center shrink-0 rotate-0 overflow-hidden {roundedClass} {btnClass} {bgClass} {_class}"
  class:shadow-sm={shadow && src}
  class:border
  style:width={width || size}
  style:height={height || size}
  style:aspect-ratio={aspectRatio}
  {onclick}
>
  {#if src}
    {#key src}
      <img
        src={proxyImage(src, proxy)}
        alt=""
        class="size-full object-cover"
        loading="lazy"
        draggable={false}
        in:fade
      />
    {/key}
  {:else if text}
    <span class="text-xl font-bold opacity-60">{extractInitial(text)}</span>
  {:else if icon}
    <iconify-icon {icon} width="100%" height="100%" style:aspect-ratio={aspectRatio} class="opacity-50"></iconify-icon>
  {/if}
</div>
