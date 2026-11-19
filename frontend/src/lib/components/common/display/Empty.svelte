<script lang="ts" module>
  import type { IconifyIcon } from 'iconify-icon';
  import type { Snippet } from 'svelte';

  export type EmptyProps = {
    /** The illustration icon; defaults to the current page's icon. */
    icon?: string | IconifyIcon;
    /** The headline of the placeholder. */
    title?: string;
    /** The supporting text below the headline. */
    description?: string;
    /** Whether to use the smaller inline layout. */
    compact?: boolean;
    /** The class names for the container. */
    class?: string;
    /** The actions rendered below the text. */
    children?: Snippet;
  };

  // the backdrop's drawing area and grid spacing, in SVG units
  const MESH_W = 320;
  const MESH_H = 240;
  const MESH_STEP = 22;

  /**
   * Bend a grid point into a gentle wave, so straight lines curve softly.
   *
   * @param x - Horizontal grid position.
   * @param y - Vertical grid position.
   * @returns The warped point.
   */
  function warp(x: number, y: number): [number, number] {
    return [x + 6 * Math.sin(y / 46 + x / 140), y + 7 * Math.sin(x / 52 + 1.2) + 3 * Math.cos(y / 70)];
  }

  /**
   * Build the path data of every grid line, extending past the edges so the fade hides their ends.
   *
   * @returns One SVG path per horizontal and vertical grid line.
   */
  function meshPaths(): string[] {
    const margin = MESH_STEP * 2;
    const line = (points: [number, number][]) =>
      'M' + points.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(' L');
    const paths: string[] = [];
    for (let y = -margin; y <= MESH_H + margin; y += MESH_STEP) {
      const points: [number, number][] = [];
      for (let x = -margin; x <= MESH_W + margin; x += 8) points.push(warp(x, y));
      paths.push(line(points));
    }
    for (let x = -margin; x <= MESH_W + margin; x += MESH_STEP) {
      const points: [number, number][] = [];
      for (let y = -margin; y <= MESH_H + margin; y += 8) points.push(warp(x, y));
      paths.push(line(points));
    }
    return paths;
  }

  // computed once and shared by every placeholder
  const MESH = meshPaths();
</script>

<script lang="ts">
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { menuIcon, navIcon } from '$lib/stores.svelte';
  import { fade } from 'svelte/transition';

  let { icon, title, description, compact = false, class: _class, children }: EmptyProps = $props();

  // an explicit icon, else the page's menu route, else its section
  let shownIcon = $derived(icon ?? menuIcon.current ?? navIcon.current ?? icons.collectionsEmpty);
</script>

<div
  class="empty flex-col-center w-full text-center {compact ? 'gap-3 py-6' : 'gap-5 py-12'} {_class}"
  in:fade={{ duration: 200 }}
>
  <div class="empty-art flex-center" class:empty-compact={compact}>
    <div class="empty-backdrop" aria-hidden="true">
      <svg class="empty-mesh" viewBox="0 0 {MESH_W} {MESH_H}" preserveAspectRatio="xMidYMid slice">
        <g fill="none" stroke="currentColor" stroke-width="0.8" stroke-linecap="round">
          {#each MESH as d, i (i)}
            <path {d} />
          {/each}
        </g>
      </svg>
    </div>
    <span class="empty-badge flex-center" aria-hidden="true">
      <iconify-icon icon={shownIcon} width={compact ? '2rem' : '2.75rem'}></iconify-icon>
    </span>
  </div>
  <div class="relative flex max-w-md flex-col gap-1.5 px-4">
    <span class="font-semibold text-base-content/50 {compact ? 'text-sm' : 'text-base'}">
      {title ?? $_('data.nodata')}
    </span>
    {#if description}
      <p class="leading-relaxed text-balance text-base-content/40 {compact ? 'text-xs' : 'text-sm'}">
        {description}
      </p>
    {/if}
  </div>
  {#if children}
    <div class="relative flex flex-wrap items-center justify-center gap-2">
      {@render children()}
    </div>
  {/if}
</div>

<style>
  .empty {
    /* close to the selected menu item, for browsers without the exact match below */
    --empty-tint: var(--color-surface);
    isolation: isolate;
  }

  /* the selected menu item's color itself, lifted on dark themes so the faint tint still shows */
  @supports (color: light-dark(#000, #fff)) and (color: oklch(from #000 l c h)) {
    .empty {
      --empty-tint: light-dark(var(--color-emphasis), oklch(from var(--color-emphasis) 0.8 c h));
    }
  }

  .empty-art {
    position: relative;
    width: 5.5rem;
    height: 5.5rem;
  }

  .empty-compact {
    width: 4rem;
    height: 4rem;
  }

  /* a softly curved grid with a faint glow, fading out in an oval around the badge */
  .empty-backdrop {
    position: absolute;
    z-index: -1;
    top: 50%;
    left: 50%;
    width: 20rem;
    height: 15rem;
    pointer-events: none;
    transform: translate(-50%, -46%);
    mask-image: radial-gradient(ellipse 46% 50% at 50% 46%, #000 20%, transparent 85%);
  }

  .empty-compact .empty-backdrop {
    width: 14rem;
    height: 10.5rem;
  }

  .empty-backdrop::before {
    position: absolute;
    inset: 0;
    background:
      radial-gradient(ellipse at 44% 40%, var(--empty-tint), transparent 62%),
      radial-gradient(ellipse at 66% 62%, var(--empty-tint), transparent 58%);
    filter: blur(16px);
    opacity: 0.07;
    content: '';
  }

  .empty-mesh {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    color: var(--empty-tint);
    opacity: 0.1;
  }

  .empty-badge {
    width: 100%;
    height: 100%;
    border-radius: 32%;
    background-color: var(--color-base-100);
    background-image: linear-gradient(
      140deg,
      color-mix(in oklab, var(--empty-tint) 7%, transparent),
      color-mix(in oklab, var(--empty-tint) 2%, transparent)
    );
    color: color-mix(in oklab, var(--empty-tint) 55%, transparent);
    box-shadow:
      0 0 0 1px color-mix(in oklab, var(--empty-tint) 12%, transparent),
      0 8px 20px -12px color-mix(in oklab, var(--empty-tint) 30%, transparent);
    transform: rotate(-6deg);

    :global(iconify-icon) {
      transform: rotate(6deg);
    }
  }
</style>
