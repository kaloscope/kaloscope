import type { AssistantToolResult } from '$lib/types';

export const reviewTools = [
  'validate_download_plan',
  'validate_download_task',
  'review_download_task_action',
  'review_workflow_schedule',
  'review_library_scan',
  'review_media_rescrape',
  'review_library_config'
] as const;

export type ReviewTool = (typeof reviewTools)[number];
type Fact = Record<string, unknown>;

export type AssistantReview = {
  tool: ReviewTool;
  request_id: string;
  status:
    | 'reviewed'
    | 'cancelled'
    | 'superseded'
    | 'incomplete'
    | 'applied'
    | 'pending'
    | 'partial'
    | 'stale'
    | 'failed'
    | 'uncertain';
  retryable?: boolean;
  preview: Fact;
  outcome?: (Fact & { issues?: { path?: string; message: string }[] }) | null;
};

/**
 * Identify an actionable preview that supplied its own stable request ID.
 * @param result - Checked server tool event.
 * @returns Whether the panel can request a decision for this exact review.
 */
export function isAssistantReview(
  result?: AssistantToolResult
): result is AssistantToolResult & { name: ReviewTool; data: Fact & { request_id: string } } {
  return (
    !!result?.ok &&
    result.data?.valid === true &&
    typeof result.data.request_id === 'string' &&
    reviewTools.some((name) => name === result.name)
  );
}

/**
 * Select only object fields that can be displayed as named review facts.
 * @param value - A value from the server's frozen preview.
 * @returns The object, or an empty record for missing or scalar values.
 */
function record(value: unknown): Fact {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Fact) : {};
}

/**
 * Limit schedule arguments to their top-level names in expanded tool output.
 * @param key - Field currently serialized by `JSON.stringify`.
 * @param value - Original value for this field.
 * @returns Names for boot parameters, or the original value for other fields.
 */
export function scheduleDetail(this: unknown, key: string, value: unknown): unknown {
  if (key === 'bootparams' || (record(this).field === 'bootparams' && (key === 'before' || key === 'after'))) {
    return Object.keys(record(value));
  }
  return value;
}

/**
 * Combine a server-verified resource name and its immutable numeric identity.
 * @param name - Human-readable name when available.
 * @param id - Resource identifier from the preview.
 * @returns Display text, or undefined if neither part exists.
 */
function identity(name: unknown, id: unknown): string | undefined {
  return (
    [typeof name === 'string' && name, typeof id === 'number' && `#${id}`].filter(Boolean).join(' · ') || undefined
  );
}

/**
 * Extract the target and material settings from an immutable preview.
 * @param receipt - Server receipt matched to the card's original tool and request.
 * @returns Ordered field labels and values for this action.
 */
export function reviewFields(receipt: AssistantReview): [string, unknown][] {
  const p = receipt.preview;
  const plan = record(p.plan);
  const download = record(p.download);
  const ingestion = record(p.ingestion);
  const schedule = record(p.after ?? p.before);
  const first = record(p.first_run);
  const fields: [string, unknown][] = [];
  switch (receipt.tool) {
    case 'validate_download_plan':
    case 'validate_download_task': {
      const settings = receipt.tool === 'validate_download_plan' ? plan : download;
      fields.push(
        ['field.graph', identity(p.workflow_name ?? settings.workflow_name, p.workflow_id ?? settings.workflow_id)],
        ['field.keyword', settings.keyword],
        ['field.title', settings.title],
        ['download.downloader.title', identity(settings.downloader_name, settings.downloader_id)],
        ['field.dir', settings.dir],
        ['entity.filters', settings.filters],
        ['field.interval', settings.interval_num],
        ['assistant.replacement.library', identity(ingestion.library_name, settings.transfer_lib_id)],
        ['assistant.plan_change.method', settings.transfer_method],
        ['assistant.plan_change.pattern', settings.sub_pattern],
        ['assistant.plan_change.replacement', settings.sub_repl],
        ['assistant.review.binding', identity(ingestion.workflow_name, ingestion.workflow_id)],
        ['field.interval_start', settings.interval_start],
        ['field.interval_end', settings.interval_end],
        ['field.batch_limit', settings.batch_limit],
        ['field.total_limit', settings.total_limit]
      );
      break;
    }
    case 'review_download_task_action':
      fields.push(
        ['entity.task', identity(p.name, p.task_id)],
        ['download.downloader.title', identity(p.downloader_name, p.downloader_id)],
        ['field.state', p.state],
        ['assistant.replacement.library', identity(p.transfer_lib_name, p.transfer_lib_id)]
      );
      break;
    case 'review_workflow_schedule':
      fields.push(
        ['field.graph', identity(p.workflow_name, p.workflow_id)],
        ['entity.job', identity(null, p.job_id)],
        ['field.trigger', schedule.trigger],
        ['assistant.review.cron_utc', schedule.cron_expr],
        ['field.run_date', schedule.run_date],
        ['assistant.review.interval_num', schedule.interval_num],
        ['assistant.review.interval_unit', schedule.interval_unit],
        ['field.interval_start', schedule.interval_start],
        ['field.interval_end', schedule.interval_end],
        ['assistant.review.next_run', first.local ?? first.estimate],
        ['assistant.review.repeatable', schedule.repeatable],
        ['assistant.review.recoverable', schedule.recoverable],
        ['assistant.review.bootparams', Object.keys(record(schedule.bootparams)).join(' · ')]
      );
      break;
    case 'review_media_rescrape':
      fields.push(
        ['assistant.replacement.library', identity(p.library_name, p.library_id)],
        ['assistant.admin_review.fields.item_id', identity(p.title ?? p.name, p.item_id)],
        ['field.graph', identity(p.workflow_name, p.workflow_id)],
        ['assistant.review.match', record(p.candidate).title],
        ['assistant.review.destination', record(p.organization).expected]
      );
      break;
    case 'review_library_config':
      fields.push(['assistant.replacement.library', identity(p.library_name, p.library_id)]);
      break;
    case 'review_library_scan':
      fields.push(
        ['assistant.replacement.library', identity(p.library_name, p.library_id)],
        ['assistant.review.queued_events', p.queued_events],
        [
          'assistant.review.binding',
          Array.isArray(p.bindings)
            ? p.bindings
                .map((entry) => {
                  const workflow = record(entry);
                  return identity(workflow.workflow_name ?? workflow.name, workflow.workflow_id ?? workflow.id);
                })
                .filter(Boolean)
                .join(' → ')
            : null
        ]
      );
      break;
  }
  return fields.filter(
    ([, value]) =>
      value !== undefined &&
      value !== null &&
      value !== '' &&
      !(typeof value === 'object' && !Object.keys(value).length)
  );
}

/**
 * Return material before-and-after changes supplied by the reviewed service.
 * @param preview - Frozen facts displayed to the owner.
 * @returns Changed fields in display order.
 */
export function reviewChanges(preview: Fact): Fact[] {
  const values = preview.changes ?? preview.settings;
  const changes = Array.isArray(values) ? values.map(record).filter((entry) => typeof entry.field === 'string') : [];
  for (const [i, entry] of changes.entries()) {
    if (entry.field === 'bootparams') {
      changes[i] = {
        field: entry.field,
        before: Object.keys(record(entry.before)).join(' · '),
        after: Object.keys(record(entry.after)).join(' · ')
      };
    }
  }
  if (Array.isArray(preview.proposed_binding_order)) {
    changes.push({ field: 'binding_order', before: preview.binding_order, after: preview.proposed_binding_order });
  }
  return changes;
}

/**
 * Refresh the open application view after a committed or dispatched decision.
 * @param receipt - Server response to an explicit confirmation.
 */
export function refreshReviewTarget(receipt: AssistantReview) {
  switch (receipt.tool) {
    case 'validate_download_plan':
      window.dispatchEvent(new Event('download-plan-created'));
      break;
    case 'validate_download_task':
    case 'review_download_task_action':
      window.dispatchEvent(new Event('download-task-changed'));
      break;
    case 'review_workflow_schedule':
      window.dispatchEvent(new Event('workflow-job-changed'));
      break;
    case 'review_media_rescrape':
      window.dispatchEvent(
        new CustomEvent('media-item-changed', {
          detail: { item_id: receipt.preview.item_id, library_id: receipt.preview.library_id }
        })
      );
      break;
    case 'review_library_scan':
    case 'review_library_config':
      window.dispatchEvent(
        new CustomEvent(receipt.tool === 'review_library_scan' ? 'media-library-scan' : 'media-library-changed', {
          detail: { library_id: receipt.preview.library_id }
        })
      );
      break;
  }
}
