/**
 * Return a record only when it still has keys.
 *
 * @param record - Record to compact.
 * @returns The original record, or `null` when empty.
 */
export function compactRecord<T>(record: Record<string, T>): Record<string, T> | null {
  return Object.keys(record).length ? record : null;
}

/**
 * Swap an item with its neighbor, moving it one place up or down in place.
 *
 * @param list - List to reorder.
 * @param index - Index of the item to move.
 * @param offset - `-1` to move the item up, or `1` to move it down.
 * @returns Whether the item moved; an item already at that end of the list stays put.
 */
export function moveItem<T>(list: T[], index: number, offset: -1 | 1): boolean {
  const target = index + offset;
  if (index < 0 || index >= list.length || target < 0 || target >= list.length) {
    return false;
  }
  [list[index], list[target]] = [list[target], list[index]];
  return true;
}
