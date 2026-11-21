<script lang="ts">
  import { createReviewCard } from '$lib/assistant/review-card.svelte';
  import { assistant } from '$lib/assistant/session';
  import { _, dateTime } from '$lib/i18n';
  import type { PlanReview } from '$lib/types';

  const { id }: { id: string } = $props();
  const labels: Record<string, string> = {
    interval_num: 'field.interval',
    dir: 'assistant.plan_change.download_directory',
    filters: 'entity.filters',
    transfer_lib_id: 'assistant.plan_change.transfer_library',
    transfer_method: 'assistant.plan_change.method',
    sub_pattern: 'assistant.plan_change.pattern',
    sub_repl: 'assistant.plan_change.replacement',
    downloader_id: 'download.downloader.title'
  };
  const card = createReviewCard<PlanReview>({
    path: () => `assistant/plan-reviews/${id}`,
    decision: (receipt, approve) => ({ request_id: receipt.request_id, approve }),
    events: ['assistant-review-changed', 'download-plan-changed'],
    received(data, previous, approve) {
      if (approve !== undefined) card.notify('assistant-review-changed');
      if (
        (data.status === 'updated' || data.status === 'deleted') &&
        (approve !== undefined || (previous && previous.status !== data.status))
      ) {
        card.notify('download-plan-changed');
      }
    }
  });
  const receipt = $derived(card.receipt);
  const busy = $derived(card.busy);
  const failed = $derived(card.failed);
  const load = card.load;

  /**
   * Format the exact field value from a frozen review.
   *
   * @param field - Name of the changed setting.
   * @param value - Stored before or after value.
   * @returns A localized description or the saved filter expression.
   */
  function display(field: string, value: unknown): string {
    if (value == null || value === '') return $_('assistant.plan_change.cleared');
    if (field === 'interval_num') return $_('assistant.plan_change.hours', String(value));
    if (field === 'interval_start' || field === 'interval_end') return $dateTime(String(value));
    if (field === 'transfer_method') return $_('enum.transfer_method.' + value, { default: String(value) });
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
  }
</script>

<section class="space-y-2 rounded-box border border-base-content/15 p-3 text-sm" aria-busy={busy}>
  {#if receipt}
    <h4 class="font-semibold">{$_(`assistant.plan_change.${receipt.action}_title`)}</h4>
    <p class="break-words">#{receipt.plan_id} · {receipt.workflow_name} · {receipt.keyword}</p>
    {#if receipt.action === 'delete'}
      <p class="text-xs text-base-content/75">{$_('assistant.plan_change.delete_effect')}</p>
    {:else}
      <dl class="space-y-1 text-xs">
        {#each receipt.changes as change (change.field)}
          <div>
            <dt class="font-semibold">
              {$_(labels[change.field] ?? `field.${change.field}`, { default: change.field })}
            </dt>
            <dd class="break-words">{display(change.field, change.before)} → {display(change.field, change.after)}</dd>
          </div>
        {/each}
      </dl>
    {/if}
    {#if receipt.issues?.length}<ul class="list-disc pl-4 text-xs">
        {#each receipt.issues as issue, index (index)}<li>
            {issue.path ? issue.path + ' · ' : ''}{issue.message}
          </li>{/each}
      </ul>{/if}
    <p role="status">{$_(`assistant.plan_change.${receipt.status}`, receipt.plan_id)}</p>
  {/if}
  {#if failed}<p role="alert">{$_('assistant.request_failed')}</p>{/if}
  <div class="flex flex-wrap justify-end gap-2">
    {#if failed || receipt?.status === 'pending' || receipt?.status === 'uncertain'}
      <button type="button" class="btn btn-ghost btn-sm" disabled={busy || $assistant.busy} onclick={() => load()}>
        {$_('action.reload')}
      </button>
    {:else if receipt?.status === 'reviewed' || (receipt?.status === 'failed' && receipt.retryable !== false)}
      <button type="button" class="btn btn-ghost btn-sm" disabled={busy || $assistant.busy} onclick={() => load(false)}>
        {$_('message.cancel')}
      </button>
      <button
        type="button"
        class="btn btn-sm"
        class:btn-error={receipt.action === 'delete'}
        class:btn-primary={receipt.action === 'update'}
        disabled={busy || $assistant.busy}
        onclick={() => load(true)}
      >
        {$_(`assistant.plan_change.confirm_${receipt.action}`)}
      </button>
    {/if}
  </div>
</section>
