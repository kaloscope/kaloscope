<script lang="ts">
  import { _ } from '$lib/i18n';
  import { assistantCanvas } from '$lib/assistant/canvas';
  import { icons } from '$lib/icons';
  import type { Field, NodeSchema } from '$lib/types';
  import { useEdges, useNodes, useStore, type NodeProps } from '@xyflow/svelte';
  import { v7 as uuidv7 } from 'uuid';
  import NodeHandle from './NodeHandle.svelte';
  import * as _fields from './fields';

  // field components
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fields: Record<string, any> = _fields;

  // get node props
  let { id, data }: NodeProps = $props();
  const schema: NodeSchema = $derived(data.$schema as NodeSchema);

  // get all nodes and edges
  const nodes = useNodes();
  const edges = useEdges();

  // check if the node is selected
  const selected = $derived(nodes.current.find((node) => node.id === id)?.selected ?? false);

  // check if the node is interactive
  const store = useStore();
  let interactive = $derived(store.nodesDraggable || store.nodesConnectable || store.elementsSelectable);
  const canvasLocked = $derived($assistantCanvas !== null);

  // dynamic class names
  const shadowClass = $derived(selected ? 'shadow-xl' : 'shadow-lg hover:shadow-xl');
  const borderClass = $derived(
    `border-2 ${selected ? 'border-base-content/30' : 'border-border hover:border-base-content/30'}`
  );
  const titleClass = $derived(
    `transition-colors ${selected ? 'bg-base-300' : 'bg-base-300/50 group-hover:bg-base-300'}`
  );
  const bodyClass = $derived(
    `transition-colors ${selected ? 'bg-base-100' : 'bg-base-100/70 group-hover:bg-base-100'}`
  );

  /**
   * Copy the node with a new ID and offset position.
   *
   * @param event - Click event.
   */
  function copyNode(event: MouseEvent) {
    event.stopPropagation();
    if (!interactive || canvasLocked) return;
    const node = nodes.current.find((node) => node.id === id);
    if (!node) {
      return;
    }
    nodes.current = [
      ...nodes.current,
      {
        ...node,
        id: uuidv7(),
        position: { x: node.position.x + 30, y: node.position.y + 30 },
        data: $state.snapshot(node.data),
        selected: false
      }
    ];
  }

  /**
   * Delete the node and all connected edges.
   *
   * @param event - Click event.
   */
  function deleteNode(event: MouseEvent) {
    event.stopPropagation();
    if (!interactive || canvasLocked) return;
    nodes.current = nodes.current.filter((node) => node.id !== id);
    edges.current = edges.current.filter((edge) => edge.source !== id && edge.target !== id);
  }

  /**
   * Get grid column span for a node field.
   *
   * @param field - Node field schema.
   */
  function fieldLayoutStyle(field: Field) {
    const span = field.span ?? 100;
    return `grid-column: span ${span} / span ${span};`;
  }

  /**
   * Show a field when its dependencies match the current values or schema defaults.
   *
   * @param field - Field schema with optional visibility conditions.
   * @returns Whether all visibility conditions match.
   */
  function fieldVisible(field: Field): boolean {
    return Object.entries(field.visible_when ?? {}).every(
      ([key, value]) => (data[key] ?? schema.fields.find((item) => item.id === key)?.default) === value
    );
  }
</script>

<div class="group relative w-max min-w-80 rounded-box backdrop-blur-lg transition-all {shadowClass} {borderClass}">
  <div class="absolute z-1 size-full bg-transparent {interactive || canvasLocked ? 'hidden' : ''}"></div>
  <div class="flex items-center gap-2 rounded-t-box p-2 {titleClass}">
    <iconify-icon icon={schema.icon in icons ? icons[schema.icon] : icons.box3d} width="1.5rem"></iconify-icon>
    <span class="mr-auto text-lg font-medium">
      {$_(`flow.node.name.${schema.node_type}`, { default: schema.name })}
    </span>
    <!-- copy button -->
    {#if schema.group !== 'start'}
      <button
        class="btn btn-square btn-subtle btn-sm"
        aria-label="Copy"
        disabled={canvasLocked}
        onclick={(event) => copyNode(event)}
      >
        <iconify-icon icon={icons.documentCopy} width="1.25rem"></iconify-icon>
      </button>
    {/if}
    <!-- delete button -->
    <button
      class="btn btn-square btn-subtle btn-sm"
      aria-label="Delete"
      disabled={canvasLocked}
      onclick={(event) => deleteNode(event)}
    >
      <iconify-icon icon={icons.delete} width="1.25rem"></iconify-icon>
    </button>
  </div>
  <div class="relative -mt-px rounded-b-box p-3 {bodyClass}">
    <div class="-mx-1 grid" style="grid-template-columns: repeat(100, minmax(0, 1fr));" style:width={schema.width}>
      {#each schema.fields as field (field.id)}
        {@const Field = fields[field.field_type]}
        {#if fieldVisible(field)}
          <div
            class="min-w-0 px-1 [&:not(:first-child):has(>.collapse)]:mt-3"
            style={fieldLayoutStyle(field)}
            inert={canvasLocked && field.field_type !== 'code' && field.field_type !== 'script'}
          >
            {#if field.field_type === 'code' || field.field_type === 'script'}
              {#key canvasLocked}
                <Field
                  nodeId={id}
                  {...field}
                  readonly={canvasLocked || field.readonly}
                  data={data[field.id] ?? field.default}
                />
              {/key}
            {:else}
              <Field nodeId={id} {...field} data={data[field.id] ?? field.default} />
            {/if}
          </div>
        {/if}
      {/each}
    </div>
    {#each schema.handles as handle (handle.id)}
      <NodeHandle nodeId={id} {...handle} />
    {/each}
  </div>
</div>
