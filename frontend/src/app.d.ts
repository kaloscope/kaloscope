import 'vite-plugin-pwa/info';
import 'vite-plugin-pwa/pwa-assets';
import 'vite-plugin-pwa/svelte';

// See https://svelte.dev/docs/kit/types#app.d.ts
// for information about these interfaces
declare global {
  namespace App {
    // interface Error {}
    // interface Locals {}
    // interface PageData {}
    interface PageState {
      createScheduledGraph?: boolean;
    }
    // interface Platform {}
  }

  // https://svelte.dev/blog/view-transitions
  interface ViewTransition {
    updateCallbackDone: Promise<void>;
    ready: Promise<void>;
    finished: Promise<void>;
    skipTransition: () => void;
  }

  interface Document {
    startViewTransition(updateCallback: () => Promise<void>): ViewTransition;
  }
}

export {};
