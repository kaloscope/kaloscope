<script lang="ts">
  import { Label } from '$lib/components';
  import { nodeFormatter } from '$lib/i18n';
  import type { Field, FlowGraphContext } from '$lib/types';
  import { useSvelteFlow } from '@xyflow/svelte';
  import { getContext, hasContext } from 'svelte';

  let {
    data = '',
    ...field
  }: Field & {
    nodeId: string;
    data?: string;
    placeholder: string | null;
    minlength: number;
    maxlength: number;
  } = $props();

  let textInput: HTMLInputElement;

  const { updateNodeData } = useSvelteFlow();
  const { label, tooltip, placeholder } = nodeFormatter;

  /**
   * Check and report the validity of the text input.
   */
  function reportValidity() {
    return textInput && textInput.reportValidity();
  }

  // register the validator
  if (hasContext('flow/graph')) {
    const context = getContext('flow/graph') as FlowGraphContext;
    context.addValidator(reportValidity);
  }
</script>

<fieldset class="fieldset">
  <Label required={field.required} tip={$tooltip(field.tooltip)}>
    {$label(field.label)}
  </Label>
  <input
    type="text"
    class="nodrag input w-full truncate input-sm"
    required={field.required}
    minlength={field.minlength}
    maxlength={field.maxlength}
    placeholder={$placeholder(field.placeholder)}
    bind:value={data}
    bind:this={textInput}
    oninput={() => {
      reportValidity();
      updateNodeData(field.nodeId, { [field.id]: data });
    }}
  />
</fieldset>
