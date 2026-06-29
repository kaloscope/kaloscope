<script lang="ts" module>
  export type SignpostsProps = {
    /** The class names for the signposts. */
    class?: string;
  };
</script>

<script lang="ts">
  import { _ } from '$lib/i18n';
  import { signposts } from '$lib/stores';

  let { class: _class }: SignpostsProps = $props();
</script>

{#if $signposts.length > 0}
  <div class="flex items-baseline gap-1 truncate px-2 {_class}">
    {#each $signposts as signpost, index (index)}
      {@const last = index === $signposts.length - 1}
      {@const title = typeof signpost === 'string' ? signpost : signpost.title}
      {@const translated = typeof signpost === 'string' || signpost.translate !== false}
      <span class="font-medium text-surface/90 {last ? 'truncate' : 'opacity-90'}">
        {translated ? $_(title, ['']) : title}
      </span>
      {#if !last}
        <span class="font-title opacity-40">&gt;</span>
      {/if}
    {/each}
  </div>
{/if}
