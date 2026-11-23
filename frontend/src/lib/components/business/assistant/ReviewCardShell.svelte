<script lang="ts">
  import { _ } from '$lib/i18n';
  import type { Snippet } from 'svelte';
  import type { ClassValue } from 'svelte/elements';

  const {
    busy,
    failed,
    loaded,
    showSpinner = true,
    hasActions = true,
    labelledBy,
    sectionClass = 'space-y-2 rounded-box border border-base-content/15 p-3 text-sm',
    issues = [],
    status = '',
    statusClass = 'text-xs text-base-content/75',
    alertClass = 'text-xs text-error',
    header,
    children,
    extraStatus,
    actions
  }: {
    busy: boolean;
    failed: boolean;
    loaded: boolean;
    showSpinner?: boolean;
    hasActions?: boolean;
    labelledBy?: string;
    sectionClass?: ClassValue;
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

<section class={sectionClass} aria-busy={busy} aria-labelledby={labelledBy}>
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
    <span class="loading loading-xs loading-spinner" role="progressbar" aria-label={$_('media.loading')}></span>
  {/if}
  {#if failed}<p class={alertClass} role="alert">{$_('assistant.request_failed')}</p>{/if}
  {#if hasActions && actions}
    <div class="flex flex-wrap justify-end gap-2">
      {@render actions()}
    </div>
  {/if}
</section>
