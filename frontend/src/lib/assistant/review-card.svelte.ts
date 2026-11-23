import { api } from '$lib/api';
import { assistant } from '$lib/assistant/session';
import { user } from '$lib/stores.svelte';
import type { Resp } from '$lib/types';
import { untrack } from 'svelte';
import { get } from 'svelte/store';

/**
 * Share authenticated loading, explicit decisions and invalidation across approval cards.
 * @param options - Endpoint, frozen decision payload and domain-specific receipt handling.
 * @returns Reactive request state and a method to read or decide the current review.
 */
export function createReviewCard<T>(options: {
  path: () => string;
  decision: (receipt: T, approve: boolean) => Record<string, unknown>;
  received?: (data: T, previous: T | null, approve?: boolean) => void;
  events: string[];
}) {
  let receipt = $state.raw<T | null>(null);
  let busy = $state(false);
  let failed = $state(false);
  let controller: AbortController | null = null;
  let refreshPending = false;
  const source = Symbol();

  /**
   * Notify sibling views after a decision while preserving this card's returned receipt.
   * @param name - Review or domain invalidation event.
   */
  function notify(name: string): void {
    window.dispatchEvent(new CustomEvent(name, { detail: { source } }));
  }

  /**
   * Read the canonical receipt or submit an explicit decision once.
   * @param approve - Explicit confirmation or cancellation, or absent to refresh.
   */
  async function load(approve?: boolean): Promise<void> {
    if (!controller || controller.signal.aborted || user.current?.role !== 'admin') return;
    if (busy) {
      if (approve === undefined) refreshPending = true;
      return;
    }
    if (approve !== undefined && (get(assistant).busy || !receipt)) return;
    const owner = user.current.id;
    const { signal } = controller;
    const path = options.path();
    const previous = receipt;
    const config = { signal, retry: 0, context: { silentErrors: true } };
    busy = true;
    failed = false;
    try {
      const request =
        approve === undefined
          ? api.get(path, config)
          : api.post(path + '/decision', {
              ...config,
              json: options.decision(previous!, approve)
            });
      const { data } = await request.json<Resp<T>>();
      if (signal.aborted || user.current?.id !== owner || user.current.role !== 'admin') return;
      options.received?.(data, previous, approve);
      receipt = data;
    } catch {
      if (!signal.aborted) failed = true;
    } finally {
      if (!signal.aborted) {
        busy = false;
        if (refreshPending) {
          refreshPending = false;
          void load();
        }
      }
    }
  }

  $effect(() => {
    const owner = user.current?.id;
    const admin = user.current?.role === 'admin';
    const path = options.path();
    return untrack(() => {
      receipt = null;
      busy = false;
      failed = false;
      refreshPending = false;
      if (!owner || !admin || !path) return;
      const current = new AbortController();
      controller = current;
      const refresh = (event?: Event) => {
        if (!(event instanceof CustomEvent) || event.detail?.source !== source) void load();
      };
      for (const name of options.events) window.addEventListener(name, refresh, { signal: current.signal });
      refresh();
      return () => current.abort();
    });
  });
  return {
    get receipt() {
      return receipt;
    },
    get busy() {
      return busy;
    },
    get failed() {
      return failed;
    },
    load,
    notify,
    source
  };
}
