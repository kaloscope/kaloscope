<script lang="ts">
  import { api } from '$lib/api';
  import { Modal } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _, number } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { MediaBatch, MediaBatchEntry, Page, Resp } from '$lib/types';
  import { formatBytes } from '$lib/utils/number';
  import { onDestroy } from 'svelte';

  const PAGE_SIZE = 50;
  const STATUSES = ['planned', 'blocked', 'done', 'skipped', 'failed', 'uncertain'] as const;

  let batch: MediaBatch | null = $state(null);
  let entries: MediaBatchEntry[] = $state([]);
  let total = $state(0);
  let page = $state(1);
  let status: (typeof STATUSES)[number] | '' = $state('');
  let failed = $state(false);
  let timer: ReturnType<typeof setTimeout> | undefined;
  let id = 0;

  // the modal dialog instance
  let modal: Modal;
  const loading = createLoading();
  const pages = $derived(Math.max(1, Math.ceil(total / PAGE_SIZE)));

  /**
   * Open the dialog for one reviewed batch.
   *
   * @param batchId - Batch whose plan and progress are shown.
   */
  export const showModal = (batchId: number) => {
    id = batchId;
    batch = null;
    entries = [];
    page = 1;
    status = '';
    failed = false;
    modal.show();
    void load();
  };

  /**
   * Load the batch header and the current page, and keep refreshing while it runs.
   */
  async function load() {
    clearTimeout(timer);
    const current = id;
    try {
      const searchParams: Record<string, string | number> = { page_num: page, page_size: PAGE_SIZE };
      if (status) {
        searchParams.status = status;
      }
      const [details, list] = await Promise.all([
        api.get(`media/batch/${current}`).json<Resp<MediaBatch>>(),
        api.get(`media/batch/${current}/entries`, { searchParams }).json<Resp<Page<MediaBatchEntry>>>()
      ]);
      if (current !== id) {
        return;
      }
      batch = details.data;
      entries = list.data.items;
      total = list.data.total ?? 0;
      failed = false;
      if (batch.state === 'running') {
        timer = setTimeout(load, 2000);
      }
    } catch {
      failed = true;
    }
  }

  /**
   * Show another page or status and reload.
   *
   * @param next - Page number to show.
   */
  function show(next: number) {
    page = next;
    void load();
  }

  /**
   * Ask the running batch to stop before its next unit.
   */
  function stop() {
    loading.start();
    api
      .post(`media/batch/${id}/cancel`)
      .then(() => load())
      .finally(() => loading.end());
  }

  onDestroy(() => clearTimeout(timer));
</script>

<Modal
  icon={icons.listCheck}
  title={$_('media.batch.title')}
  maxWidth="56rem"
  bind:this={modal}
  onclose={() => clearTimeout(timer)}
>
  {#if failed}
    <p class="py-6 text-sm text-base-content/60">{$_('media.batch.unavailable')}</p>
  {:else if !batch}
    <div class="flex justify-center py-10"><span class="loading loading-md loading-spinner"></span></div>
  {:else}
    <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
      <span class="font-medium">{batch.library_name}</span>
      <span class="badge badge-sm" class:badge-info={batch.state === 'running'}>
        {$_(`media.batch.state.${batch.state}`)}
      </span>
      <span class="text-base-content/60">
        {$_('media.batch.planned', [$number(batch.planned.units), formatBytes(batch.planned.bytes)])}
      </span>
      {#if batch.blocked.units}
        <span class="text-base-content/60">{$_('media.batch.blocked', $number(batch.blocked.units))}</span>
      {/if}
      {#if batch.operation}
        <span class="text-base-content/60">{$_(`media.batch.operation.${batch.operation}`)}</span>
      {/if}
      {#if batch.deletion}
        <span class={batch.deletion === 'permanent' ? 'text-warning' : 'text-base-content/60'}>
          {$_(`media.batch.deletion.${batch.deletion}`)}
        </span>
      {/if}
    </div>
    {#if batch.state !== 'reviewed'}
      <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm" role="status">
        <progress class="progress w-40" value={batch.planned.units - batch.counts.remaining} max={batch.planned.units}
        ></progress>
        <span>
          {$_('media.batch.progress', [
            $number(batch.counts.done),
            $number(batch.counts.skipped + batch.counts.failures + batch.counts.uncertain),
            $number(batch.counts.remaining)
          ])}
        </span>
        {#if batch.state === 'running'}
          <button
            type="button"
            class="btn btn-outline btn-sm"
            disabled={batch.stop_requested || $loading !== null}
            onclick={stop}
          >
            <iconify-icon icon={icons.stopFilled} width="0.875rem" aria-hidden="true"></iconify-icon>
            {$_(batch.stop_requested ? 'media.batch.stopping' : 'media.batch.stop')}
          </button>
        {:else if batch.outcome}
          <span class="text-base-content/60">{$_(`media.batch.outcome.${batch.outcome}`)}</span>
        {/if}
      </div>
    {/if}
    {#if batch.more}
      <p class="mt-2 text-xs text-base-content/60">{$_('media.batch.more')}</p>
    {/if}

    <div class="mt-3 flex items-center gap-2">
      <select
        class="select w-auto select-sm"
        aria-label={$_('media.batch.column.status')}
        bind:value={status}
        onchange={() => show(1)}
      >
        <option value="">{$_('media.batch.all')}</option>
        {#each STATUSES as value (value)}
          <option {value}>{$_(`media.batch.status.${value}`)}</option>
        {/each}
      </select>
      <span class="text-xs text-base-content/60">{$_('data.paginator.total', $number(total))}</span>
    </div>

    <div class="mt-2 max-h-[50vh] overflow-auto rounded-field border border-base-content/10">
      <table class="table table-pin-rows table-sm">
        <thead>
          <tr>
            <th>{$_('media.batch.column.path')}</th>
            <th class="text-right">{$_('media.batch.column.size')}</th>
            <th>{$_('media.batch.column.owner')}</th>
            <th>{$_('media.batch.column.status')}</th>
          </tr>
        </thead>
        <tbody>
          {#each entries as entry (entry.seq)}
            <tr>
              <td class="font-mono text-xs break-all">
                {entry.src}
                {#if entry.dst}
                  <span class="block text-base-content/60">→ {entry.dst}</span>
                {/if}
              </td>
              <td class="text-right text-xs whitespace-nowrap">{formatBytes(entry.size)}</td>
              <td class="text-xs whitespace-nowrap">{$_(`media.batch.owner.${entry.owner}`)}</td>
              <td class="text-xs">
                <span class="whitespace-nowrap">{$_(`media.batch.status.${entry.status}`)}</span>
                {#if entry.reason}
                  <span class="block text-base-content/60">
                    {$_(`media.batch.reason.${entry.reason}`, { default: entry.reason })}
                  </span>
                {/if}
              </td>
            </tr>
          {:else}
            <tr><td colspan="4" class="py-6 text-center text-base-content/60">{$_('media.batch.empty')}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>

    {#if pages > 1}
      <div class="mt-3 flex items-center justify-center gap-3 text-sm">
        <button type="button" class="btn btn-sm" disabled={page <= 1} onclick={() => show(page - 1)}>
          {$_('data.paginator.previous')}
        </button>
        <span class="text-base-content/60">{page} / {pages}</span>
        <button type="button" class="btn btn-sm" disabled={page >= pages} onclick={() => show(page + 1)}>
          {$_('data.paginator.next')}
        </button>
      </div>
    {/if}
  {/if}
  <div class="modal-action">
    <button type="button" class="btn" onclick={() => modal.close()}>{$_('action.close', '')}</button>
  </div>
</Modal>
