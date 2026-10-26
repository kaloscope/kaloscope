<script lang="ts">
  import type { AssistantTool } from '$lib/assistant/session';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';

  const { tool, unsaved = false, onplan }: { tool: AssistantTool; unsaved?: boolean; onplan?: () => void } = $props();
  const statusIcon = $derived.by(() => {
    if (tool.status === 'complete') return icons.checkmark;
    if (tool.status === 'error') return icons.warning;
    return icons.subtractCircle;
  });
  const detail = $derived(tool.result?.data ? JSON.stringify(tool.result.data, null, 2) : '');
  const error = $derived(
    tool.result?.error
      ? $_(`assistant.errors.${tool.result.error.code}`, {
          default: $_(`alert.${tool.result.error.code}`, { default: $_('assistant.tool_failed') })
        })
      : ''
  );
</script>

{#snippet row()}
  <span class="flex size-4 shrink-0 items-center justify-center" aria-hidden="true">
    {#if tool.status === 'running'}
      <span class="loading loading-xs loading-spinner"></span>
    {:else}
      <iconify-icon icon={statusIcon} width="0.875rem" class="size-3.5"></iconify-icon>
    {/if}
  </span>
  <span class="min-w-0 truncate font-medium">
    {$_(`assistant.tools.${tool.name}`, { default: tool.name })}
  </span>
  <span class="shrink-0" class:sr-only={tool.status === 'complete' || tool.status === 'running'}>
    {$_(`assistant.tool_status.${tool.status}`)}
  </span>
{/snippet}

{#if detail || error}
  <details class="group">
    <summary
      class="flex min-h-7 cursor-pointer list-none items-center gap-2 rounded-field px-2 text-xs text-base-content/60 transition-colors hover:text-base-content focus-visible:text-base-content focus-visible:outline-2 focus-visible:outline-primary [&::-webkit-details-marker]:hidden"
    >
      {@render row()}
      <iconify-icon
        icon={icons.chevronRight}
        width="0.75rem"
        class="ml-auto size-3 shrink-0 transition-transform group-open:rotate-90"
        aria-hidden="true"
      ></iconify-icon>
    </summary>
    <div class="mt-1 mb-2 ml-8 space-y-2 text-xs leading-5">
      {#if error}
        <p class="text-base-content/60">{error}</p>
      {/if}
      {#if detail}
        <pre
          class="max-h-64 overflow-auto rounded-field bg-base-content/5 px-2.5 py-2 font-mono text-[0.6875rem] leading-4 break-all whitespace-pre-wrap">{detail}</pre>
      {/if}
    </div>
  </details>
{:else}
  <div class="flex min-h-7 items-center gap-2 px-2 text-xs text-base-content/60">{@render row()}</div>
{/if}
{#if tool.name === 'preview_workflow_draft' && tool.result?.ok && tool.result.data?.proposal_id}
  <p class="ml-8 text-xs leading-5 text-base-content/60">
    {#if tool.proposal?.status === 'saved'}
      {$_('assistant.canvas.saved')}
    {:else if tool.proposal?.status === 'unchanged'}
      {$_('assistant.canvas.unchanged')}
    {:else if tool.result.data?.canvas_change_count === 0}
      <!-- compared with the captured canvas, so no change says nothing about earlier unsaved edits -->
      {$_(unsaved ? 'assistant.canvas.no_change_unsaved' : 'assistant.canvas.no_change')}
    {:else if tool.proposal?.status === 'cancelled'}
      {$_('assistant.canvas.reverted')}
    {:else}
      {$_('assistant.canvas.record')}
    {/if}
  </p>
{/if}
{#if tool.name === 'create_workflow_draft' && tool.result?.ok && tool.result.data}
  {@const { name, workflow_id, replayed } = tool.result.data}
  <p class="ml-8 text-xs leading-5 text-base-content/60">
    {$_(replayed ? 'assistant.create.replayed' : 'assistant.create.created', [String(name), Number(workflow_id)])}
  </p>
{/if}
{#if onplan && tool.name === 'validate_download_plan' && tool.result?.ok && tool.result.data?.valid}
  <button type="button" class="btn ml-8 btn-outline btn-sm" onclick={onplan}>
    {$_('assistant.plan.review')}
  </button>
{/if}
{#if tool.name === 'create_download_plan' && tool.result?.ok && tool.result.data?.created}
  <p class="ml-8 text-xs leading-5 text-base-content/60">
    {$_('assistant.plan.created', Number(tool.result.data.plan_id))}
  </p>
{/if}
{#if tool.name === 'create_download_plan' && tool.result?.ok && tool.result.data?.confirmation_required}
  <p class="ml-8 text-xs leading-5 text-base-content/60">{$_('assistant.plan.confirmation_required')}</p>
{/if}
{#if tool.name === 'search_indexer' && tool.result?.ok && tool.result.data}
  {@const result = tool.result.data}
  <p class="ml-8 text-xs leading-5 text-base-content/60" role="status">
    {#if result.status === 'ok'}
      {$_(result.received_count === 0 ? 'assistant.plan.search_empty' : 'assistant.plan.search_preview', [
        Number(result.returned_count)
      ])}
      {#if result.truncated}<span class="block">{$_('assistant.plan.search_truncated')}</span>{/if}
    {:else}
      {$_(`assistant.plan.search_${String(result.status)}`, { default: $_('assistant.plan.search_failed') })}
    {/if}
  </p>
{/if}
