<script lang="ts">
  import { createReviewCard } from '$lib/assistant/review-card.svelte';
  import { assistant } from '$lib/assistant/session';
  import { _, dateTime } from '$lib/i18n';
  import type { PlanReplacement } from '$lib/types';

  const { id }: { id: string } = $props();
  const card = createReviewCard<PlanReplacement>({
    path: () => `download/plan/replacement/${id}`,
    decision: (receipt, approve) => ({ digest: receipt.digest, approve }),
    events: ['download-plan-changed', 'assistant-replacement-changed'],
    received(data, previous, approve) {
      if (approve !== undefined) card.notify('assistant-replacement-changed');
      if (data.status === 'applied' && (approve !== undefined || (previous && previous.status !== 'applied'))) {
        card.notify('download-plan-changed');
      }
    }
  });
  const receipt = $derived(card.receipt);
  const busy = $derived(card.busy);
  const failed = $derived(card.failed);
  const load = card.load;
</script>

<section
  aria-label={$_('assistant.replacement.title')}
  aria-busy={busy}
  class="space-y-2 rounded-box border border-base-content/15 p-3 text-sm"
>
  <h4 class="font-semibold">{$_('assistant.replacement.title')}</h4>
  {#if receipt}
    <p class="break-words">#{receipt.plan_id} · {receipt.before.keyword}</p>
    <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
      <dt>{$_('field.graph')}</dt>
      <dd class="break-words">{receipt.before.workflow_name ?? ''} #{receipt.before.graph_id}</dd>
      <dt>{$_('field.dir')}</dt>
      <dd class="break-all">{receipt.before.dir}</dd>
      {#if receipt.before.transfer_lib_id}<dt>{$_('assistant.replacement.library')}</dt>
        <dd class="break-words">{receipt.before.transfer_lib_name ?? ''} #{receipt.before.transfer_lib_id}</dd>{/if}
      <dt>{$_('field.interval')}</dt>
      <dd>{$_('field.every', `${receipt.before.interval_num} ${$_('duration.hours')}`)}</dd>
      <dt>{$_('assistant.replacement.history')}</dt>
      <dd>
        {$_(receipt.keep_history ? 'assistant.replacement.keep' : 'assistant.replacement.reset', [
          receipt.before.history_count,
          receipt.before.total_count
        ])}
      </dd>
      <dt>{$_('assistant.replacement.next')}</dt>
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
        <dt>{$_('assistant.replacement.expires')}</dt>
        <dd>{$dateTime(receipt.expires_at)}</dd>
      {/if}
    </dl>
    <details class="rounded-field bg-base-200/50 px-2 py-1.5">
      <summary class="cursor-pointer text-xs font-medium">{$_('assistant.replacement.settings')}</summary>
      <dl class="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
        <dt>{$_('download.downloader.title')}</dt>
        <dd class="break-words">{receipt.before.downloader_name ?? ''} #{receipt.before.downloader_id}</dd>
        <dt>{$_('entity.filters')}</dt>
        <dd class="break-all">
          {receipt.before.filters && Object.keys(receipt.before.filters).length
            ? JSON.stringify(receipt.before.filters)
            : $_('assistant.plan_change.cleared')}
        </dd>
        <dt>{$_('assistant.plan_change.method')}</dt>
        <dd>
          {receipt.before.transfer_method
            ? $_(`enum.transfer_method.${receipt.before.transfer_method}`)
            : $_('assistant.plan_change.cleared')}
        </dd>
        <dt>{$_('assistant.plan_change.pattern')}</dt>
        <dd class="break-all">{receipt.before.sub_pattern || $_('assistant.plan_change.cleared')}</dd>
        <dt>{$_('assistant.plan_change.replacement')}</dt>
        <dd class="break-all">{receipt.before.sub_repl || $_('assistant.plan_change.cleared')}</dd>
        <dt>{$_('field.batch_limit')}</dt>
        <dd>{receipt.before.batch_limit}</dd>
        <dt>{$_('field.total_limit')}</dt>
        <dd>{receipt.before.total_limit ?? $_('assistant.replacement.no_limit')}</dd>
        <dt>{$_('assistant.replacement.count_after')}</dt>
        <dd>{receipt.after.total_count}</dd>
        <dt>{$_('field.interval_start')}</dt>
        <dd>
          {receipt.before.interval_start
            ? $dateTime(receipt.before.interval_start)
            : $_('assistant.replacement.no_limit')}
        </dd>
        <dt>{$_('field.interval_end')}</dt>
        <dd>
          {receipt.before.interval_end ? $dateTime(receipt.before.interval_end) : $_('assistant.replacement.no_limit')}
        </dd>
      </dl>
    </details>
    <p role="status">{$_(`assistant.replacement.states.${receipt.status}`, receipt.new_plan_id ?? '')}</p>
    {#if receipt.status === 'applied' && receipt.first_execution}
      <p class="text-xs" role="status">
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
  {/if}
  {#if failed}<p role="alert">{$_('assistant.request_failed')}</p>{/if}
  <div class="flex flex-wrap justify-end gap-2">
    {#if failed || receipt?.status === 'approved' || receipt?.status === 'uncertain' || receipt?.status === 'applied'}
      <button type="button" class="btn btn-ghost btn-sm" disabled={busy || $assistant.busy} onclick={() => load()}
        >{$_('assistant.replacement.refresh')}</button
      >
    {/if}
    {#if !failed && receipt && ['pending', 'approved', 'uncertain'].includes(receipt.status)}
      <button type="button" class="btn btn-ghost btn-sm" disabled={busy || $assistant.busy} onclick={() => load(false)}
        >{$_('assistant.replacement.cancel')}</button
      >
    {/if}
    {#if !failed && receipt?.status === 'pending'}
      <button type="button" class="btn btn-primary btn-sm" disabled={busy || $assistant.busy} onclick={() => load(true)}
        >{$_('assistant.replacement.confirm')}</button
      >
    {/if}
  </div>
</section>
