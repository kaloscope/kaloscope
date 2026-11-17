import { api } from '$lib/api';
import type { MediaBatch, MediaBatchOutcome, Resp } from '$lib/types';
import { isHTTPError } from 'ky';

const watchers = new Map<number, AbortController>();

/**
 * Notify open library lists that their saved media may have changed.
 *
 * @param libraryId - Library whose current items should be reloaded.
 */
export function notifyMediaItemsChanged(libraryId: number): void {
  window.dispatchEvent(new CustomEvent('media-item-changed', { detail: { library_id: libraryId } }));
}

/**
 * Refresh the affected library after a batch finishes, even after its dialog closes.
 *
 * Share one watcher per batch and retry temporary read failures. Unavailable
 * batches stop polling; account changes cancel every pending read and timer.
 *
 * @param batchId - Applied or running batch whose terminal outcome is awaited.
 */
export function watchMediaBatch(batchId: number): void {
  if (watchers.has(batchId)) return;

  const controller = new AbortController();
  watchers.set(batchId, controller);

  let timer: ReturnType<typeof setTimeout> | undefined;
  controller.signal.addEventListener('abort', () => clearTimeout(timer), { once: true });

  /**
   * Read the authoritative state and notify once the batch reaches an outcome.
   *
   * @returns A promise resolving after this check has finished or scheduled its retry.
   */
  async function check(): Promise<void> {
    try {
      const { data } = await api
        .get(`media/batch/${batchId}`, {
          signal: controller.signal,
          retry: 0,
          context: { silentErrors: true }
        })
        .json<Resp<MediaBatch>>();

      if (controller.signal.aborted) return;

      if (data.state !== 'running') {
        watchers.delete(batchId);
        if (data.state === 'finished') notifyMediaItemsChanged(data.library_id);
        return;
      }
    } catch (error) {
      if (controller.signal.aborted) return;

      if (isHTTPError(error) && [401, 403, 404].includes(error.response.status)) {
        watchers.delete(batchId);
        return;
      }
    }

    timer = setTimeout(() => void check(), 2000);
  }

  void check();
}

/**
 * Stop batch status checks before replacing the current account or session.
 */
export function resetMediaBatchWatchers(): void {
  for (const controller of watchers.values()) controller.abort();
  watchers.clear();
}

/**
 * Join the reasons a batch left units alone, such as "download in progress (1) and being ingested (2)".
 *
 * @param reasons - Unit counts by reason code; reasons without units are left out.
 * @param translate - Message formatter for reason labels and counts.
 * @param formatNumber - Number formatter for the counts.
 * @param language - Locale that joins the list and lower-cases the labels.
 * @param counted - Whether each reason shows its unit count. Defaults to `true`.
 * @returns The joined reasons, or an empty string when none has units.
 */
export function describeBatchReasons(
  reasons: Record<string, number>,
  translate: (id: string, values?: (string | number)[] | { default?: string }) => string,
  formatNumber: (value: number) => string,
  language?: string | null,
  counted: boolean = true
): string {
  const parts = Object.entries(reasons)
    .filter(([, count]) => count > 0)
    .map(([reason, count]) => {
      const label = translate(`media.batch.reason.${reason}`, { default: reason });
      // the labels stand alone elsewhere; inside a sentence they start in lower case
      const inline = label.charAt(0).toLocaleLowerCase(language ?? undefined) + label.slice(1);
      return counted ? translate('media.batch.reason_count', [inline, formatNumber(count)]) : inline;
    });
  return new Intl.ListFormat(language ?? undefined, { type: 'conjunction' }).format(parts);
}

type Translate = (id: string, values?: string | (string | number)[] | { default?: string }) => string;

/**
 * Review deleting library items; nothing changes until the plan is confirmed.
 *
 * @param libraryId - Library holding the items.
 * @param itemIds - Items to delete.
 * @param deleteFiles - Whether their files go too, rather than only leaving the library. Defaults to `false`.
 * @returns The review, carrying a `batch_id` when there is a plan to confirm.
 */
export async function reviewItemDeletion(
  libraryId: number,
  itemIds: number[],
  deleteFiles: boolean = false
): Promise<MediaBatchOutcome> {
  const { data } = await api
    .post('media/batch/review', {
      json: { library_id: libraryId, operation: 'delete', item_ids: itemIds, delete_files: deleteFiles }
    })
    .json<Resp<MediaBatchOutcome>>();
  return data;
}

/**
 * Explain why a deletion review produced no plan.
 *
 * @param outcome - Review that returned no batch.
 * @param translate - Message formatter.
 * @param formatNumber - Number formatter for the counts.
 * @param language - Locale that joins and lower-cases the reasons.
 * @returns The translated reason, naming each blocking cause with its count when there is one.
 */
export function describeEmptyReview(
  outcome: MediaBatchOutcome,
  translate: Translate,
  formatNumber: (value: number) => string,
  language?: string | null
): string {
  const reasons = describeBatchReasons(outcome.blocked?.reasons ?? {}, translate, formatNumber, language);
  if (reasons) {
    return translate('alert.media_batch_blocked', reasons);
  }
  // for a deletion, an `item_ids` issue means a selected item left this library
  if (outcome.issues?.some((issue) => issue.path === 'item_ids')) {
    return translate('alert.media_batch_mismatch');
  }
  return translate('alert.media_batch_empty');
}
