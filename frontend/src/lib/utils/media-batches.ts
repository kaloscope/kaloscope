import { api } from '$lib/api';
import type { MediaBatch, Resp } from '$lib/types';
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
