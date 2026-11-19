<script lang="ts">
  import { goto } from '$app/navigation';
  import { user } from '$lib/stores.svelte';

  goto(user.current?.preferences?.homepage ?? '/dashboard');
</script>
