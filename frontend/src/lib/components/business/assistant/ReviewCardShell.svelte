<script lang="ts">
  import { _ } from '$lib/i18n';
  import type { Snippet } from 'svelte';
  import type { ClassValue } from 'svelte/elements';

  const {
    busy,
    failed,
    loaded,
    showSpinner = false,
    sectionClass = 'space-y-2 rounded-box border border-base-content/15 p-3 text-sm',
    ariaLabel,
    issues = [],
    status = '',
    statusClass,
    alertClass,
    header,
    children,
    extraStatus,
    actions
  }: {
    busy: boolean;
    failed: boolean;
    loaded: boolean;
    showSpinner?: boolean;
    sectionClass?: ClassValue;
    ariaLabel?: string;
    issues?: { path?: string; message: string }[];
    status?: string;
    statusClass?: ClassValue;
    alertClass?: ClassValue;
    header?: Snippet;
    children?: Snippet;
    extraStatus?: Snippet;
    actions?: Snippet;
  } = $props();
</script>

<section class={sectionClass} aria-busy={busy} aria-label={ariaLabel}>
  {@render header?.()}
  {#if loaded}
    {@render children?.()}
    {#if issues.length}
      <ul class="list-disc pl-4 text-xs">
        {#each issues as issue, index (index)}<li>{issue.path ? issue.path + ' · ' : ''}{issue.message}</li>{/each}
      </ul>
    {/if}
    {#if status}
      <p role="status" class={statusClass}>{status}</p>
    {/if}
    {@render extraStatus?.()}
  {:else if showSpinner && busy}
    <span class="loading loading-xs loading-spinner" role="status" aria-label={$_('media.loading')}></span>
  {/if}
  {#if failed}<p class={alertClass} role="alert">{$_('assistant.request_failed')}</p>{/if}
  <div class="flex flex-wrap justify-end gap-2">
    {@render actions?.()}
  </div>
</section>
