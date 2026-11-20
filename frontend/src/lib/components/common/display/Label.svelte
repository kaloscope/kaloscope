<script lang="ts" module>
  import type { Snippet } from 'svelte';
  import type { Placement } from 'tippy.js';

  export type LabelProps = {
    /** The label text snippet. */
    children: Snippet;
    /** The tooltip content. */
    tip?: string;
    /** The placement of the tooltip. */
    tipPlacement?: Placement;
    /** Whether to show the required mark. */
    required?: boolean;
    /** Whether to prevent the default click event. */
    preventDefault?: boolean;
    /** Whether to use a bold font weight. */
    bold?: boolean;
    /** The class names for the label. */
    class?: string;
  };
</script>

<script lang="ts">
  import { tooltip } from '$lib/actions';
  import { icons } from '$lib/icons';
  import { sniffer } from '$lib/utils/browser';

  let {
    children,
    tip,
    tipPlacement = 'left',
    required = false,
    preventDefault = true,
    bold = false,
    class: _class
  }: LabelProps = $props();
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="mt-2 flex min-h-6.5 items-center justify-between gap-1 px-1 {_class}"
  onclick={(event) => preventDefault && event.preventDefault()}
>
  <span class="text-sm {bold ? 'font-semibold' : 'font-medium'}">
    {#if required}
      <span class="align-middle text-error">* </span>
    {/if}
    {@render children()}
  </span>
  {#if tip}
    <span
      class="flex-center cursor-help text-lg"
      use:tooltip={{ content: tip, placement: tipPlacement, trigger: sniffer.isDesktop() ? 'mouseenter' : 'click' }}
    >
      <iconify-icon icon={icons.questionCircle} class="text-muted"></iconify-icon>
    </span>
  {/if}
</div>
