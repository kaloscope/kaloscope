<script lang="ts">
  import ReviewCardShell from './ReviewCardShell.svelte';
  import { createReviewCard } from '$lib/assistant/review-card.svelte';
  import { formatReviewValue, planFieldLabels, refreshDomainTarget } from '$lib/assistant/reviews';
  import { assistant } from '$lib/assistant/session';
  import { _, dateTime } from '$lib/i18n';
  import type { PlanReplacement } from '$lib/types';

  const { id }: { id: string } = $props();
  const headingId = $props.id();
  const card = createReviewCard<PlanReplacement>({
    path: () => `download/plan/replacement/${id}`,
    decision: (receipt, approve) => ({ digest: receipt.digest, approve }),
    events: ['download-plan-changed', 'assistant-replacement-changed'],
    received(data, previous, approve) {
      if (approve !== undefined) card.notify('assistant-replacement-changed');
      if (data.status === 'applied' && (approve !== undefined || (previous && previous.status !== 'applied'))) {
        refreshDomainTarget({ kind: 'download_plan_changed', source: card.source });
      }
    }
  });
  const receipt = $derived(card.receipt);
  const busy = $derived(card.busy);
  const failed = $derived(card.failed);
  const load = card.load;
  const showRefresh = $derived(
    failed || receipt?.status === 'approved' || receipt?.status === 'uncertain' || receipt?.status === 'applied'
  );
  const showCancel = $derived(!failed && !!receipt && ['pending', 'approved', 'uncertain'].includes(receipt.status));
  const showConfirm = $derived(!failed && receipt?.status === 'pending');
  const hasActions = $derived(showRefresh || showCancel || showConfirm);

  /**
   * Format one setting from the frozen replacement plan with optional empty text.
   *
   * @param value - Stored plan field value.
   * @param key - Plan field identifier for enum, date, or object formatting.
   * @param emptyText - Localized placeholder for unset values.
   * @returns Readable localized text for the settings table.
   */
  function display(value: unknown, key?: string, emptyText?: string): string {
    return formatReviewValue(value, {
      t: $_,
      formatDate: $dateTime,
      key,
      emptyText,
      emptyObjectAsEmpty: key === 'filters'
    });
  }
</script>

<ReviewCardShell
  {busy}
  {failed}
  loaded={!!receipt}
  {hasActions}
  labelledBy={headingId}
  status={receipt ? $_(`assistant.replacement.states.${receipt.status}`, receipt.new_plan_id ?? '') : ''}
>
  {#snippet header()}
    <h3 id={headingId} class="font-semibold">{$_('assistant.replacement.title')}</h3>
  {/snippet}
  {#if receipt}
    <p class="break-words">#{receipt.plan_id} · {receipt.before.keyword}</p>
    <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
      <dt class="text-base-content/70">{$_(planFieldLabels.graph_id)}</dt>
      <dd class="break-words">{receipt.before.workflow_name ?? ''} #{receipt.before.graph_id}</dd>
      <dt class="text-base-content/70">{$_(planFieldLabels.dir)}</dt>
      <dd class="break-all">{receipt.before.dir}</dd>
      {#if receipt.before.transfer_lib_id}<dt class="text-base-content/70">{$_(planFieldLabels.transfer_lib_id)}</dt>
        <dd class="break-words">{receipt.before.transfer_lib_name ?? ''} #{receipt.before.transfer_lib_id}</dd>{/if}
      <dt class="text-base-content/70">{$_(planFieldLabels.interval_num)}</dt>
      <dd>{$_('field.every', `${receipt.before.interval_num} ${$_('duration.hours')}`)}</dd>
      <dt class="text-base-content/70">{$_('assistant.replacement.history')}</dt>
      <dd>
        {$_(receipt.keep_history ? 'assistant.replacement.keep' : 'assistant.replacement.reset', [
          receipt.before.history_count,
          receipt.before.total_count
        ])}
      </dd>
      <dt class="text-base-content/70">{$_('assistant.replacement.next')}</dt>
      <dd>
        {#if receipt.after.schedule === 'blocked'}
          {$_(`assistant.replacement.${receipt.after.blocked_by}`)}
        {:else if receipt.after.schedule === 'immediate'}
          {$_('assistant.replacement.immediate')}
        {:else}
          {$dateTime(receipt.after.next_due)}
        {/if}
      </dd>
      {#if receipt.status === 'pending'}
        <dt class="text-base-content/70">{$_('assistant.replacement.expires')}</dt>
        <dd>{$dateTime(receipt.expires_at)}</dd>
      {/if}
    </dl>
    <details class="rounded-field bg-base-200/50 px-2 py-1.5">
      <summary class="cursor-pointer text-xs font-medium">{$_('assistant.replacement.settings')}</summary>
      <dl class="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
        <dt class="text-base-content/70">{$_(planFieldLabels.downloader_id)}</dt>
        <dd class="break-words">{receipt.before.downloader_name ?? ''} #{receipt.before.downloader_id}</dd>
        <dt class="text-base-content/70">{$_(planFieldLabels.filters)}</dt>
        <dd class="break-all">{display(receipt.before.filters, 'filters')}</dd>
        <dt class="text-base-content/70">{$_(planFieldLabels.transfer_method)}</dt>
        <dd>{display(receipt.before.transfer_method, 'transfer_method')}</dd>
        <dt class="text-base-content/70">{$_(planFieldLabels.sub_pattern)}</dt>
        <dd class="break-all">{display(receipt.before.sub_pattern)}</dd>
        <dt class="text-base-content/70">{$_(planFieldLabels.sub_repl)}</dt>
        <dd class="break-all">{display(receipt.before.sub_repl)}</dd>
        <dt class="text-base-content/70">{$_(planFieldLabels.batch_limit)}</dt>
        <dd>{receipt.before.batch_limit}</dd>
        <dt class="text-base-content/70">{$_(planFieldLabels.total_limit)}</dt>
        <dd>{display(receipt.before.total_limit, undefined, $_('assistant.replacement.no_limit'))}</dd>
        <dt class="text-base-content/70">{$_('assistant.replacement.count_after')}</dt>
        <dd>{receipt.after.total_count}</dd>
        <dt class="text-base-content/70">{$_(planFieldLabels.interval_start)}</dt>
        <dd>{display(receipt.before.interval_start, 'interval_start', $_('assistant.replacement.no_limit'))}</dd>
        <dt class="text-base-content/70">{$_(planFieldLabels.interval_end)}</dt>
        <dd>{display(receipt.before.interval_end, 'interval_end', $_('assistant.replacement.no_limit'))}</dd>
      </dl>
    </details>
  {/if}
  {#snippet extraStatus()}
    {#if receipt?.status === 'applied' && receipt.first_execution}
      <p class="text-xs text-base-content/75" role="status">
        {$_(`assistant.plan.first.${receipt.first_execution.status}`)}
        {#if receipt.first_execution.added != null}
          {$_('assistant.replacement.outcome_counts', [
            receipt.first_execution.added,
            receipt.first_execution.errors ?? 0,
            receipt.first_execution.uncertain ?? 0
          ])}
        {/if}
      </p>
    {/if}
  {/snippet}
  {#snippet actions()}
    {#if showRefresh}
      <button type="button" class="btn btn-ghost btn-sm" disabled={busy || $assistant.busy} onclick={() => load()}
        >{$_('assistant.review.refresh')}</button
      >
    {/if}
    {#if showCancel}
      <button type="button" class="btn btn-ghost btn-sm" disabled={busy || $assistant.busy} onclick={() => load(false)}
        >{$_('message.cancel')}</button
      >
    {/if}
    {#if showConfirm}
      <button type="button" class="btn btn-primary btn-sm" disabled={busy || $assistant.busy} onclick={() => load(true)}
        >{$_('assistant.replacement.confirm')}</button
      >
    {/if}
  {/snippet}
</ReviewCardShell>
