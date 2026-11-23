<script lang="ts">
  import ReviewCardShell from './ReviewCardShell.svelte';
  import { createReviewCard } from '$lib/assistant/review-card.svelte';
  import { formatReviewValue, refreshDomainTarget } from '$lib/assistant/reviews';
  import { assistant } from '$lib/assistant/session';
  import { _, dateTime } from '$lib/i18n';

  type Review = {
    action: string;
    status: string;
    preview: Record<string, unknown>;
    result: Record<string, unknown> | null;
    digest: string;
    expires_at: string;
  };
  const { id }: { id: string } = $props();
  const card = createReviewCard<Review>({
    path: () => `assistant/operations/${id}`,
    decision: (receipt, approve) => ({ digest: receipt.digest, approve }),
    events: ['assistant-operation-changed'],
    received(data, _previous, approve) {
      if (approve === undefined) return;
      card.notify('assistant-operation-changed');
      if (data.status !== 'applied' && data.status !== 'partial') return;
      const detail = { ...data.preview, ...data.result };
      if (data.action === 'create_library' || data.action === 'delete_library') {
        refreshDomainTarget({ kind: 'media_library_changed', library_id: detail.library_id });
      } else if (data.action === 'edit_metadata') {
        refreshDomainTarget({ kind: 'media_item_changed', item_id: detail.item_id, library_id: detail.library_id });
      }
    }
  });
  const review = $derived(card.receipt);
  const busy = $derived(card.busy);
  const failed = $derived(card.failed);
  const load = card.load;

  /**
   * Format the bounded identifiers and scalar values returned in a review.
   *
   * @param value - Human-readable setting, resource list or decision result.
   * @param key - Field key used to localize known enums.
   * @returns A compact plain-text description.
   */
  function display(value: unknown, key?: string): string {
    return formatReviewValue(value, { t: $_, key, emptyText: '—', structuredObjects: true });
  }
</script>

<ReviewCardShell
  {busy}
  {failed}
  loaded={!!review}
  sectionClass="space-y-3 rounded-box border border-base-content/15 p-4 text-sm"
  ariaLabel={$_('assistant.admin_review.title')}
  status={review ? $_(`assistant.admin_review.states.${review.status}`) : ''}
>
  {#snippet header()}
    <h3 class="font-semibold">
      {review ? $_(`assistant.admin_review.actions.${review.action}`) : $_('assistant.admin_review.title')}
    </h3>
  {/snippet}
  {#if review}
    <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
      {#each Object.entries({ ...review.preview, ...review.result }) as [key, value] (key)}
        {#if !['revision', 'source_digest', 'valid', 'issues'].includes(key) && (key !== 'additional' || Object.keys(value as object).length > 0)}
          <dt>{$_(`assistant.admin_review.fields.${key}`, { default: key })}</dt>
          <dd class="break-words">{display(value, key)}</dd>
        {/if}
      {/each}
      {#if review.status === 'pending'}
        <dt>{$_('assistant.replacement.expires')}</dt>
        <dd>{$dateTime(review.expires_at)}</dd>
      {/if}
    </dl>
    {#if review.action === 'publish_workflow' && typeof review.preview.workflow_id === 'number'}
      <a class="link text-xs link-primary" href={`/settings/workflows/graphs/${review.preview.workflow_id}`}
        >{$_('assistant.admin_review.open_draft')}</a
      >
    {/if}
  {/if}
  {#snippet actions()}
    {#if failed || review?.status === 'executing' || review?.status === 'uncertain' || review?.status === 'partial'}
      <button class="btn btn-ghost btn-sm" type="button" disabled={busy || $assistant.busy} onclick={() => load()}
        >{$_('assistant.replacement.refresh')}</button
      >
    {:else if review?.status === 'pending' || review?.status === 'failed'}
      <button class="btn btn-ghost btn-sm" type="button" disabled={busy || $assistant.busy} onclick={() => load(false)}
        >{$_('assistant.admin_review.cancel')}</button
      >
      <button
        class="btn btn-sm"
        class:btn-error={review.action === 'delete_library'}
        class:btn-primary={review.action !== 'delete_library'}
        type="button"
        disabled={busy || $assistant.busy}
        onclick={() => load(true)}>{$_(`assistant.admin_review.approve.${review.action}`)}</button
      >
    {/if}
  {/snippet}
</ReviewCardShell>
