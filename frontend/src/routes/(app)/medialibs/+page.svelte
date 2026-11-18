<script lang="ts">
  import { MediaLibEmpty } from '$lib/components';
  import { restoreRoute } from '$lib/stores';
  import { onMount } from 'svelte';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();
  onMount(() => {
    restoreRoute(data.menus[0]?.routes[0]?.path);
  });
</script>

{#if data.libs.length === 0}
  <MediaLibEmpty />
{/if}
