<script lang="ts">
  import ReviewCardShell from './ReviewCardShell.svelte';
  import { createReviewCard } from '$lib/assistant/review-card.svelte';
  import { formatReviewValue, planChangeFieldLabels, refreshDomainTarget } from '$lib/assistant/reviews';
  import { assistant } from '$lib/assistant/session';
  import { _, dateTime } from '$lib/i18n';
  import type { PlanReview } from '$lib/types';

  const { id }: { id: string } = $props();
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
        refreshDomainTarget({ kind: 'download_plan_changed', source: card.source });
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
    return formatReviewValue(value, { t: $_, formatDate: $dateTime, key: field, intervalAsHours: true });
  }
</script>

<ReviewCardShell
  {busy}
  {failed}
  loaded={!!receipt}
  issues={receipt?.issues ?? []}
  status={receipt ? $_(`assistant.plan_change.${receipt.status}`, receipt.plan_id) : ''}
>
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
              {$_(planChangeFieldLabels[change.field] ?? `field.${change.field}`, { default: change.field })}
            </dt>
            <dd class="break-words">{display(change.field, change.before)} → {display(change.field, change.after)}</dd>
          </div>
        {/each}
      </dl>
    {/if}
  {/if}
  {#snippet actions()}
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
  {/snippet}
</ReviewCardShell>
