<script lang="ts">
  import { _ } from '$lib/i18n';
  import type { Handle as HandleProps } from '$lib/types';
  import { Handle, useNodeConnections, useSvelteFlowStore } from '@xyflow/svelte';
  import { onDestroy } from 'svelte';
  import type { Instance } from 'tippy.js';
  import tippy from 'tippy.js';

  type NodeHandleProps = { nodeId: string } & HandleProps;
  let { nodeId, id, handle_type: type, position, maxconn, style }: NodeHandleProps = $props();

  // check if the handle is connectable
  const store = useSvelteFlowStore();
  const connections = useNodeConnections(() => ({ id: nodeId, handleType: type, handleId: id }));
  const isConnectable = $derived(store.nodesConnectable && connections.current.length < maxconn);

  // dynamic class names
  const bgClass = $derived(type === 'source' ? '!bg-base-300' : '!bg-base-100');
  const sizeClass = $derived(`!size-2 ${isConnectable ? 'group-hover:!size-4' : ''}`);

  // tippy instance
  let tipInst: Instance;
  $effect(() => {
    let tipContent = `${$_(`flow.node.handle.${id}`, { default: id })} (${connections.current.length}/${maxconn})`;
    if (tipInst) {
      tipInst.setContent(tipContent);
    } else {
      tipInst = tippy(`[data-nodeid='${nodeId}'][data-handleid='${id}']`, {
        content: tipContent,
        placement: position,
        theme: 'handle',
        followCursor: true
      })[0];
    }
  });

  onDestroy(() => {
    tipInst?.destroy();
  });
</script>

<Handle
  {id}
  {type}
  {position}
  {isConnectable}
  style={style ?? ''}
  class="border-base-content/40! transition-all {bgClass} {sizeClass}"
/>
