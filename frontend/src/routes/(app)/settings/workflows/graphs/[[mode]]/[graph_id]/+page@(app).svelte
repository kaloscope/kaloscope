<script lang="ts">
  import { page } from '$app/state';
  import { FlowDesigner } from '$lib/components';
</script>

{#if page.params.graph_id}
  {#key `${page.params.mode ?? ''}/${page.params.graph_id}`}
    <FlowDesigner id={page.params.graph_id} readonly={page.params.mode === 'r'} />
  {/key}
{/if}
