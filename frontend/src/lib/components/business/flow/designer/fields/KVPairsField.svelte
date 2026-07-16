<script lang="ts">
  import { Label } from '$lib/components';
  import { nodeFormatter } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { Field } from '$lib/types';
  import { useSvelteFlow } from '@xyflow/svelte';

  let {
    data = [],
    ...field
  }: Field & {
    nodeId: string;
    data?: { key: string; value: string }[];
    placeholder: (string | null)[] | null;
  } = $props();

  const { updateNodeData } = useSvelteFlow();
  const { label, tooltip, placeholder } = nodeFormatter;
  const keyPlaceholder = $derived($placeholder(field.placeholder?.[0]));
  const valuePlaceholder = $derived($placeholder(field.placeholder?.[1]));
</script>

<fieldset class="fieldset">
  <Label required={field.required} tip={$tooltip(field.tooltip)}>
    {$label(field.label)}
  </Label>
  <div class="flex flex-col gap-2">
    <div class="flex items-center gap-2">
      <input type="text" class="input w-1/2 input-sm" placeholder={keyPlaceholder} disabled />
      <span>:</span>
      <input type="text" class="input w-1/2 input-sm" placeholder={valuePlaceholder} disabled />
      <button
        aria-label="Add"
        class="btn ml-auto btn-square btn-subtle btn-sm"
        onclick={() => {
          data = [...data, { key: '', value: '' }];
          updateNodeData(field.nodeId, { [field.id]: data });
        }}
      >
        <iconify-icon icon={icons.addCircle} width="1rem"></iconify-icon>
      </button>
    </div>
    {#each data as entry, index (index)}
      <!-- eslint-disable svelte/no-unused-svelte-ignore -->
      <div class="flex items-center gap-2">
        <!-- svelte-ignore binding_property_non_reactive -->
        <input
          type="text"
          class="nodrag input w-1/2 truncate input-sm"
          placeholder={keyPlaceholder}
          bind:value={entry.key}
          oninput={() => updateNodeData(field.nodeId, { [field.id]: data })}
        />
        <span>:</span>
        <!-- svelte-ignore binding_property_non_reactive -->
        <input
          type="text"
          class="nodrag input w-1/2 truncate input-sm"
          placeholder={valuePlaceholder}
          bind:value={entry.value}
          oninput={() => updateNodeData(field.nodeId, { [field.id]: data })}
        />
        <button
          aria-label="Delete"
          class="btn ml-auto btn-square btn-subtle btn-sm"
          onclick={() => {
            data = data.filter((_, i) => i !== index);
            updateNodeData(field.nodeId, { [field.id]: data });
          }}
        >
          <iconify-icon icon={icons.subtractCircle} width="1rem"></iconify-icon>
        </button>
      </div>
    {/each}
  </div>
</fieldset>
