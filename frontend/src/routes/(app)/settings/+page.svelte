<script lang="ts">
  import { restoreRoute } from '$lib/stores.svelte';
  import { onMount } from 'svelte';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();
  onMount(() => {
    restoreRoute(data.menus[0]?.routes[0]?.path);
  });
</script>
