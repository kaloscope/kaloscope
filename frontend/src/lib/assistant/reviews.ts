import type { AssistantToolResult } from '$lib/types';
import type { AssistantTranslator } from './errors';

const reviewTools = [
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

export const planFieldLabels = {
  graph_id: 'field.graph',
  keyword: 'field.keyword',
  title: 'field.title',
  downloader_id: 'download.downloader.title',
  dir: 'field.dir',
  filters: 'entity.filters',
  interval_num: 'field.interval',
  transfer_lib_id: 'assistant.replacement.library',
  transfer_method: 'assistant.plan_change.method',
  sub_pattern: 'assistant.plan_change.pattern',
  sub_repl: 'assistant.plan_change.replacement',
  interval_start: 'field.interval_start',
  interval_end: 'field.interval_end',
  batch_limit: 'field.batch_limit',
  total_limit: 'field.total_limit'
} as const;

export const planChangeFieldLabels: Record<string, string> = {
  ...planFieldLabels,
  dir: 'assistant.plan_change.download_directory',
  transfer_lib_id: 'assistant.plan_change.transfer_library'
};

export type DomainRefreshTarget =
  | { kind: 'download_plan_created' }
  | { kind: 'download_plan_changed'; source?: symbol }
  | { kind: 'download_task_changed' }
  | { kind: 'workflow_job_changed' }
  | { kind: 'media_item_changed'; item_id: unknown; library_id: unknown }
  | { kind: 'media_library_changed'; library_id: unknown }
  | { kind: 'media_library_scan'; library_id: unknown };

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
 * Identify a standalone review, leaving release selection to its question card.
 *
 * @param result - Checked server tool event.
 * @param asksForChoices - Whether the same turn issued a question after preparing settings.
 * @returns Whether the panel can request a decision for this exact review.
 */
export function isAssistantReview(
  result?: AssistantToolResult,
  asksForChoices = false
): result is AssistantToolResult & { name: ReviewTool; data: Fact & { request_id: string } } {
  const status = record(result?.data?.assistant_submission).status;
  return (
    !!result?.ok &&
    result.data?.valid === true &&
    typeof result.data.request_id === 'string' &&
    !(
      result.name === 'validate_download_plan' &&
      (status === 'needs_confirmation' || (asksForChoices && typeof status === 'string' && status !== 'ready'))
    ) &&
    reviewTools.some((name) => name === result.name)
  );
}

/**
 * Select only object fields that can be displayed as named review facts.
 *
 * @param value - A value from the server's frozen preview.
 * @returns The object, or an empty record for missing or scalar values.
 */
function record(value: unknown): Fact {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Fact) : {};
}

const sensitiveParameter =
  /password|passwd|passphrase|secret|token|authorization|credential|cookie|api[-_]?key|private[-_]?key|access[-_]?key/i;

/**
 * Prepare schedule values for display while masking nested credential fields.
 *
 * @param value - Parameter value from the schedule preview.
 * @returns Its public value with credential-bearing fields masked.
 */
function publicParameters(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(publicParameters);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [key, sensitiveParameter.test(key) ? '••••' : publicParameters(item)])
  );
}

/**
 * Render the same public parameter values in expanded schedule details and approvals.
 *
 * @param key - Field currently serialized.
 * @param value - Original value for the field.
 * @returns The public review value.
 */
export function scheduleDetail(this: unknown, key: string, value: unknown): unknown {
  if (key === 'bootparams' || (record(this).field === 'bootparams' && (key === 'before' || key === 'after'))) {
    return publicParameters(value);
  }
  return value;
}

/**
 * Combine a server-verified resource name and its immutable numeric identity.
 *
 * @param name - Human-readable name when available.
 * @param id - Resource identifier from the preview.
 * @returns Display text, or undefined if neither part exists.
 */
function identity(name: unknown, id: unknown): string | undefined {
  return (
    [typeof name === 'string' && name, typeof id === 'number' && `#${id}`].filter(Boolean).join(' · ') || undefined
  );
}

const dateFieldKeys = new Set([
  'field.interval_start',
  'field.interval_end',
  'field.run_date',
  'assistant.review.next_run',
  'interval_start',
  'interval_end',
  'run_date'
]);

/**
 * Format one saved field value for display inside an approval or replacement card.
 *
 * @param value - Stored field value from a frozen preview.
 * @param options - Formatting helpers, field key, empty marker, and object display mode.
 * @returns A readable localized representation without exposing raw objects unnecessarily.
 */
export function formatReviewValue(
  value: unknown,
  options: {
    t: AssistantTranslator;
    formatDate?: (value: string) => string;
    key?: string;
    emptyText?: string;
    structuredObjects?: boolean;
    intervalAsHours?: boolean;
    emptyObjectAsEmpty?: boolean;
  }
): string {
  const {
    t,
    formatDate,
    key,
    structuredObjects = false,
    intervalAsHours = false,
    emptyObjectAsEmpty = false
  } = options;
  const empty = options.emptyText ?? t('assistant.plan_change.cleared');

  if (structuredObjects) {
    if (Array.isArray(value)) {
      return value.length
        ? value.map((item) => formatReviewValue(item, { t, emptyText: empty, structuredObjects })).join(', ')
        : empty;
    }
    if (value === null || value === undefined) return empty;
    if (typeof value === 'boolean') return t(value ? 'assistant.admin_review.yes' : 'assistant.admin_review.no');
    if (typeof value === 'object') {
      return Object.entries(value)
        .map(
          ([name, item]) =>
            t('assistant.admin_review.fields.' + name, { default: name }) +
            ' · ' +
            formatReviewValue(item, { t, key: name, emptyText: empty, structuredObjects })
        )
        .join(', ');
    }
    if (key === 'library_type') return t('enum.lib_type.' + value, { default: String(value) });
    return String(value);
  }

  if (value === null || value === undefined || value === '') return empty;
  if (key === 'binding_order' && Array.isArray(value)) {
    return value.length ? value.map((id) => '#' + id).join(' → ') : empty;
  }
  if (typeof value === 'boolean') return t(value ? 'assistant.admin_review.yes' : 'assistant.admin_review.no');
  if (key === 'field.state' && typeof value === 'string') {
    return t('enum.download_state.' + value, { default: value });
  }
  if ((key === 'field.trigger' || key === 'trigger') && typeof value === 'string') {
    return t('enum.job_trigger.' + value, { default: value });
  }
  if (key === 'state' && typeof value === 'string') {
    return t('enum.job_state.' + value, { default: value });
  }
  if ((key === 'assistant.review.interval_unit' || key === 'interval_unit') && typeof value === 'string') {
    return t('duration.' + value, { default: value });
  }
  if (key === 'field.interval' && typeof value === 'number') {
    return t('assistant.plan_change.hours', value);
  }
  if (intervalAsHours && key === 'interval_num') {
    return t('assistant.plan_change.hours', String(value));
  }
  if ((key === 'assistant.plan_change.method' || key === 'transfer_method') && typeof value === 'string') {
    return t('enum.transfer_method.' + value, { default: value });
  }
  if (key && dateFieldKeys.has(key) && formatDate) {
    if (typeof value === 'string' || key === 'interval_start' || key === 'interval_end') {
      return formatDate(String(value));
    }
  }
  if (typeof value === 'object') {
    if (emptyObjectAsEmpty && !Object.keys(value).length) return empty;
    return JSON.stringify(value);
  }
  return String(value);
}

/**
 * Extract the target and material settings from an immutable preview.
 *
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
        [
          planFieldLabels.graph_id,
          identity(p.workflow_name ?? settings.workflow_name, p.workflow_id ?? settings.workflow_id)
        ],
        [planFieldLabels.keyword, settings.keyword],
        [planFieldLabels.title, settings.title],
        [planFieldLabels.downloader_id, identity(settings.downloader_name, settings.downloader_id)],
        [planFieldLabels.dir, settings.dir],
        [planFieldLabels.filters, settings.filters],
        [planFieldLabels.interval_num, settings.interval_num],
        [planFieldLabels.transfer_lib_id, identity(ingestion.library_name, settings.transfer_lib_id)],
        [planFieldLabels.transfer_method, settings.transfer_method],
        [planFieldLabels.sub_pattern, settings.sub_pattern],
        [planFieldLabels.sub_repl, settings.sub_repl],
        ['assistant.review.binding', identity(ingestion.workflow_name, ingestion.workflow_id)],
        [planFieldLabels.interval_start, settings.interval_start],
        [planFieldLabels.interval_end, settings.interval_end],
        [planFieldLabels.batch_limit, settings.batch_limit],
        [planFieldLabels.total_limit, settings.total_limit]
      );
      break;
    }
    case 'review_download_task_action':
      fields.push(
        ['entity.task', identity(p.name, p.task_id)],
        [planFieldLabels.downloader_id, identity(p.downloader_name, p.downloader_id)],
        ['field.state', p.state],
        [planFieldLabels.transfer_lib_id, identity(p.transfer_lib_name, p.transfer_lib_id)]
      );
      break;
    case 'review_workflow_schedule':
      fields.push(
        [planFieldLabels.graph_id, identity(p.workflow_name, p.workflow_id)],
        ['entity.job', identity(null, p.job_id)],
        ['field.trigger', schedule.trigger],
        ['assistant.review.cron_utc', schedule.cron_expr],
        ['field.run_date', schedule.run_date],
        ['assistant.review.interval_num', schedule.interval_num],
        ['assistant.review.interval_unit', schedule.interval_unit],
        [planFieldLabels.interval_start, schedule.interval_start],
        [planFieldLabels.interval_end, schedule.interval_end],
        ['assistant.review.next_run', first.local ?? first.estimate],
        ['assistant.review.repeatable', schedule.repeatable],
        ['assistant.review.recoverable', schedule.recoverable],
        ['assistant.review.bootparams', publicParameters(schedule.bootparams)]
      );
      break;
    case 'review_media_rescrape':
      fields.push(
        [planFieldLabels.transfer_lib_id, identity(p.library_name, p.library_id)],
        ['assistant.admin_review.fields.item_id', identity(p.title ?? p.name, p.item_id)],
        [planFieldLabels.graph_id, identity(p.workflow_name, p.workflow_id)],
        ['assistant.review.match', record(p.candidate).title],
        ['assistant.review.destination', record(p.organization).expected]
      );
      break;
    case 'review_library_config':
      fields.push([planFieldLabels.transfer_lib_id, identity(p.library_name, p.library_id)]);
      break;
    case 'review_library_scan':
      fields.push(
        [planFieldLabels.transfer_lib_id, identity(p.library_name, p.library_id)],
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
 * Identify changed credential fields without returning their values.
 *
 * @param before - Previous parameters.
 * @param after - Proposed parameters.
 * @param path - Parent property names used to identify nested fields.
 * @returns Names of masked fields whose contents changed.
 */
function changedCredentials(before: unknown, after: unknown, path: string[] = []): string[] {
  const left = before && typeof before === 'object' ? Object.entries(before) : [];
  const right = after && typeof after === 'object' ? Object.entries(after) : [];
  const oldValues = new Map<string, unknown>(left);
  const newValues = new Map<string, unknown>(right);
  const changes: string[] = [];
  for (const key of new Set([...oldValues.keys(), ...newValues.keys()])) {
    const previous = oldValues.get(key);
    const next = newValues.get(key);
    if (JSON.stringify(previous) === JSON.stringify(next)) continue;
    const names = [...path, key];
    if (sensitiveParameter.test(key)) changes.push(names.join('.'));
    else changes.push(...changedCredentials(previous, next, names));
  }
  return changes;
}

/**
 * Return material before-and-after changes supplied by the reviewed service.
 *
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
        maskedChanges: changedCredentials(entry.before, entry.after),
        before: publicParameters(entry.before),
        after: publicParameters(entry.after)
      };
    }
  }
  if (Array.isArray(preview.proposed_binding_order)) {
    changes.push({ field: 'binding_order', before: preview.binding_order, after: preview.proposed_binding_order });
  }
  return changes;
}

/**
 * Dispatch a browser refresh event for the affected domain view after a committed change.
 *
 * @param target - Domain event kind and optional resource identifiers or deduplication source.
 */
export function refreshDomainTarget(target: DomainRefreshTarget): void {
  switch (target.kind) {
    case 'download_plan_created':
      window.dispatchEvent(new Event('download-plan-created'));
      break;
    case 'download_plan_changed':
      window.dispatchEvent(
        target.source
          ? new CustomEvent('download-plan-changed', { detail: { source: target.source } })
          : new Event('download-plan-changed')
      );
      break;
    case 'download_task_changed':
      window.dispatchEvent(new Event('download-task-changed'));
      break;
    case 'workflow_job_changed':
      window.dispatchEvent(new Event('workflow-job-changed'));
      break;
    case 'media_item_changed':
      window.dispatchEvent(
        new CustomEvent('media-item-changed', { detail: { item_id: target.item_id, library_id: target.library_id } })
      );
      break;
    case 'media_library_changed':
      window.dispatchEvent(new CustomEvent('media-library-changed', { detail: { library_id: target.library_id } }));
      break;
    case 'media_library_scan':
      window.dispatchEvent(new CustomEvent('media-library-scan', { detail: { library_id: target.library_id } }));
      break;
  }
}

/**
 * Refresh the open application view after a committed or dispatched decision.
 *
 * @param receipt - Server response to an explicit confirmation.
 */
export function refreshReviewTarget(receipt: AssistantReview): void {
  switch (receipt.tool) {
    case 'validate_download_plan':
      refreshDomainTarget({ kind: 'download_plan_created' });
      break;
    case 'validate_download_task':
    case 'review_download_task_action':
      refreshDomainTarget({ kind: 'download_task_changed' });
      break;
    case 'review_workflow_schedule':
      refreshDomainTarget({ kind: 'workflow_job_changed' });
      break;
    case 'review_media_rescrape':
      refreshDomainTarget({
        kind: 'media_item_changed',
        item_id: receipt.preview.item_id,
        library_id: receipt.preview.library_id
      });
      break;
    case 'review_library_scan':
      refreshDomainTarget({ kind: 'media_library_scan', library_id: receipt.preview.library_id });
      break;
    case 'review_library_config':
      refreshDomainTarget({ kind: 'media_library_changed', library_id: receipt.preview.library_id });
      break;
  }
}
