<script lang="ts">
  import { api } from '$lib/api';
  import { errorMessage } from '$lib/assistant/errors';
  import { isAssistantReview, scheduleDetail } from '$lib/assistant/reviews';
  import type { AssistantTool } from '$lib/assistant/session';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type {
    MediaBatch,
    MediaBatchOutcome,
    MediaChangeOutcome,
    PlanChangeOutcome,
    ScheduleChangeOutcome,
    Resp,
    TaskActionOutcome
  } from '$lib/types';
  import BatchPlanDialog from '../library/BatchPlanDialog.svelte';

  const {
    tool,
    callNumber,
    unsaved = false
  }: { tool: AssistantTool; callNumber?: number; unsaved?: boolean } = $props();

  const trialStatus = $derived.by(() => {
    if (tool.name !== 'run_workflow' || tool.status !== 'complete' || !tool.result?.ok) return null;
    const result = tool.result.data;
    if (typeof result?.status !== 'string') return null;
    if (
      result.status === 'completed' &&
      (result.evidence as { output_check?: string } | undefined)?.output_check === 'failed'
    ) {
      return 'invalid_output';
    }
    return result.status;
  });

  // reviewed schedule, plan, task and media changes share their review and outcome lines
  const changeKind = $derived(
    // batches share the media apply tool but report unit counts, not one target
    tool.name === 'review_media_batch' || tool.result?.data?.action === 'batch'
      ? 'media_batch'
      : (
          {
            review_workflow_schedule: 'schedule',
            apply_workflow_schedule: 'schedule',
            review_download_plan_change: 'plan_change',
            apply_download_plan_change: 'plan_change',
            review_download_task_action: 'task_action',
            apply_download_task_action: 'task_action',
            review_library_scan: 'media_change',
            review_media_rescrape: 'media_change',
            review_library_config: 'media_change',
            apply_media_change: 'media_change'
          } as Record<string, string>
        )[tool.name]
  );

  const batch = $derived(
    changeKind === 'media_batch' && tool.result?.ok ? (tool.result.data as unknown as MediaBatchOutcome) : null
  );
  // the stored plan a review or an outcome refers to
  const batchId = $derived(batch?.batch_id ?? null);

  let planDialog: BatchPlanDialog | undefined = $state();
  // a reviewed plan may be confirmed, stopped or finished in its dialog
  let livePlan = $state<MediaBatch | null>(null);
  const reviewedBatch = $derived(tool.name === 'review_media_batch' && batch?.valid ? batchId : null);

  $effect(() => {
    const id = reviewedBatch;
    if (id === null) return;

    api
      .get(`media/batch/${id}`, { retry: 0 })
      .json<Resp<MediaBatch>>()
      .then(({ data }) => {
        if (reviewedBatch === id) livePlan = data;
      })
      .catch(() => {
        // an expired plan keeps the review's own status
      });
  });

  const livePlanStatus = $derived(
    livePlan && livePlan.state !== 'reviewed' ? (livePlan.state === 'running' ? 'running' : livePlan.outcome) : null
  );

  /**
   * Translate a change status, using the wording shared by every change kind when the kind has no line of its own.
   *
   * @param kind - Change kind, such as `schedule` or `media_batch`.
   * @param status - Review or apply status.
   * @param values - Values for the message placeholders, in order.
   * @returns The translated status line.
   */
  function changeMessage(kind: string, status: string, values: (string | number)[] = []): string {
    const indexed = Object.fromEntries(values.map((value, index) => [index, value]));
    return $_(`assistant.${kind}.${status}`, {
      values: indexed,
      default: $_(`assistant.change.${status}`, { values: indexed })
    });
  }

  // applied outcomes name themselves; other replies say why nothing changed
  const changeStatus = $derived.by(() => {
    if (!changeKind || !tool.name.startsWith('apply_') || !tool.result?.ok || !tool.result.data) return null;
    const result = tool.result.data as unknown as
      ScheduleChangeOutcome | PlanChangeOutcome | TaskActionOutcome | MediaChangeOutcome | MediaBatchOutcome;
    if (result.outcome) return result.outcome;
    for (const key of ['confirmation_required', 'stale', 'conflict', 'pending', 'failed'] as const) {
      if ((result as Record<string, unknown>)[key]) return key;
    }
    return result.valid === false ? 'invalid' : 'refused';
  });

  const statusIcon = $derived.by(() => {
    if (trialStatus === 'running' || trialStatus === 'submitting') return icons.clock;
    if (trialStatus && trialStatus !== 'completed') return icons.warning;
    // queued, submitted and running work is accepted but not yet observed
    if (changeStatus && ['pending', 'queued', 'submitted', 'running'].includes(changeStatus)) return icons.clock;
    if (
      changeStatus &&
      ![
        'created',
        'updated',
        'enabled',
        'paused',
        'deleted',
        'changed',
        'scanned',
        'rescraped',
        'configured',
        'completed'
      ].includes(changeStatus)
    ) {
      return icons.warning;
    }
    if (tool.status === 'complete') return icons.checkmark;
    if (tool.status === 'error') return icons.warning;
    return icons.subtractCircle;
  });

  let everOpened = $state(false);
  const answers = $derived(
    tool.name === 'ask_user' && tool.result?.ok
      ? (tool.result.data as import('$lib/types').QuestionCard | undefined)?.summary
      : null
  );
  const hasDetail = $derived(tool.name !== 'ask_user' && !!tool.result?.data);
  const detail = $derived(
    everOpened && hasDetail
      ? JSON.stringify(tool.result!.data, tool.name.includes('workflow_schedule') ? scheduleDetail : undefined, 2)
      : ''
  );
  const error = $derived(tool.result?.error ? errorMessage($_, tool.result.error.code, 'assistant.tool_failed') : '');
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
    {callNumber
      ? $_('assistant.activity_call', callNumber)
      : $_(`assistant.tools.${tool.name}`, { default: tool.name })}
  </span>
  <span class="shrink-0" class:sr-only={!trialStatus && (tool.status === 'complete' || tool.status === 'running')}>
    {trialStatus
      ? $_(`assistant.trial_status.${trialStatus}`, { default: $_('assistant.trial_status.unknown') })
      : $_(`assistant.tool_status.${tool.status}`)}
  </span>
{/snippet}

{#if hasDetail || error}
  <details
    class="group"
    ontoggle={(event) => {
      if (event.currentTarget.open) everOpened = true;
    }}
  >
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
{#if tool.name === 'navigate' && tool.result?.data?.device_outcome}
  <p class="ml-8 text-xs" role="status">{$_(`assistant.device.${tool.result.data.device_outcome}`)}</p>
{/if}
{#if answers?.length}
  <dl class="ml-8 grid gap-1 text-sm">
    {#each answers as answer (answer.id)}
      <div>
        <dt class="text-xs text-base-content/65">{answer.title || $_('assistant.question.plan_title')}</dt>
        <dd>{answer.skipped ? $_('assistant.question.skipped') : answer.values.join(' · ')}</dd>
      </div>
    {/each}
  </dl>
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
{#if tool.name === 'create_download_plan' && tool.result?.ok && tool.result.data?.created}
  <p class="ml-8 text-xs leading-5 text-base-content/60">
    {$_(
      tool.result.data.already_exists ? 'assistant.review.plan_exists' : 'assistant.plan.created',
      Number(tool.result.data.plan_id)
    )}
  </p>
{/if}
{#if tool.name === 'create_download_plan' && tool.result?.ok && tool.result.data?.confirmation_required}
  <p class="ml-8 text-xs leading-5 text-base-content/60">{$_('assistant.plan.confirmation_required')}</p>
{/if}
{#if livePlanStatus && livePlan}
  <p class="ml-8 text-xs leading-5 text-base-content/60" role="status">
    {$_(`assistant.media_batch.${livePlanStatus}`, [
      livePlan.counts.done,
      livePlan.counts.skipped + livePlan.counts.failures + livePlan.counts.uncertain,
      livePlan.counts.remaining
    ])}
  </p>
{:else if changeKind && !isAssistantReview(tool.result) && tool.name.startsWith('review_') && tool.result?.ok && tool.result.data?.valid}
  <p class="ml-8 text-xs leading-5 text-base-content/60">{changeMessage(changeKind, 'reviewed')}</p>
{/if}
{#if batchId !== null}
  <button
    type="button"
    class="btn ml-8 btn-outline btn-sm"
    onclick={() => planDialog?.showModal(livePlan?.id ?? batchId)}
  >
    {$_('assistant.media_batch.view_plan')}
  </button>
  <BatchPlanDialog
    bind:this={planDialog}
    onstate={(plan) => {
      // switching between keeping and deleting files moves the review to another plan
      if (reviewedBatch !== null) livePlan = plan;
    }}
  />
{/if}
{#if changeStatus && batch}
  <p class="ml-8 text-xs leading-5 text-base-content/60" role="status">
    {changeMessage('media_batch', changeStatus, [
      Number(batch.done ?? 0),
      Number(batch.skipped ?? 0) + Number(batch.failures ?? 0) + Number(batch.uncertain ?? 0),
      Number(batch.remaining ?? 0)
    ])}
  </p>
{:else if changeStatus}
  <p class="ml-8 text-xs leading-5 text-base-content/60" role="status">
    {changeMessage(changeKind!, changeStatus, [
      Number(
        tool.result?.data?.job_id ??
          tool.result?.data?.plan_id ??
          tool.result?.data?.task_id ??
          tool.result?.data?.item_id ??
          tool.result?.data?.library_id
      )
    ])}
  </p>
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
