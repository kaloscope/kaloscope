<script lang="ts" module>
  export type SkeletonProps = {
    /** The placeholder layout. */
    type?: 'card' | 'row' | 'block';
    /** The number of placeholders to render. */
    count?: number;
    /** The aspect ratio of the cover area; rows have no cover without it. */
    ratio?: string | null;
    /** The number of text lines under or beside the cover. */
    lines?: number;
    /** Whether the card has no surrounding surface, like a poster. */
    plain?: boolean;
    /** The class names for each placeholder. */
    class?: string;
  };

  // the repeating text line widths
  const WIDTHS = ['85%', '60%', '72%', '48%'];
</script>

<script lang="ts">
  let { type = 'card', count = 1, ratio, lines = 2, plain = false, class: _class }: SkeletonProps = $props();

  // `auto` covers have no intrinsic size until loaded
  let aspectRatio = $derived(!ratio || ratio === 'auto' ? '16/9' : ratio);
</script>

{#each { length: count }, i}
  {#if type === 'card'}
    <li
      class="flex min-w-0 list-none flex-col overflow-hidden {plain ? '' : 'card-surface'} {_class}"
      aria-hidden="true"
    >
      <div
        class="w-full skeleton skeleton-soft {plain ? 'rounded-card' : 'rounded-none'}"
        style:aspect-ratio={aspectRatio}
      ></div>
      <div class="flex flex-col gap-2 {plain ? 'items-center px-1 py-2' : 'p-2.5'}">
        {#each { length: lines }, j}
          <div class="h-3 skeleton skeleton-soft" style:width={WIDTHS[(i + j) % WIDTHS.length]}></div>
        {/each}
      </div>
    </li>
  {:else if type === 'row'}
    <div class="flex items-center gap-3 px-2 py-3 {_class}" aria-hidden="true">
      {#if ratio}
        <div class="h-14 shrink-0 skeleton skeleton-soft" style:aspect-ratio={aspectRatio}></div>
      {/if}
      <div class="flex min-w-0 grow flex-col gap-2.5">
        {#each { length: lines }, j}
          <div class="h-3 skeleton skeleton-soft" style:width={WIDTHS[(i + j) % WIDTHS.length]}></div>
        {/each}
      </div>
    </div>
  {:else}
    <div class="skeleton skeleton-soft {_class}" aria-hidden="true"></div>
  {/if}
{/each}
