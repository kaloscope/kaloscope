<script lang="ts" module>
  export type BackdropProps = {
    /** The source URL of the background image. */
    src: string | null;
    /** An image shown as a soft color wash when `src` is missing, such as a poster. */
    fallback?: string | null;
    /** Whether to proxy the image through the server. */
    proxy?: boolean | 'store';
    /**
     * Where the backdrop is drawn: `screen` fills the viewport behind the page, while `header` is a band at the top
     * of the positioned parent that scrolls with it and fades into the content below.
     */
    layout?: 'screen' | 'header';
    /** The value for the blur filter applied to the background image; defaults depend on `layout`. */
    blur?: string;
    /** The opacity of the background image; defaults depend on `layout`. */
    opacity?: string;
  };
</script>

<script lang="ts">
  import { proxyImage } from '$lib/api';
  import { fade } from 'svelte/transition';

  let { src, fallback = null, proxy = false, layout = 'screen', blur, opacity }: BackdropProps = $props();

  const wash = $derived(!src && !!fallback);
  const url = $derived(src || fallback ? proxyImage((src || fallback)!, proxy) : null);
  // a wash always uses its own heavy blur so a portrait poster never shows as a stretched picture
  const filter = $derived(wash ? 'blur(40px) saturate(1.4)' : `blur(${blur ?? (layout === 'header' ? '2px' : '5px')})`);
  const alpha = $derived(wash ? '0.45' : (opacity ?? (layout === 'header' ? '0.35' : '0.2')));
</script>

{#if url}
  {#key url}
    {#if layout === 'header'}
      <div
        class="backdrop-header pointer-events-none absolute inset-x-0 top-0 z-0 h-[min(75svh,38rem)] overflow-hidden"
        style:opacity={alpha}
        transition:fade
      >
        <img
          src={url}
          alt=""
          class="size-full object-cover object-[center_25%]"
          style:filter
          style:transform="scale({wash ? 1.2 : 1.02})"
        />
      </div>
    {:else}
      <div
        class="pointer-events-none fixed inset-0 z-0"
        style="
          background-image: url({url});
          background-size: cover;
          background-position: center;
          background-repeat: no-repeat;
          opacity: {alpha};
          filter: {filter};
          mask: radial-gradient(ellipse 80% 70% at center 60%, black 0%, black 50%, transparent 80%);
        "
        transition:fade
      ></div>
    {/if}
  {/key}
{/if}

<style>
  /* fade the band out toward the content instead of dimming the whole page */
  .backdrop-header {
    mask-image: linear-gradient(to bottom, black 0%, rgb(0 0 0 / 55%) 45%, transparent 100%);
  }
</style>
