<script lang="ts">
  import { Label } from '$lib/components';
  import { nodeFormatter } from '$lib/i18n';
  import type { Field } from '$lib/types';
  import { useSvelteFlow } from '@xyflow/svelte';

  let {
    data = 0,
    ...field
  }: Field & {
    nodeId: string;
    data?: number;
    min: number | null;
    max: number | null;
    step: number;
    precision: number;
  } = $props();

  const { updateNodeData } = useSvelteFlow();
  const { label, tooltip } = nodeFormatter;

  /**
   * Handle the input event on the number input field.
   */
  function updateFieldData() {
    // clamp the value to the min and max values
    if (field.min !== null && data < field.min) {
      data = field.min;
    } else if (field.max !== null && data > field.max) {
      data = field.max;
    }
    // limit the number of decimal places
    const factor = Math.pow(10, field.precision);
    if (data > 0) {
      data = Math.floor(data * factor) / factor;
    } else {
      data = Math.ceil(data * factor) / factor;
    }
    // update the node data
    updateNodeData(field.nodeId, { [field.id]: data });
  }
</script>

<fieldset class="fieldset">
  <Label required={field.required} tip={$tooltip(field.tooltip)}>
    {$label(field.label)}
  </Label>
  <input
    type="number"
    class="nodrag input w-full input-sm"
    required={field.required}
    min={field.min}
    max={field.max}
    step={field.step}
    bind:value={data}
    oninput={updateFieldData}
  />
</fieldset>
