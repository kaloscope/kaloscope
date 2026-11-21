<script lang="ts">
  import { page } from '$app/state';
  import AdminReviewCard from '$lib/components/business/assistant/AdminReviewCard.svelte';
  import { user } from '$lib/stores.svelte';
  import { _ } from '$lib/i18n';

  const id = $derived(page.url.searchParams.get('operation'));
</script>

<div class="mx-auto my-4 max-w-xl px-4">
  {#if !user.current}<span role="status" class="loading loading-spinner" aria-label={$_('media.loading')}></span>
  {:else if user.current.role !== 'admin'}<p role="status">{$_('alert.forbidden')}</p>
  {:else if id && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(id)}
    {#key id}<AdminReviewCard {id} />{/key}
  {:else}
    <p role="status">{$_('assistant.admin_review.missing')}</p>
  {/if}
</div>
