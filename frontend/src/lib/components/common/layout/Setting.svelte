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

<section class="w-full">
  <div class="mb-3 flex items-center justify-between gap-2">
    <h2 class="section-title"><span class="truncate">{title}</span></h2>
    {#if tip}
      <span
        class="flex-center cursor-help text-xl opacity-60 transition-opacity hover:opacity-100"
        use:tooltip={{ content: tip, placement: 'left', trigger: sniffer.isDesktop() ? 'mouseenter' : 'click' }}
      >
        <iconify-icon icon={icons.questionCircle}></iconify-icon>
      </span>
    {/if}
  </div>
  <div class="flex flex-col gap-4 card-surface p-4 sm:p-5 [&_form]:flex [&_form]:flex-col [&_form]:gap-4">
    {@render children()}
  </div>
</section>
