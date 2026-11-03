import '@unplugin-pwa/core/info';
import '@unplugin-pwa/core/pwa-assets';
import '@unplugin-pwa/core/svelte';

// see https://svelte.dev/docs/kit/types#app.d.ts
// for information about these interfaces
declare global {
  namespace App {
    // interface Error {}
    // interface Locals {}
    // interface PageData {}
    interface PageState {
      createScheduledGraph?: boolean;
      resumeReading?: boolean;
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
