<script lang="ts" module>
  export type RankingProps = {
    /** The ranking number to display. */
    rank?: number | string | null;
    /** Whether to render the compact table-view style. */
    compact?: boolean;
    /** The class names for the ranking badge. */
    class?: string;
  };
</script>

<script lang="ts">
  let { rank: _rank, compact = false, class: _class }: RankingProps = $props();

  // the formatted ranking to display
  let rank: string = $derived.by(() => {
    const num = Number(_rank);
    if (!Number.isFinite(num) || num <= 0 || num > 100) {
      return '';
    }
    return num.toFixed(0);
  });

  // the visual style for the ranking badge
  let rankClass = $derived.by(() => {
    switch (rank) {
      case '1':
        return 'ranking-first';
      case '2':
        return 'ranking-second';
      case '3':
        return 'ranking-third';
      default:
        return 'ranking-default';
    }
  });
</script>

{#if rank}
  <span class="ranking-badge flex-center {rankClass} {_class}" class:ranking-compact={compact} aria-label="Rank {rank}">
    {rank}
  </span>
{/if}

<style>
  .ranking-badge {
    min-width: 1.75rem;
    height: 1.5rem;
    padding: 0 0.5rem;
    border-radius: 0 0 0.45rem 0;
    color: white;
    font-weight: 800;
    line-height: 1;
    box-shadow: 0 0.125rem 0.5rem rgb(0 0 0 / 18%);
  }

  .ranking-compact {
    min-width: 1.35rem;
    height: 1.15rem;
    padding: 0 0.35rem;
    border-radius: 0 0 0.35rem 0;
    font-size: 0.65rem;
    box-shadow: 0 0.0625rem 0.25rem rgb(0 0 0 / 14%);
  }

  .ranking-first {
    background: linear-gradient(135deg, #ffd84d, #ff9f1c);
  }

  .ranking-second {
    background: linear-gradient(135deg, #9ee7ff, #5ba8ff);
  }

  .ranking-third {
    background: linear-gradient(135deg, #ffcfbc, #ff8f70);
  }

  .ranking-default {
    background: linear-gradient(135deg, #9aa6b8, #6f7d95);
  }
</style>
