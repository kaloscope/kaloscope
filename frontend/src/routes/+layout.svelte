<script lang="ts">
  import { onNavigate } from '$app/navigation';
  import { Alerts, DownloadPrompt, Messages } from '$lib/components';
  import { user } from '$lib/stores';
  import { sniffer, throttle } from '$lib/utils';
  import type { Snippet } from 'svelte';
  import { pwaAssetsHead } from 'virtual:pwa-assets/head';
  import { pwaInfo } from 'virtual:pwa-info';
  // import web component
  import 'cally';
  import 'iconify-icon';
  // import font sources
  import '@fontsource-variable/noto-sans';
  import '@fontsource-variable/noto-sans-sc';
  import '@fontsource/audiowide';
  import '@fontsource/zcool-qingke-huangyou';
  // import style sources
  import '@xyflow/svelte/dist/style.css';
  import 'tippy.js/animations/scale.css';
  import 'tippy.js/dist/tippy.css';
  import 'xgplayer/dist/index.min.css';
  import 'xgplayer/es/plugins/danmu/index.css';
  import 'xgplayer/es/plugins/track/index.css';
  import '../app.css';

  let { children }: { children: Snippet } = $props();
  // the generated web app manifest
  let webManifest = $derived(pwaInfo ? pwaInfo.webManifest.linkTag : '');
  // throttled vibrate function
  const vibrate = throttle(() => navigator.vibrate?.(20), 500);

  onNavigate((navigation) => {
    // vibrate if user preference is enabled
    $user?.preferences?.vibration && vibrate();
    // start view transition if supported and not on Safari
    if (document.startViewTransition && !sniffer.isSafari()) {
      return new Promise((resolve) => {
        document.startViewTransition(async () => {
          resolve();
          await navigation.complete;
        });
      });
    }
  });
</script>

<svelte:head>
  {#if pwaAssetsHead.themeColor}
    <meta name="theme-color" content={pwaAssetsHead.themeColor.content} />
  {/if}
  {#each pwaAssetsHead.links as link (link.href)}
    <link {...link} />
  {/each}
  <!-- eslint-disable-next-line svelte/no-at-html-tags -->
  {@html webManifest}
</svelte:head>

{@render children()}
<Alerts />
<Messages />
<DownloadPrompt />
{#await import('$lib/components/common/feature/ReloadPrompt.svelte') then { default: ReloadPrompt }}
  <ReloadPrompt />
{/await}
