import { browser } from '$app/env';
import { goto } from '$app/navigation';
import { page } from '$app/state';
import type { DataView } from '$lib/components';
import type { ScrollPosition, Signpost, User } from '$lib/types';
import type { IconifyIcon } from 'iconify-icon';
import { compactRecord } from '$lib/utils/object';
import { normalizePathname } from '$lib/utils/url';
import { tick, untrack } from 'svelte';

/**
 * A reactive value read and replaced through its `current` property.
 */
export type Shared<T> = { current: T };

// define all the shared states here
export const user = shared<User | null>(null);
export const token = persisted<string>('token');
export const freeze = shared<boolean>(false);
export const signposts = sharedRaw<Signpost[]>([]);
// icons of the current page's menu route and top-level section, shown by empty placeholders
export const menuIcon = sharedRaw<IconifyIcon | null>(null);
export const navIcon = sharedRaw<IconifyIcon | null>(null);
export const positions = sharedRaw<Record<string, ScrollPosition>>({});
export const urlparams = persisted<Record<string, string>>('urlparams');
export const histories = persisted<Record<string, string>>('histories');
export const subroutes = persisted<Record<string, string>>('subroutes');

/**
 * Create a shared state whose nested properties are reactive as well.
 *
 * @param value - Initial value.
 * @returns A reactive value that can be replaced, mutated, or bound through `current`.
 */
function shared<T>(value: T): Shared<T> {
  let state = $state(value);
  return {
    get current() {
      return state;
    },
    set current(newValue: T) {
      state = newValue;
    }
  };
}

/**
 * Create a shared state that only notifies readers when its value is replaced.
 *
 * The value is kept as is instead of being proxied, so mutating it in place does not notify readers.
 *
 * @param value - Initial value.
 * @returns A reactive value that can be replaced through `current`.
 */
function sharedRaw<T>(value: T): Shared<T> {
  let state = $state.raw(value);
  return {
    get current() {
      return state;
    },
    set current(newValue: T) {
      state = newValue;
    }
  };
}

/**
 * Create a shared state that persists to localStorage.
 *
 * Nested changes are persisted as well, and changes made by other windows update the state.
 *
 * @param key - LocalStorage key.
 * @param value - Initial value used when nothing is stored.
 * @returns A reactive value that can be replaced, mutated, or bound through `current`.
 */
export function persisted<T>(key: string, value: T | null = null): Shared<T | null> {
  const initial = browser && localStorage.getItem(key);
  let state = $state<T | null>(initial ? JSON.parse(initial) : value);

  if (browser) {
    // update localStorage when the state or any of its nested properties changes
    $effect.root(() => {
      $effect(() => {
        const snapshot = $state.snapshot(state);
        if (snapshot !== null && snapshot !== undefined) {
          localStorage.setItem(key, JSON.stringify(snapshot));
        } else {
          localStorage.removeItem(key);
        }
      });
    });

    // update the state when localStorage changes
    // only triggered when a window other than itself makes the changes
    window.addEventListener('storage', (event) => {
      if (event.key === key) {
        state = event.newValue ? JSON.parse(event.newValue) : null;
      }
    });
  }

  return {
    get current() {
      return state;
    },
    set current(newValue: T | null) {
      state = newValue;
    }
  };
}

/**
 * Call a function with the latest value whenever the shared states read by a getter change.
 *
 * This lets plain modules react to shared states. The callback first runs once the effect starts, so callers that only
 * handle changes compare the value with the one they saw last. It then runs once after each batch of changes, and the
 * states it reads itself are not tracked.
 *
 * @param getter - Function reading the states to watch.
 * @param callback - Function receiving the value returned by `getter`.
 * @returns A function that stops watching.
 */
export function watch<T>(getter: () => T, callback: (value: T) => void): () => void {
  return $effect.root(() => {
    $effect(() => {
      const value = getter();
      untrack(() => callback(value));
    });
  });
}

/**
 * Navigate back to the previous page.
 */
export function historyBack() {
  const pathname = normalizePathname(page.url.pathname);
  const _histories = untrack(() => ({ ...(histories.current ?? {}) }));
  const history = _histories[pathname];
  if (history) {
    goto(history);
    delete _histories[pathname];
    histories.current = compactRecord(_histories);
  } else {
    window.history.back();
  }
}

/**
 * Navigate to the last visited subroute.
 *
 * @param defaultRoute - Default route to navigate to if no match is found.
 */
export function restoreRoute(defaultRoute?: string) {
  const pathname = normalizePathname(page.url.pathname);
  const subroute = untrack(() => subroutes.current?.[pathname]) ?? defaultRoute;
  if (subroute) {
    goto(subroute, { replaceState: true });
  }
}

/**
 * Capture the scroll position of the current page before navigation.
 *
 * @param from - Source navigation target.
 * @param to - Destination navigation target.
 * @param view - Data view instance to capture scroll position from.
 * @param standalone - Whether the app is in standalone display mode.
 */
export function captureScrollPosition(
  from: { url: URL } | null | undefined,
  to: { url: URL } | null | undefined,
  view: { scrollPosition: () => ScrollPosition } | null | undefined,
  standalone: boolean = false
) {
  const fromUrl = from?.url;
  const toUrl = to?.url;
  if (fromUrl && toUrl && view) {
    const fromPath = normalizePathname(fromUrl.pathname);
    const toPath = normalizePathname(toUrl.pathname);
    if (fromPath === toPath) {
      return;
    }
    const position = standalone ? view.scrollPosition() : { left: window.scrollX, top: window.scrollY };
    const _positions = untrack(() => positions.current);
    positions.current = { ..._positions, [fromPath]: position };
  }
}

/**
 * Restore the scroll position for the current page.
 *
 * @param target - Target window or data view to scroll.
 * @param toTop - Whether to scroll to the top instead of restoring the position.
 */
export function restorePosition<T>(target: Window | DataView<T> | null | undefined, toTop?: boolean) {
  if (!target) {
    return;
  }
  tick().then(() => {
    if (toTop) {
      target.scrollTo({ top: 0, behavior: 'smooth' });
    } else {
      const pathname = normalizePathname(page.url.pathname);
      const _positions = positions.current;
      const position = _positions[pathname];
      if (position) {
        target.scrollTo(position);
        delete _positions[pathname];
      }
    }
  });
}
