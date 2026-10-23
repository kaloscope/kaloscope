<script lang="ts">
  import { assistantCanvas } from '$lib/assistant/canvas';
  import { BaseEdge, EdgeLabel, getBezierPath, useEdges, type EdgeProps } from '@xyflow/svelte';

  let { id, sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition, markerEnd }: EdgeProps = $props();
  let [path, labelX, labelY] = $derived(
    // draw a curved edge between two nodes
    getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition })
  );

  // get all edges
  const edges = useEdges();

  // check if the edge is selected
  const selected = $derived(edges.current.find((edge) => edge.id === id)?.selected ?? false);

  /**
   * Delete the edge.
   */
  function deleteEdge() {
    if ($assistantCanvas) return;
    edges.current = edges.current.filter((edge) => edge.id !== id);
  }
</script>

<BaseEdge {path} {markerEnd} />
<EdgeLabel x={labelX} y={labelY} class="absolute -top-0.5 bg-transparent! opacity-50 hover:opacity-100">
  <button class="cursor-pointer text-3xl {selected ? '' : 'hidden'}" disabled={!!$assistantCanvas} onclick={deleteEdge}>
    x
  </button>
</EdgeLabel>
