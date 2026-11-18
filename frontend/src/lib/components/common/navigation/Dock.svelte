<script lang="ts" module>
  import type { Navigation } from '$lib/types';

  export type DockProps = {
    /** List of navigation items. */
    navs: Navigation[];
    /** Whether to show the shadow. */
    shadow?: boolean;
  };
</script>

<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { _ } from '$lib/i18n';
  import { subroutes } from '$lib/stores';

  let { navs, shadow = true }: DockProps = $props();
  // box shadow class
  let shadowClass = $derived(shadow ? 'shadow-[0_-1px_2px_0_rgba(0,0,0,0.05)]' : '');
</script>

<div class="dock layer-2 h-(--ks-dock-h) border-t bg-blur-90 py-0 sm:hidden {shadowClass}">
  {#each navs.filter((nav) => nav.mobile) as nav (nav.path)}
    {@const active = page.url.pathname.startsWith(nav.path)}
    <a
      href={nav.path}
      class="mt-4 mb-0 justify-start duration-300 {active ? 'pointer-events-none text-surface' : ''}"
      onclick={(event) => {
        if (active) {
          event.preventDefault();
          return;
        }
        // navigate to subroute if exists
        const subroute = $subroutes?.[nav.path];
        if (subroute) {
          event.preventDefault();
          goto(subroute, { replaceState: true });
        }
      }}
    >
      <iconify-icon icon={active ? nav.iconFilled : nav.icon} width="1.25rem" class:opacity-70={!active}></iconify-icon>
      <span class="dock-label font-title" class:opacity-70={!active}>
        {$_(nav.title)}
      </span>
    </a>
  {/each}
</div>
