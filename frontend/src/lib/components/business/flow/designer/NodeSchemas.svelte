<script lang="ts">
  import { activate } from '$lib/actions';
  import { api } from '$lib/api';
  import { Button, Overlay } from '$lib/components';
  import { GraphCategory } from '$lib/enums';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { FlowGraph, NodeSchema, Resp } from '$lib/types';
  import { useSvelteFlow, type Node } from '@xyflow/svelte';
  import { v7 as uuidv7 } from 'uuid';

  let {
    nodes = $bindable(),
    graph,
    sidebar = $bindable(),
    interactive
  }: {
    nodes: Node[];
    graph: FlowGraph | null;
    sidebar: boolean;
    interactive: boolean;
  } = $props();

  let schemas: Record<string, NodeSchema[]> = $state({});
  const loading = createLoading();
  const { fitView } = useSvelteFlow();

  /**
   * Check if the node is disabled.
   *
   * @param group - Group name.
   * @param schema - Node schema.
   */
  function isDisabled(group: string, schema: NodeSchema) {
    if (graph === null) {
      return true;
    }
    if (group === 'start') {
      // only one start node
      return nodes.some((node) => (node.data.$schema as NodeSchema).node_type === schema.node_type);
    }
    return false;
  }

  /**
   * Add a node to the graph.
   *
   * @param schema - Node schema.
   */
  function addNode(schema: NodeSchema) {
    if (!interactive) {
      return;
    }
    // find the first non-occupied coordinate
    const coordinate = nextCoordinate(
      Array.from(
        new Set(
          nodes
            .filter((node) => {
              const x = node.position.x;
              const y = node.position.y;
              return x >= 0 && x === y && x % 10 === 0;
            })
            .map((node) => node.position.x)
        )
      ).sort((a, b) => a - b)
    );
    // unselect all nodes
    nodes = nodes.map((node) => {
      node.selected = false;
      return { ...node };
    });
    // add the node
    nodes.push({
      id: uuidv7(),
      type: 'node',
      position: { x: coordinate, y: coordinate },
      data: { $schema: { ...schema } },
      origin: [0, 0],
      selected: true
    });
    nodes = [...nodes];
    // fit the view
    fitView({ duration: 500 });
  }

  /**
   * Find the first non-occupied coordinate.
   *
   * @param coordinates - List of coordinates.
   */
  function nextCoordinate(coordinates: number[]) {
    for (let i = 0; i < coordinates.length; i++) {
      const expected = i * 10;
      if (coordinates[i] !== expected) {
        return expected;
      }
    }
    return coordinates.length * 10;
  }

  /**
   * Handle the drag start event.
   *
   * @param event - Drag event.
   * @param schema - Node schema.
   */
  function ondragstart(event: DragEvent, schema: NodeSchema) {
    const transfer = event.dataTransfer;
    if (!transfer) {
      return;
    }
    transfer.effectAllowed = 'copyMove';
    transfer.setData('application/svelteflow', JSON.stringify(schema));
  }

  $effect(() => {
    // get the node schemas
    if (graph && graph.category) {
      loading.start();
      api
        .get('flow/node/schemas', { searchParams: { category: graph.category } })
        .json<Resp<Record<string, NodeSchema[]>>>()
        .then(({ data }) => (schemas = data))
        .finally(() => loading.end());
    }
  });
</script>

<div class="drawer-side top-(--ks-navbar-h) max-h-(--ks-svh) sm:max-h-(--ks-lvh)">
  <div class="h-full w-60 overflow-y-auto border-r bg-base-125">
    <Overlay loading={$loading} />
    {#if graph && Object.keys(schemas).length > 0}
      {@const category = GraphCategory[graph.category]}
      <div class="flex items-center justify-between p-4">
        <div class="flex-center gap-1">
          <iconify-icon icon={category.icon} style:color={category.iconColor} width="1.5rem" class="size-6">
          </iconify-icon>
          <span class="text-lg font-medium">{$_(category.label)}</span>
        </div>
        <Button
          class="bg-base-content/5 shadow-sm"
          icon={icons.goStart}
          text={$_('action.close', $_('flow.sidebar'))}
          onclick={() => (sidebar = false)}
        />
      </div>
      <div class="menu w-full gap-2 pb-10 select-none">
        {#each Object.keys(schemas) as group (group)}
          <li class="menu-title">{$_(`flow.node.group.${group}`)}</li>
          {#each schemas[group] as schema (schema.node_type)}
            {@const disabled = isDisabled(group, schema)}
            {@const btnClass = disabled ? 'opacity-50 pointer-events-none' : 'shadow-sm hover:shadow-lg'}
            <li>
              <!-- https://stackoverflow.com/questions/22922761/rounded-corners-with-html-draggable/54428199 -->
              <button
                class="mx-1 rotate-0 cursor-grab border active:cursor-grabbing {btnClass}"
                ondragstart={(event) => ondragstart(event, schema)}
                draggable={!disabled}
                {disabled}
              >
                <iconify-icon
                  icon={schema.icon in icons ? icons[schema.icon] : icons.box3d}
                  width="1.75rem"
                  class="size-7 opacity-80"
                ></iconify-icon>
                <span class="truncate">{$_(`flow.node.name.${schema.node_type}`, { default: schema.name })}</span>
                <!-- svelte-ignore a11y_click_events_have_key_events -->
                <div
                  tabindex={disabled ? -1 : 0}
                  role="button"
                  aria-disabled={disabled}
                  aria-label={$_('action.add')}
                  use:activate
                  class="btn btn-square btn-subtle btn-sm {disabled ? 'btn-disabled' : ''}"
                  onclick={() => addNode(schema)}
                >
                  <iconify-icon icon={icons.addCircle} width="1.25rem" class="opacity-70"></iconify-icon>
                </div>
              </button>
            </li>
          {/each}
        {/each}
      </div>
    {/if}
  </div>
</div>
