import { token } from '$lib/stores';
import type { FlowGraph } from '$lib/types';

export type GraphUpdate = Partial<FlowGraph> & Pick<FlowGraph, 'id'>;

const pendingWrites = new Map<number, Promise<void>>();
let updateList: ((updated: GraphUpdate) => void) | undefined;
let session = new AbortController();

token.subscribe(() => {
  session.abort();
  session = new AbortController();
  pendingWrites.clear();
});

/**
 * Connect graph writes to the current list and its retained navigation state.
 *
 * @param update - Callback that updates rows without querying or reordering them.
 */
export function setGraphListUpdater(update: (updated: GraphUpdate) => void): void {
  updateList = update;
}

/**
 * Serialize writes to the same graph and retain their successful list updates.
 *
 * @param id - Flow graph ID.
 * @param operation - Request factory receiving a signal canceled when the login session changes.
 * @returns The request result, preserving failures for the caller.
 */
export function trackGraphWrite<T extends GraphUpdate | void>(
  id: number,
  operation: (signal: AbortSignal) => Promise<T>
): Promise<T> {
  // retain the originating list even if navigation creates a different list
  const update = updateList;
  const { signal } = session;
  const request = (pendingWrites.get(id) ?? Promise.resolve())
    .then(() => {
      signal.throwIfAborted();
      return operation(signal);
    })
    .then((data) => {
      signal.throwIfAborted();
      if (data) {
        update?.(data);
      }
      return data;
    });
  const settled = request.then(
    () => {},
    () => {}
  );
  pendingWrites.set(id, settled);
  void settled.then(() => {
    if (pendingWrites.get(id) === settled) {
      pendingWrites.delete(id);
    }
  });
  return request;
}

/**
 * Wait for graph writes to settle before reading graph data.
 *
 * @param id - Flow graph ID, or all graphs when omitted.
 * @returns A promise that resolves after matching writes succeed or fail.
 */
export async function waitForGraphWrites(id?: number): Promise<void> {
  while (id === undefined ? pendingWrites.size > 0 : pendingWrites.has(id)) {
    await Promise.all(id === undefined ? pendingWrites.values() : [pendingWrites.get(id)]);
  }
}
