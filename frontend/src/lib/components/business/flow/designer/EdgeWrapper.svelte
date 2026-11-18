<script lang="ts">
  import { assistantCanvas } from '$lib/assistant/canvas';
  import { BaseEdge, EdgeLabel, getBezierPath, useSvelteFlow, type EdgeProps } from '@xyflow/svelte';

  let { id, sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition, markerEnd, selected }: EdgeProps =
    $props();
  let [path, labelX, labelY] = $derived(
    // draw a curved edge between two nodes
    getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition })
  );

  const { deleteElements } = useSvelteFlow();

  /**
   * Delete the edge.
   */
  function deleteEdge() {
    if ($assistantCanvas) return;
    void deleteElements({ edges: [{ id }] });
  }
</script>

<BaseEdge {path} {markerEnd} />
<EdgeLabel x={labelX} y={labelY} class="absolute -top-0.5 bg-transparent! opacity-50 hover:opacity-100">
  <button class="cursor-pointer text-3xl {selected ? '' : 'hidden'}" disabled={!!$assistantCanvas} onclick={deleteEdge}>
    x
  </button>
</EdgeLabel>
