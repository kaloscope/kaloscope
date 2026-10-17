<script lang="ts">
  import { icons } from '$lib/icons';
  import { providerIcons } from '$lib/icons/providers';
  import type { AIProviderPresetID } from '$lib/types';

  let { preset, class: className = 'size-6' }: { preset: AIProviderPresetID | null; class?: string } = $props();
  const Icon = $derived(preset ? providerIcons[preset] : null);
</script>

{#if Icon}
  <Icon class={className} />
{:else}
  <iconify-icon icon={icons.cloudCube} class={className} width="100%" height="100%" aria-hidden="true"></iconify-icon>
{/if}
