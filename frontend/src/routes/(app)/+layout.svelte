<script lang="ts">
  import { afterNavigate, beforeNavigate } from '$app/navigation';
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import { Dock, Navbar } from '$lib/components';
  import { headTitle } from '$lib/i18n';
  import { freeze, histories, positions, subroutes, urlparams, user } from '$lib/stores';
  import type { Resp, User } from '$lib/types';
  import { normalizePathname } from '$lib/utils';
  import { SvelteFlowProvider } from '@xyflow/svelte';
  import { untrack, type Snippet } from 'svelte';
  import { Spring } from 'svelte/motion';
  import type { LayoutData } from './$types';

  let { data, children }: { data: LayoutData; children: Snippet } = $props();

  // the main route of the current page
  let mainRoute = $derived(data.navs.find((nav) => page.url.pathname.startsWith(nav.path)));

  // the back button to go back to the previous page
  let historyBack: boolean = $state(false);

  // the states for the navbar component
  let navbarHidden: boolean = $state(false);
  let navbarShadow: boolean = $state(false);

  // the scroll y position of the window
  let scrollY: number = $state(0);

  // the custom pull-to-refresh feature
  let pullToRefresh: boolean = $state(false);
  let pullStartY: number = $state(0);
  let refreshKey: number = $state(0);
  let refreshing: boolean = $state(false);
  const refresherH = new Spring(0, { stiffness: 0.1, damping: 1 });
  const overscrollH = new Spring(0, { stiffness: 0.1, damping: 1 });

  /**
   * Pull down to refresh the page.
   *
   * @param pixels - The number of pixels to pull down.
   */
  function pullDown(pixels: number) {
    if (scrollY === 0) {
      refresherH.target = pixels;
      if (pixels > 300) {
        refreshing = true;
        refresherH.target = 50;
        setTimeout(() => {
          refreshKey = new Date().getTime();
          refresherH.target = 0;
          refreshing = false;
        }, 800);
      }
    }
  }

  /**
   * Pull up to show the overscroll effect.
   *
   * @param pixels - The number of pixels to pull up.
   */
  function pullUp(pixels: number) {
    overscrollH.target = Math.min(pixels, 100);
  }

  beforeNavigate(({ from, to }) => {
    const fromUrl = from?.url;
    const toUrl = to?.url;
    if (!fromUrl || !toUrl) {
      return;
    }
    // normalize pathnames to ensure consistent keys regardless of trailing slashes
    const fromPath = normalizePathname(fromUrl.pathname);
    const toPath = normalizePathname(toUrl.pathname);
    if (fromPath === toPath) {
      return;
    }
    // capture the scroll position
    const position = { left: window.scrollX, top: window.scrollY };
    positions.set({ ...$positions, [fromPath]: position });
    // capture the URL parameters
    if (fromUrl.search) {
      urlparams.set({ ...$urlparams, [fromPath]: fromUrl.search });
    }
    // restore the URL parameters for the next page
    // eslint-disable-next-line svelte/prefer-svelte-reactivity
    const toSearchParams = new URLSearchParams(toUrl.search);
    if (toSearchParams.get('restore') === 'false') {
      toSearchParams.delete('restore');
      toUrl.search = toSearchParams.toString();
    } else {
      let searchParams = $urlparams[toPath];
      if (searchParams && searchParams !== toUrl.search) {
        toUrl.search = searchParams;
      }
    }
  });

  afterNavigate(({ from }) => {
    // enable certain features based on specific class names
    historyBack = document.querySelector('.history-back') !== null;
    navbarHidden = document.querySelector('.navbar-hidden') !== null;
    navbarShadow = document.querySelector('.navbar-shadow') !== null;
    pullToRefresh = document.querySelector('.pull-to-refresh') !== null;
    // update the subroutes store after navigation
    const mainPath = mainRoute?.path;
    const pageUrl = page.url;
    const pagePath = normalizePathname(pageUrl.pathname);
    if (!historyBack && mainPath && mainPath !== pagePath) {
      subroutes.set({ ...($subroutes ?? {}), [mainPath]: pagePath + pageUrl.search });
    }
    // update the histories store after navigation
    const fromUrl = from?.url;
    const fromPath = fromUrl?.pathname && normalizePathname(fromUrl.pathname);
    if (historyBack && fromPath && fromPath !== pagePath) {
      histories.set({ ...($histories ?? {}), [pagePath]: fromPath + fromUrl.search });
    }
  });

  // refresh the current user data if it's not available
  $effect(() => {
    if (!$user) {
      api
        .get('auth/current')
        .json<Resp<User>>()
        .then(({ data }) => user.set(data));
    }
  });

  // freeze the body scrolling when the $freeze store is true
  let frozenScrollY: number = $state(0);
  $effect(() => {
    const _freeze = $freeze;
    untrack(() => {
      if (_freeze) {
        // disable body scrolling
        frozenScrollY = scrollY;
        document.body.style.top = `-${frozenScrollY}px`;
        document.body.style.width = '100%';
        document.body.style.position = 'fixed';
      } else {
        // enable body scrolling
        document.body.style.top = '';
        document.body.style.width = '';
        document.body.style.position = '';
        window.scrollTo(0, frozenScrollY);
        frozenScrollY = 0;
      }
    });
  });
</script>

<svelte:head>
  <title>{$headTitle(mainRoute?.title ?? '')}</title>
</svelte:head>

<svelte:window bind:scrollY />

<!-- The top navigation bar. -->
<Navbar
  appMode
  navs={data.navs}
  back={historyBack}
  hidden={navbarHidden}
  shadow={navbarShadow || scrollY > 0 || frozenScrollY > 0}
/>

<!-- The loading spinner for the pull-to-refresh feature. -->
<div
  class="max-h-48 justify-center bg-base-125 {refresherH.current ? 'flex' : 'hidden'}"
  style:height={`${refresherH.current | 0}px`}
>
  <span
    class="loading loading-lg loading-spinner"
    style:opacity={refreshing ? '0.5' : (refresherH.current / 600).toFixed(1)}
  >
  </span>
</div>

<!-- The main content of the page. -->
{#key refreshKey}
  <main
    class="min-h-(--ks-svh) bg-base-125 pb-(--ks-dock-h) sm:min-h-(--ks-lvh) sm:pb-0"
    ontouchstart={(event) => {
      pullStartY = event.touches[0].pageY;
    }}
    ontouchmove={(event) => {
      if (pullToRefresh && !refreshing && !$freeze) {
        const diff = event.touches[0].pageY - pullStartY;
        diff > 0 ? pullDown(diff) : pullUp(-diff);
      }
    }}
    ontouchend={() => {
      !refreshing && (refresherH.target = 0);
      overscrollH.target = 0;
    }}
  >
    <!--
    The `SvelteFlowProvider` component wraps its child nodes with a Svelte context
    that makes it possible to access a flow's internal state outside of the `SvelteFlow` component.
    -->
    <SvelteFlowProvider>
      {@render children()}
    </SvelteFlowProvider>
  </main>
{/key}

<!-- The overscroll effect at the bottom of the page. -->
<div
  class="max-h-24 bg-base-125 {overscrollH.current ? 'flex' : 'hidden'}"
  style:height={`${overscrollH.current | 0}px`}
></div>

<!-- The bottom navigation dock. -->
<Dock navs={data.navs} />
