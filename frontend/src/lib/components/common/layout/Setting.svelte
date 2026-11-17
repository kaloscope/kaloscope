<script lang="ts" module>
  import type { Snippet } from 'svelte';

  export type SettingProps = {
    /** The setting content snippet. */
    children: Snippet;
    /** The setting title text. */
    title: string;
    /** The tooltip content. */
    tip?: string;
  };
</script>

<script lang="ts">
  import { tooltip } from '$lib/actions';
  import { icons } from '$lib/icons';
  import { sniffer } from '$lib/utils/browser';

  let { children, title, tip }: SettingProps = $props();
</script>

<div class="w-full">
  <div class="flex items-center justify-between text-xl">
    <span class="font-bold">{title}</span>
    {#if tip}
      <span
        class="flex-center cursor-help"
        use:tooltip={{ content: tip, placement: 'left', trigger: sniffer.isDesktop() ? 'mouseenter' : 'click' }}
      >
        <iconify-icon icon={icons.questionCircle}></iconify-icon>
      </span>
    {/if}
  </div>
  <div class="divider"></div>
  <div class="flex flex-col gap-4 p-1 [&_form]:flex [&_form]:flex-col [&_form]:gap-4">
    {@render children()}
  </div>
</div>
