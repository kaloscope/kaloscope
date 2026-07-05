<script lang="ts" module>
  export type RatingProps = {
    /** The rating score to display. */
    score?: number | null;
    /** The minimum value of the rating. */
    min?: number;
    /** The maximum value of the rating. */
    max?: number;
    /** The number of decimal places to show. */
    decimals?: number;
    /** Whether to render the compact table-view style. */
    compact?: boolean;
    /** The class names for the rating badge. */
    class?: string;
  };
</script>

<script lang="ts">
  import { icons } from '$lib/icons';
  import { fixedNumber } from '$lib/utils';

  let { score: _score, min = 0, max = 10, decimals = 1, compact = false, class: _class }: RatingProps = $props();

  // the formatted score to display
  let score: string = $derived(fixedNumber(_score, decimals, min, max));
</script>

{#if score}
  <span class="rating-badge flex-center {_class}" class:rating-compact={compact}>
    <iconify-icon icon={icons.starFilled} width="0.8em" class="rating-star"></iconify-icon>
    {score}
  </span>
{/if}

<style>
  .rating-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 2rem;
    gap: 0.125rem;
    padding: 0.2rem 0.4rem;
    border: 1px solid rgb(255 255 255 / 16%);
    border-radius: var(--radius-selector);
    background: linear-gradient(135deg, rgb(18 20 24 / 58%), rgb(18 20 24 / 28%));
    color: #f4cf72;
    font-weight: 750;
    line-height: 1;
    letter-spacing: 0;
    text-shadow: 0 0.0625rem 0.125rem rgb(0 0 0 / 32%);
    box-shadow:
      0 0.0625rem 0.375rem rgb(0 0 0 / 16%),
      inset 0 0.0625rem 0 rgb(255 255 255 / 14%);
    backdrop-filter: blur(0.45rem) saturate(1.1);
  }

  .rating-compact {
    min-width: 1.55rem;
    gap: 0.075rem;
    padding: 0.15rem 0.3rem;
    border-width: 0;
    border-radius: calc(infinity * 1px);
    font-size: 0.625rem;
    box-shadow:
      0 0.0625rem 0.25rem rgb(0 0 0 / 14%),
      inset 0 0.0625rem 0 rgb(255 255 255 / 12%);
  }

  .rating-star {
    display: flex;
    flex-shrink: 0;
    color: #f1d78f;
    filter: drop-shadow(0 0.0625rem 0.0625rem rgb(0 0 0 / 24%));
    transform: translateY(-0.015em);
  }
</style>
