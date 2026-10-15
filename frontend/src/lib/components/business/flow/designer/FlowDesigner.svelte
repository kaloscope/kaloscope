<script lang="ts">
  import { tooltip } from '$lib/actions';
  import { api } from '$lib/api';
  import { Overlay } from '$lib/components';
  import { LOOP_TAG } from '$lib/constants';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { mediaQuery } from '$lib/stores';
  import type { FlowGraph, NodeSchema, Resp } from '$lib/types';
  import {
    Background,
    BackgroundVariant,
    MarkerType,
    SvelteFlow,
    useStore,
    useSvelteFlow,
    type Edge,
    type IsValidConnection,
    type Node
  } from '@xyflow/svelte';
  import { onMount, setContext } from 'svelte';
  import { writable } from 'svelte/store';
  import { fly } from 'svelte/transition';
  import { hideAll } from 'tippy.js';
  import { v7 as uuidv7 } from 'uuid';
  import { waitForGraphWrites } from '../graphWrites';
  import ControlBar from './ControlBar.svelte';
  import EdgeWrapper from './EdgeWrapper.svelte';
  import NodeSchemas from './NodeSchemas.svelte';
  import NodeWrapper from './NodeWrapper.svelte';

  const { id: graphId, readonly }: { id: string; readonly: boolean } = $props();
  const { screenToFlowPosition } = useSvelteFlow();
  const sidebar = $derived(readonly ? writable<boolean | null>(false) : mediaQuery('(min-width: 40rem)'));
  const loading = createLoading();
  const store = $derived(useStore());

  let nodes = $state.raw<Node[]>([]);
  let edges = $state.raw<Edge[]>([]);
  let graph: FlowGraph | null = $state(null);
  let jsonGraph: string | null = $state(null);
  let interactive: boolean = $derived(store.nodesDraggable || store.nodesConnectable || store.elementsSelectable);

  /**
   * Handle the drag over event.
   *
   * @param event - Drag event.
   */
  function ondragover(event: DragEvent) {
    event.preventDefault();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'copy';
    }
  }

  /**
   * Handle the drop event.
   *
   * @param event - Drag event.
   */
  function ondrop(event: DragEvent) {
    event.preventDefault();
    if (!event.dataTransfer || !interactive) {
      return;
    }
    const data = event.dataTransfer.getData('application/svelteflow');
    const schema: NodeSchema = JSON.parse(data);
    nodes.push({
      id: uuidv7(),
      type: 'node',
      position: screenToFlowPosition({
        x: event.clientX,
        y: event.clientY
      }),
      data: { $schema: { ...schema } },
      origin: [0.5, 0.0]
    });
    nodes = [...nodes];
  }

  /**
   * Check if the connection is valid.
   *
   * @param conn - Connection to check.
   */
  const isValidConnection: IsValidConnection = (conn) => {
    // the node can't connect to itself directly
    if (conn.source === conn.target) {
      return false;
    }
    // extract the tags of the node handles
    const tags = nodes.reduce(
      (nodes, node) => {
        const schema = node.data.$schema as NodeSchema;
        nodes[node.id] = schema.handles.reduce(
          (handles, handle) => {
            handles[handle.id] = handle.tag;
            return handles;
          },
          {} as Record<string, string | null>
        );
        return nodes;
      },
      {} as Record<string, Record<string, string | null>>
    );

    // construct the node-sources and node-targets maps
    const incomers = { [`${conn.target}`]: [{ source: conn.source, sourceHandle: conn.sourceHandle }] };
    const outgoers = { [`${conn.source}`]: [{ target: conn.target, targetHandle: conn.targetHandle }] };
    for (const edge of edges) {
      const target = edge.target;
      if (!incomers[target]) {
        incomers[target] = [];
      }
      incomers[target].push({ source: edge.source, sourceHandle: edge.sourceHandle });

      const source = edge.source;
      if (!outgoers[source]) {
        outgoers[source] = [];
      }
      outgoers[source].push({ target: edge.target, targetHandle: edge.targetHandle });
    }

    // check if the connection is valid
    const conflictingTags = findConflictingTags(
      tags,
      incomers,
      outgoers,
      new Set([conn.source, conn.target]),
      new Set(),
      new Set()
    );
    if (conflictingTags.size > 1) {
      return false;
    }
    const sourceTag = findLoopSourceTag(tags, incomers, conn.source, conn.sourceHandle, conn.target, new Set());
    if (sourceTag === '' && findLoopTag(tags, conn.target, conn.targetHandle) !== conn.target) {
      return false;
    }
    const targetTag = findLoopTargetTag(tags, outgoers, conn.target, conn.targetHandle, conn.source, new Set());
    if (targetTag === '' && findLoopTag(tags, conn.source, conn.sourceHandle) !== conn.source) {
      return false;
    }
    if (sourceTag && targetTag && sourceTag !== targetTag) {
      return false;
    }
    return true;
  };

  /**
   * Find the conflicting tags.
   *
   * @param tags - Tags of the node handles.
   * @param incomers - Node-sources map.
   * @param outgoers - Node-targets map.
   * @param nodeIds - Node IDs to check.
   * @param checked - Checked node IDs.
   * @param result - Result set.
   */
  function findConflictingTags(
    tags: Record<string, Record<string, string | null>>,
    incomers: Record<string, { source: string; sourceHandle?: string | null }[]>,
    outgoers: Record<string, { target: string; targetHandle?: string | null }[]>,
    nodeIds: Set<string>,
    checked: Set<string>,
    result: Set<string>
  ): Set<string> {
    // eslint-disable-next-line svelte/prefer-svelte-reactivity
    const nextNodeIds = new Set<string>();
    nodeIds.forEach((nodeId) => checked.add(nodeId));
    for (const nodeId of nodeIds) {
      incomers[nodeId]?.forEach((source) => {
        const sourceId = source.source;
        const sourceTag = findTag(tags, sourceId, source.sourceHandle);
        if (sourceTag) {
          result.add(sourceTag);
        }
        if (!checked.has(sourceId)) {
          nextNodeIds.add(sourceId);
        }
      });
      outgoers[nodeId]?.forEach((target) => {
        const targetId = target.target;
        const targetTag = findTag(tags, targetId, target.targetHandle);
        if (targetTag) {
          result.add(targetTag);
        }
        if (!checked.has(targetId)) {
          nextNodeIds.add(targetId);
        }
      });
      if (result.size > 1) {
        return result;
      }
    }
    if (nextNodeIds.size === 0) {
      return result;
    }
    return findConflictingTags(tags, incomers, outgoers, nextNodeIds, checked, result);
  }

  /**
   * Find the first loop source tag.
   *
   * @param tags - Tags of the node handles.
   * @param incomers - Node-sources map.
   * @param nodeId - Source node ID.
   * @param handleId - Source handle ID.
   * @param closeId - Close node ID.
   * @param checked - Checked node IDs.
   */
  function findLoopSourceTag(
    tags: Record<string, Record<string, string | null>>,
    incomers: Record<string, { source: string; sourceHandle?: string | null }[]>,
    nodeId: string,
    handleId: string | null | undefined,
    closeId: string,
    checked: Set<string>
  ): string | null {
    let tag = findLoopTag(tags, nodeId, handleId);
    if (tag) {
      return tag;
    }
    const sources = incomers[nodeId];
    if (!sources) {
      return tag;
    }
    checked.add(nodeId);
    for (const source of sources) {
      if (closeId === source.source) {
        // use an empty string to indicate the loop is closed
        return '';
      }
      if (!checked.has(source.source)) {
        tag = findLoopSourceTag(tags, incomers, source.source, source.sourceHandle, closeId, checked);
        if (tag || tag === '') {
          return tag;
        }
      }
    }
    return tag;
  }

  /**
   * Find the first loop target tag.
   *
   * @param tags - Tags of the node handles.
   * @param outgoers - Node-targets map.
   * @param nodeId - Target node ID.
   * @param handleId - Target handle ID.
   * @param closeId - Close node ID.
   * @param checked - Checked node IDs.
   */
  function findLoopTargetTag(
    tags: Record<string, Record<string, string | null>>,
    outgoers: Record<string, { target: string; targetHandle?: string | null }[]>,
    nodeId: string,
    handleId: string | null | undefined,
    closeId: string,
    checked: Set<string>
  ): string | null {
    let tag = findLoopTag(tags, nodeId, handleId);
    if (tag) {
      return tag;
    }
    const targets = outgoers[nodeId];
    if (!targets) {
      return tag;
    }
    checked.add(nodeId);
    for (const target of targets) {
      if (closeId === target.target) {
        // use an empty string to indicate the loop is closed
        return '';
      }
      if (!checked.has(target.target)) {
        tag = findLoopTargetTag(tags, outgoers, target.target, target.targetHandle, closeId, checked);
        if (tag || tag === '') {
          return tag;
        }
      }
    }
    return tag;
  }

  /**
   * Find the tag of the node handle.
   *
   * @param tags - Tags of the node handles.
   * @param nodeId - Node ID.
   * @param handleId - Handle ID.
   */
  function findTag(
    tags: Record<string, Record<string, string | null>>,
    nodeId: string,
    handleId: string | null | undefined
  ): string | null {
    const tag = handleId ? tags[nodeId][handleId] : null;
    return tag === LOOP_TAG ? null : tag;
  }

  /**
   * Find the loop tag of the node handle.
   *
   * @param tags - Tags of the node handles.
   * @param nodeId - Node ID.
   * @param handleId - Handle ID.
   */
  function findLoopTag(
    tags: Record<string, Record<string, string | null>>,
    nodeId: string,
    handleId: string | null | undefined
  ): string | null {
    const tag = handleId ? tags[nodeId][handleId] : null;
    return tag === LOOP_TAG ? nodeId : tag;
  }

  // add the validators to the context
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  const validators = new Set<() => boolean>();
  setContext('flow/graph', {
    validators: validators,
    /**
     * Register a graph validator for the lifetime of its component.
     *
     * @param validator - Validation callback returning whether the graph is valid.
     */
    addValidator: (validator: () => boolean) => {
      onMount(() => {
        validators.add(validator);
        return () => validators.delete(validator);
      });
    }
  });

  /**
   * Hide all visible tooltips on the document.
   */
  const hideAllTips = () => hideAll();

  onMount(() => {
    let disposed = false;
    // get the flow graph draft
    loading.start();
    nodes = [];
    edges = [];
    waitForGraphWrites(Number(graphId))
      .then(() => {
        if (!disposed) {
          return api.get(`flow/graph/${graphId}`).json<Resp<FlowGraph>>();
        }
      })
      .then((response) => {
        if (disposed || !response) {
          return;
        }
        graph = response.data;
        if (graph.draft) {
          nodes = graph.draft.nodes;
          edges = graph.draft.edges;
        }
        jsonGraph = JSON.stringify({ nodes, edges });
      })
      .catch(() => {})
      .finally(() => {
        loading.end();
      });
    // hide all visible tooltips when the pane is touched
    const pane = document.getElementsByClassName('svelte-flow__pane')[0];
    pane.addEventListener('touchstart', hideAllTips);
    return () => {
      disposed = true;
      pane.removeEventListener('touchstart', hideAllTips);
    };
  });
</script>

{#snippet sideButton()}
  {@const btnClass = 'svelte-flow__panel flex-center size-8 border bg-blur-70 shadow-md **:opacity-80'}
  {#if readonly && graph}
    <div class="rounded-field {btnClass}" use:tooltip={{ content: $_('flow.readonly'), followCursor: true }}>
      <iconify-icon icon={icons.eye} width="1.5rem"></iconify-icon>
    </div>
  {:else if !$sidebar && graph}
    <button
      aria-label={$_('action.open', $_('flow.sidebar'))}
      class="ml-0! cursor-pointer rounded-r-field border-l-0 {btnClass}"
      onclick={() => ($sidebar = true)}
      use:tooltip={{ content: $_('action.open', $_('flow.sidebar')), followCursor: true }}
      in:fly={{ x: -50 }}
    >
      <iconify-icon icon={icons.panelLeftText} width="1.5rem"></iconify-icon>
    </button>
  {/if}
{/snippet}

<div class="history-back navbar-shadow drawer bg-base-200 {$sidebar ? 'drawer-open' : ''}">
  <Overlay loading={$loading} />
  <input id="drawer" type="checkbox" class="drawer-toggle" bind:checked={$sidebar} />
  <!--
  By default, the Svelte Flow container has a height of 100%.
  This means that the parent container needs a height to render the flow.
  -->
  <div class="drawer-content h-(--ks-svh) sm:h-(--ks-lvh)">
    <!-- eslint-disable @typescript-eslint/no-explicit-any -->
    <SvelteFlow
      bind:nodes
      bind:edges
      {ondrop}
      {ondragover}
      {isValidConnection}
      fitView
      minZoom={0.2}
      maxZoom={2}
      snapGrid={[10, 10]}
      nodeTypes={{ node: NodeWrapper as any }}
      edgeTypes={{ edge: EdgeWrapper as any }}
      defaultEdgeOptions={{
        type: 'edge',
        animated: true,
        markerEnd: {
          type: MarkerType.ArrowClosed
        }
      }}
      onnodedragstart={hideAllTips}
      class="bg-transparent!"
    >
      {@render sideButton()}
      <ControlBar bind:nodes bind:edges {graph} {jsonGraph} {readonly} {interactive} />
      <Background gap={10} variant={BackgroundVariant.Dots} patternColor="rgba(128,128,128,0.4)" />
    </SvelteFlow>
  </div>
  <NodeSchemas bind:nodes {graph} {sidebar} {interactive} />
</div>

<style>
  :global {
    .svelte-flow {
      --xy-edge-stroke-width-default: 2 !important;
      --xy-connectionline-stroke-width-default: 2 !important;

      .svelte-flow__attribution {
        display: none;
      }

      .svelte-flow__handle-top {
        top: -1px;
      }
      .svelte-flow__handle-left {
        left: -1px;
      }
      .svelte-flow__handle-right {
        right: -1px;
      }
      .svelte-flow__handle-bottom {
        bottom: -1px;
      }
    }
  }
</style>
