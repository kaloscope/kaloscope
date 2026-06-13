import { browser } from '$app/environment';
import { goto } from '$app/navigation';
import { page } from '$app/state';
import type { DataView } from '$lib/components';
import type { ScrollPosition, User } from '$lib/types';
import { normalizePathname } from '$lib/utils';
import { tick } from 'svelte';
import { get, writable, type Writable } from 'svelte/store';

// define all the stores here
export const user = writable<User | null>(null);
export const token = persisted<string>('token');
export const freeze = writable<boolean>(false);
export const signposts = writable<string[]>([]);
export const urlparams = writable<Record<string, string>>({});
export const positions = writable<Record<string, ScrollPosition>>({});
export const histories = persisted<Record<string, string>>('histories');
export const subroutes = persisted<Record<string, string>>('subroutes');

/**
 * Create a writable store that persists to localStorage.
 *
 * @param key - The localStorage key.
 * @param value - The initial value.
 * @returns A writable store.
 */
export function persisted<T>(key: string, value?: T | null): Writable<T | null> {
  const initial = browser && localStorage.getItem(key);
  const store = writable<T | null>(initial ? JSON.parse(initial) : value);

  if (browser) {
    // update localStorage when the store changes
    store.subscribe((newValue) => {
      if (newValue !== null && newValue !== undefined) {
        localStorage.setItem(key, JSON.stringify(newValue));
      } else {
        localStorage.removeItem(key);
      }
    });

    // update the store when localStorage changes
    // only triggered when a window other than itself makes the changes
    window.addEventListener('storage', (event) => {
      if (event.key === key) {
        store.set(event.newValue ? JSON.parse(event.newValue) : null);
      }
    });
  }

  return store;
}

/**
 * Create a writable store that listens to media query changes.
 *
 * @param query - The media query string.
 * @param immediate - Whether the initial value should match the query.
 * @returns A writable store.
 */
export function mediaQuery(query: string, immediate: boolean = true): Writable<boolean | null> {
  // create a media query list
  const mediaQueryList = window.matchMedia(query);

  const initial = immediate ? mediaQueryList.matches : null;
  const store = writable<boolean | null>(initial, (set) => {
    const onchange = () => set(mediaQueryList.matches);

    try {
      mediaQueryList.addEventListener('change', onchange);
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
    } catch (error) {
      mediaQueryList.addListener(onchange);
    }

    return () => {
      try {
        mediaQueryList.removeEventListener('change', onchange);
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
      } catch (error) {
        mediaQueryList.removeListener(onchange);
      }
    };
  });

  return store;
}

/**
 * Navigate back to the previous page.
 */
export function historyBack() {
  const pathname = normalizePathname(page.url.pathname);
  const _histories = get(histories);
  const history = _histories?.[pathname];
  if (history) {
    goto(history);
    delete _histories[pathname];
    histories.set({ ..._histories });
  } else {
    window.history.back();
  }
}

/**
 * Navigate to the last visited subroute.
 *
 * @param defaultRoute - The default route to navigate to if no match is found.
 */
export function restoreRoute(defaultRoute?: string) {
  const pathname = normalizePathname(page.url.pathname);
  const _subroutes = get(subroutes);
  const subroute = _subroutes?.[pathname] ?? defaultRoute;
  if (subroute) {
    goto(subroute, { replaceState: true });
  }
}

/**
 * Capture the scroll position of the current page before navigation.
 *
 * @param from - The source navigation target.
 * @param to - The destination navigation target.
 * @param view - The data view instance to capture scroll position from.
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
    const _positions = get(positions);
    positions.set({ ..._positions, [fromPath]: position });
  }
}

/**
 * Restore the scroll position for the current page.
 *
 * @param target - The target window or data view to scroll.
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
      const _positions = get(positions);
      const position = _positions[pathname];
      if (position) {
        target.scrollTo(position);
        delete _positions[pathname];
      }
    }
  });
}
