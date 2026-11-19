<script lang="ts" module>
  import type { Snippet } from 'svelte';

  export type DrawerProps = {
    /** The drawer content snippet. */
    children: Snippet;
    /** The drawer side snippet. */
    side: Snippet;
  };
</script>

<script lang="ts">
  import { freeze } from '$lib/stores.svelte';
  import { MediaQuery } from 'svelte/reactivity';

  let { children, side }: DrawerProps = $props();
  // the drawer toggle state
  let checked = $state(false);
  // the screen width media query
  const drawerOpen = new MediaQuery('(min-width: 40rem)');

  $effect(() => {
    // set the freeze state based on the drawer toggle state
    if (drawerOpen.current) {
      freeze.current = false;
    } else {
      freeze.current = checked;
    }
  });
</script>

<div class="drawer sm:drawer-open">
  <input id="drawer" type="checkbox" class="drawer-toggle" bind:checked onchange={() => (freeze.current = checked)} />
  <div class="drawer-content flex items-start justify-center">
    {@render children()}
  </div>
  <div class="drawer-side layer-1 overscroll-none sm:top-(--ks-navbar-h) sm:layer-4 sm:max-h-(--ks-lvh)">
    <label for="drawer" class="drawer-overlay"></label>
    {@render side()}
  </div>
</div>
