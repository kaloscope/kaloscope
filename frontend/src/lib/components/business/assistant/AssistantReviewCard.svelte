<script lang="ts">
  import FirstCheckResult from './FirstCheckResult.svelte';
  import { createReviewCard } from '$lib/assistant/review-card.svelte';
  import {
    reviewChanges,
    reviewFields,
    refreshReviewTarget,
    type AssistantReview,
    type ReviewTool
  } from '$lib/assistant/reviews';
  import { assistant } from '$lib/assistant/session';
  import Modal from '$lib/components/common/interaction/Modal.svelte';
  import { icons } from '$lib/icons';
  import { onDestroy } from 'svelte';
  import { _, dateTime } from '$lib/i18n';

  const {
    id,
    requestId,
    createdInChat = false,
    tool,
    onedit,
    onview
  }: {
    id: string;
    requestId: string;
    createdInChat?: boolean;
    tool: ReviewTool;
    onedit?: () => void;
    onview?: () => void;
  } = $props();
  let modal: Modal | undefined = $state();
  const card = createReviewCard<AssistantReview>({
    path: () => `assistant/reviews/${id}`,
    decision: (_receipt, approve) => ({ request_id: requestId, approve }),
    get events() {
      return tool === 'validate_download_plan'
        ? ['assistant-review-changed', 'download-plan-created']
        : ['assistant-review-changed'];
    },
    received(data, _previous, approve) {
      if (data.request_id !== requestId || data.tool !== tool || !data.preview) throw new Error('review_mismatch');
      if (approve !== undefined) {
        card.notify('assistant-review-changed');
        if (approve && ['applied', 'pending', 'partial', 'uncertain'].includes(data.status)) refreshReviewTarget(data);
      }
    }
  });
  const receipt = $derived(card.receipt);
  const busy = $derived(card.busy);
  const failed = $derived(card.failed);
  const load = card.load;
  const pending = $derived(
    receipt?.status === 'reviewed' || (receipt?.status === 'failed' && receipt.retryable === true)
  );
  const action = $derived.by(() => {
    if (tool === 'review_workflow_schedule') return 'schedule_' + String(receipt?.preview.action);
    if (tool === 'review_download_task_action') return 'task_' + String(receipt?.preview.action);
    return tool;
  });
  const fields = $derived(receipt ? reviewFields(receipt) : []);
  const changes = $derived(receipt ? reviewChanges(receipt.preview) : []);
  const notes = $derived.by(() => {
    const source = receipt?.preview.notes ?? receipt?.preview.warnings;
    return Array.isArray(source) ? source.filter((note): note is string => typeof note === 'string') : [];
  });
  const impact = $derived.by(() => {
    const p = receipt?.preview;
    if (!p) return '';
    if (tool === 'validate_download_plan') return p.first_execution === 'scheduled' ? 'scheduled' : 'first_check';
    if (tool === 'validate_download_task') return 'submit_download';
    if (tool === 'review_download_task_action' && p.action === 'delete')
      return p.delete_files ? 'delete_files' : 'keep_files';
    if (tool === 'review_download_task_action' && p.dispatch === 'deferred') return 'deferred';
    if (tool === 'review_workflow_schedule' && (p.action === 'pause' || p.action === 'delete')) return 'stop_schedule';
    if (tool === 'review_library_scan') return 'scan';
    if (tool === 'review_media_rescrape') return 'rescrape';
    if (tool === 'review_library_config' && Array.isArray(p.proposed_binding_order)) return 'binding_change';
    return '';
  });
  const outcome = $derived(receipt?.outcome);
  const statusText = $derived.by(() => {
    if (!receipt) return '';
    if (tool === 'validate_download_plan' && (receipt.status === 'reviewed' || receipt.status === 'superseded'))
      return $_('assistant.plan.' + receipt.status);
    const fallback = $_(
      'assistant.review.states.' + (receipt.status === 'failed' && receipt.retryable ? 'retryable' : receipt.status)
    );
    if (receipt.status === 'partial') {
      if (tool === 'validate_download_plan') return $_('assistant.review.partial_plan');
      if (tool === 'validate_download_task') return $_('assistant.review.partial_download');
      return fallback;
    }
    if (tool === 'validate_download_plan' && outcome?.outcome === 'absent' && typeof outcome.plan_id === 'number')
      return $_('assistant.review.plan_removed', outcome.plan_id);
    if (tool === 'validate_download_plan' && typeof outcome?.duplicate_plan_id === 'number')
      return $_('assistant.review.plan_exists', outcome.duplicate_plan_id);
    if (!outcome || !['applied', 'pending'].includes(receipt.status)) return fallback;
    if (tool === 'validate_download_plan' && outcome.created && typeof outcome.plan_id === 'number')
      return $_(outcome.already_exists ? 'assistant.review.plan_exists' : 'assistant.plan.created', outcome.plan_id);
    if (tool === 'validate_download_task' && outcome.created && typeof outcome.task_id === 'number')
      return $_('assistant.review.download_created', outcome.task_id);
    const target = outcome.job_id ?? outcome.task_id ?? outcome.item_id ?? outcome.library_id;
    if (typeof outcome.outcome !== 'string' || typeof target !== 'number') return fallback;
    const domain =
      tool === 'review_workflow_schedule'
        ? 'schedule'
        : tool === 'review_download_task_action'
          ? 'task_action'
          : 'media_change';
    return $_(`assistant.${domain}.${outcome.outcome}`, { values: { 0: target }, default: fallback });
  });
  const planName = $derived(
    String(fields.find(([key]) => key === 'field.keyword')?.[1] || $_('assistant.pages.download_plans'))
  );

  /**
   * Check whether the server still accepts a decision for this download plan.
   * @returns Whether the composer should offer to review the plan.
   */
  export function needsPlanDecision(): boolean {
    return tool === 'validate_download_plan' && !failed && pending;
  }

  /**
   * Open the stored plan and refresh its receipt before enabling a decision.
   */
  export function showPlanRecord(): void {
    if (!modal || modal.isOpen()) return;
    void load();
    modal.show();
  }

  /**
   * Submit a decision for the receipt currently on screen.
   * @param approve - Whether to create the plan or cancel the review.
   */
  async function decide(approve: boolean): Promise<void> {
    if (!pending || busy || failed) return;
    await load(approve);
    if (
      tool === 'validate_download_plan' &&
      !failed &&
      (receipt?.status === 'applied' || receipt?.status === 'cancelled')
    )
      modal?.close();
  }

  onDestroy(() => modal?.close());

  /**
   * Display one saved value from the immutable review.
   * @param value - A single field value.
   * @param key - Its optional localized field key.
   * @returns The readable value, with units or an explicit empty marker.
   */
  function display(value: unknown, key?: string): string {
    if (value === null || value === undefined || value === '') return $_('assistant.plan_change.cleared');
    if (key === 'binding_order' && Array.isArray(value))
      return value.length ? value.map((id) => '#' + id).join(' → ') : $_('assistant.plan_change.cleared');
    if (typeof value === 'boolean') return $_(value ? 'assistant.admin_review.yes' : 'assistant.admin_review.no');
    if (key === 'field.state' && typeof value === 'string')
      return $_('enum.download_state.' + value, { default: value });
    if ((key === 'field.trigger' || key === 'trigger') && typeof value === 'string')
      return $_('enum.job_trigger.' + value, { default: value });
    if (key === 'state' && typeof value === 'string') return $_('enum.job_state.' + value, { default: value });
    if ((key === 'assistant.review.interval_unit' || key === 'interval_unit') && typeof value === 'string')
      return $_('duration.' + value, { default: value });
    if (key === 'field.interval' && typeof value === 'number') return $_('assistant.plan_change.hours', value);
    if (key === 'assistant.plan_change.method' && typeof value === 'string')
      return $_('enum.transfer_method.' + value, { default: value });
    if (
      key &&
      [
        'field.interval_start',
        'field.interval_end',
        'field.run_date',
        'assistant.review.next_run',
        'interval_start',
        'interval_end',
        'run_date'
      ].includes(key) &&
      typeof value === 'string'
    )
      return $dateTime(value);
    return typeof value === 'object' ? JSON.stringify(value) : String(value);
  }
</script>

{#snippet details()}
  <section
    class={['space-y-2 text-sm', tool !== 'validate_download_plan' && 'rounded-box border border-base-content/15 p-3']}
    aria-busy={busy}
  >
    {#if receipt}
      {#if tool !== 'validate_download_plan'}<h4 class="font-semibold">
          {$_('assistant.review.actions.' + action)}
        </h4>{/if}
      <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
        {#each fields as [key, value] (key)}
          <dt class="text-base-content/70">{$_(key)}</dt>
          <dd class="break-words whitespace-pre-line">{display(value, key)}</dd>
        {/each}
      </dl>
      {#if changes.length}
        <dl class="space-y-1 border-t border-base-content/10 pt-2 text-xs">
          {#each changes as change, i (i)}
            <div>
              <dt class="font-medium">
                {['binding_order', 'interval_num', 'interval_unit', 'bootparams', 'repeatable', 'recoverable'].includes(
                  String(change.field)
                )
                  ? $_('assistant.review.' + change.field)
                  : $_('field.' + change.field, {
                      default: $_('metadata.fields.' + change.field, { default: String(change.field) })
                    })}
              </dt>
              <dd class="break-words">
                {display(change.before, String(change.field))} → {display(change.after, String(change.field))}
                {#if Array.isArray(change.maskedChanges) && change.maskedChanges.length}
                  <span class="block">{$_('assistant.review.masked_changes', change.maskedChanges.join(', '))}</span>
                {/if}
              </dd>
            </div>
          {/each}
        </dl>
      {/if}
      {#if impact}
        <p class="text-xs font-medium">{$_('assistant.review.impact.' + impact)}</p>
      {/if}
      {#if receipt.preview.ingestion && typeof receipt.preview.ingestion === 'object' && 'binding' in receipt.preview.ingestion && receipt.preview.ingestion.binding === 'add'}
        <p class="text-xs font-medium">{$_('assistant.review.impact.add_binding')}</p>
      {/if}
      {#if notes.length}
        <details class="text-xs">
          <summary class="cursor-pointer">{$_('assistant.review.notes')}</summary>
          <ul class="mt-1 list-disc space-y-1 pl-4">
            {#each notes as note, i (i)}<li class="break-words">{note}</li>{/each}
          </ul>
        </details>
      {/if}
      {#if outcome?.issues?.length && !outcome.duplicate_plan_id}
        <ul class="list-disc pl-4 text-xs">
          {#each outcome.issues as issue, index (index)}<li>
              {issue.path ? issue.path + ' · ' : ''}{issue.message}
            </li>{/each}
        </ul>
      {/if}
      <p role="status" class="text-xs text-base-content/75">
        {statusText}
      </p>
    {:else if busy}<span class="loading loading-xs loading-spinner" role="status" aria-label={$_('media.loading')}
      ></span>{/if}
    {#if failed}<p class="text-xs" role="alert">{$_('assistant.request_failed')}</p>{/if}
    <div class="flex flex-wrap justify-end gap-2">
      {#if failed || receipt?.status === 'pending' || receipt?.status === 'partial' || receipt?.status === 'uncertain' || (receipt?.status === 'failed' && !receipt.retryable)}
        <button type="button" class="btn btn-ghost btn-sm" disabled={busy || $assistant.busy} onclick={() => load()}
          >{$_('action.reload')}</button
        >
      {:else if onview && outcome?.created && receipt?.status === 'applied'}
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          onclick={() => {
            modal?.close();
            onview?.();
          }}
        >
          {$_('action.open', $_('assistant.pages.download_plans'))}
        </button>
      {:else if pending}
        {#if onedit && receipt?.status === 'reviewed'}
          <button
            type="button"
            class="btn mr-auto btn-ghost btn-sm"
            disabled={busy || $assistant.busy}
            onclick={() => {
              modal?.close();
              onedit?.();
            }}>{$_('assistant.plan.review')}</button
          >
        {/if}
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          disabled={busy || $assistant.busy}
          onclick={() => decide(false)}>{$_('message.cancel')}</button
        >
        <button
          type="button"
          class="btn btn-sm"
          class:btn-error={action === 'task_delete' || action === 'schedule_delete'}
          class:btn-primary={action !== 'task_delete' && action !== 'schedule_delete'}
          disabled={busy || $assistant.busy}
          onclick={() => decide(true)}>{$_('assistant.review.actions.' + action)}</button
        >
      {/if}
    </div>
  </section>
{/snippet}

{#if tool === 'validate_download_plan'}
  <button
    type="button"
    class="flex max-w-full items-center gap-1.5 rounded-field px-1 py-0.5 text-start text-xs leading-5 text-base-content/70 hover:bg-base-content/5 focus-visible:outline-2 focus-visible:outline-primary"
    aria-haspopup="dialog"
    onclick={showPlanRecord}
  >
    <iconify-icon icon={icons.document} width="0.875rem" class="shrink-0" aria-hidden="true"></iconify-icon>
    <span class="min-w-0 truncate"
      >{planName} · {failed ? $_('assistant.request_failed') : statusText || $_('media.loading')}</span
    >
    <iconify-icon icon={icons.chevronRight} width="0.75rem" class="shrink-0" aria-hidden="true"></iconify-icon>
  </button>
  {#if !createdInChat && outcome?.created && !outcome.already_exists && typeof outcome.plan_id === 'number'}
    <FirstCheckResult planId={outcome.plan_id} />
  {/if}
  <Modal bind:this={modal} shallow={false} title={$_('assistant.plan.record')}>
    {@render details()}
  </Modal>
{:else}
  {@render details()}
{/if}
