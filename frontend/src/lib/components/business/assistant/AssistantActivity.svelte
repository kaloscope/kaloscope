<script lang="ts">
  import type { AssistantTool } from '$lib/assistant/session';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { Snippet } from 'svelte';

  const { tools, entry }: { tools: AssistantTool[]; entry: Snippet<[AssistantTool, number?]> } = $props();

  const groups = $derived.by(() => {
    const runs: AssistantTool[][] = [];
    for (const tool of tools) {
      const last = runs.at(-1);
      if (last?.[0].name === tool.name) last.push(tool);
      else runs.push([tool]);
    }
    return runs;
  });
</script>

{#snippet calls(items: AssistantTool[])}
  {#each items as tool, index (tool.id)}
    <li>{@render entry(tool, items.length > 1 ? index + 1 : undefined)}</li>
  {/each}
{/snippet}

{#snippet heading(name: string | undefined, count: number)}
  <summary
    class="flex min-h-7 cursor-pointer list-none items-center gap-2 rounded-field px-2 text-xs text-base-content/70 hover:text-base-content focus-visible:outline-2 focus-visible:outline-primary [&::-webkit-details-marker]:hidden"
  >
    <iconify-icon
      icon={icons.chevronRight}
      width="0.875rem"
      class="chevron size-4 shrink-0 motion-safe:transition-transform"
      aria-hidden="true"
    ></iconify-icon>
    <span class="min-w-0 truncate font-medium"
      >{name ? $_(`assistant.tools.${name}`, { default: name }) : $_('assistant.activity')}</span
    >
    <span class="shrink-0 tabular-nums">{$_('assistant.activity_count', count)}</span>
  </summary>
{/snippet}

<div class="-mx-2">
  {#if tools.length === 1}
    {@render entry(tools[0])}
  {:else}
    <details>
      {@render heading(groups.length === 1 ? tools[0].name : undefined, tools.length)}
      <ol class="ml-4 border-s border-border pl-2">
        {#each groups as group (group[0].id)}
          {#if groups.length > 1 && group.length > 1}
            <li>
              <details>
                {@render heading(group[0].name, group.length)}
                <ol class="ml-4 border-s border-border pl-2">{@render calls(group)}</ol>
              </details>
            </li>
          {:else}
            {@render calls(group)}
          {/if}
        {/each}
      </ol>
    </details>
  {/if}
</div>

<style>
  details[open] > summary > .chevron {
    rotate: 90deg;
  }
</style>
