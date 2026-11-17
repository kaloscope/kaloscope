<script lang="ts">
  import { api } from '$lib/api';
  import { Modal, Select, alert } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _, locale, number } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { IconifyIcon } from 'iconify-icon';
  import type { MediaBatch, MediaBatchEntry, MediaBatchOutcome, Page, Resp } from '$lib/types';
  import { describeBatchReasons, watchMediaBatch } from '$lib/utils/media-batches';
  import { formatBytes } from '$lib/utils/number';
  import { onDestroy } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  const PAGE_SIZE = 50;
  const STATUSES = ['planned', 'done', 'skipped', 'failed', 'uncertain', 'blocked'] as const;
  // the status column marks each outcome with an icon and a tone
  const STATUS_STYLES: Record<MediaBatchEntry['status'], { icon: IconifyIcon; tone: string }> = {
    planned: { icon: icons.clock, tone: 'text-base-content/60' },
    started: { icon: icons.clock, tone: 'text-info' },
    done: { icon: icons.checkmarkCircle, tone: 'text-success' },
    skipped: { icon: icons.subtractCircle, tone: 'text-warning' },
    failed: { icon: icons.dismissCircle, tone: 'text-error' },
    uncertain: { icon: icons.warning, tone: 'text-warning' },
    blocked: { icon: icons.subtractCircle, tone: 'text-base-content/60' }
  };

  const { onfinish, onstate }: { onfinish?: () => void; onstate?: (batch: MediaBatch) => void } = $props();

  let batch: MediaBatch | null = $state(null);
  let entries: MediaBatchEntry[] = $state([]);
  let total = $state(0);
  let page = $state(1);
  // `all` keeps the default option from looking like a placeholder
  let status: string = $state('all');
  let failed = $state(false);
  let timer: ReturnType<typeof setTimeout> | undefined;
  let id = 0;
  let request = 0;
  let opened = false;
  // units start expanded, so only the ones the user folds are kept
  const collapsed = new SvelteSet<number>();

  // the modal dialog instance
  let modal: Modal;
  const loading = createLoading();
  const pages = $derived(Math.max(1, Math.ceil(total / PAGE_SIZE)));
  // rows without files keep a gap where the chevron sits, so names line up
  const tree = $derived(entries.some((entry) => entry.children?.length));
  // read through closures, since `batch` would otherwise narrow to its initial `null`
  const reviewed = $derived.by(() => batch?.state === 'reviewed');
  // files handled with their unit are listed under it, so only real refusals count here
  const blockedReasons = $derived.by(() => {
    const reasons: Record<string, number> = { ...batch?.blocked.reasons };
    delete reasons.follows_item;
    return reasons;
  });
  const blocked = $derived.by(() => Object.values(blockedReasons).reduce((sum, count) => sum + count, 0));
  const action = $derived.by(() =>
    batch?.operation === 'delete' ? (batch.deletion ?? 'trash') : (batch?.operation ?? 'apply')
  );
  const scope = $derived.by(() =>
    batch ? $_('media.batch.scope', [batch.planned.units, batch.planned.files, formatBytes(batch.planned.bytes)]) : ''
  );
  // after applying, the scope line names the operation instead of the review sentence
  const actionLabel = $derived.by(() => {
    if (action === 'trash' || action === 'permanent') {
      return $_(`media.batch.deletion.${action}`);
    }
    return action === 'apply' ? '' : $_(`media.batch.operation.${action}`);
  });
  const blockedText = $derived.by(() => {
    // a single reason already shares the total, so it needs no count of its own
    const counted = Object.keys(blockedReasons).length > 1;
    return $_('media.batch.blocked_reasons', [
      blocked,
      describeBatchReasons(blockedReasons, $_, $number, $locale, counted)
    ]);
  });
  const groups = $derived.by(() => {
    if (!batch) {
      return [];
    }
    const counts: Record<(typeof STATUSES)[number], number> = reviewed
      ? { planned: batch.planned.units, done: 0, skipped: 0, failed: 0, uncertain: 0, blocked }
      : {
          planned: batch.counts.remaining,
          done: batch.counts.done,
          skipped: batch.counts.skipped,
          failed: batch.counts.failures,
          uncertain: batch.counts.uncertain,
          blocked
        };
    return STATUSES.filter((value) => counts[value] > 0 || value === status).map((value) => ({
      value,
      count: counts[value]
    }));
  });
  // filtering only helps when the units do not all share one status
  const filterable = $derived(status !== 'all' || groups.filter((group) => group.count > 0).length > 1);

  /**
   * Open the dialog for one reviewed batch.
   *
   * @param batchId - Batch whose plan and progress are shown.
   */
  export const showModal = (batchId: number) => {
    id = batchId;
    opened = true;
    batch = null;
    entries = [];
    page = 1;
    status = 'all';
    failed = false;
    collapsed.clear();
    modal.show();
    void load();
  };

  /**
   * Load the latest batch view and keep refreshing while it runs.
   *
   * Ignore superseded responses and failures, including those from an earlier opening.
   */
  async function load() {
    if (!opened) {
      return;
    }

    clearTimeout(timer);
    const current = ++request;
    const batchId = id;

    try {
      const searchParams: Record<string, string | number> = { page_num: page, page_size: PAGE_SIZE };
      if (status !== 'all') {
        searchParams.status = status;
      }

      const [details, list] = await Promise.all([
        api.get(`media/batch/${batchId}`).json<Resp<MediaBatch>>(),
        api.get(`media/batch/${batchId}/entries`, { searchParams }).json<Resp<Page<MediaBatchEntry>>>()
      ]);
      if (current !== request) {
        return;
      }

      const wasRunning = batch?.state === 'running';
      batch = details.data;
      onstate?.(batch);
      entries = list.data.items;
      total = list.data.total ?? 0;
      failed = false;

      if (batch.state === 'running') {
        watchMediaBatch(batch.id);
        timer = setTimeout(load, 2000);
      } else if (wasRunning) {
        onfinish?.();
      }
    } catch {
      if (current === request) {
        failed = true;
      }
    }
  }

  /**
   * Stop polling and discard pending reads when the dialog closes or is destroyed.
   */
  function stopRefreshing() {
    opened = false;
    request += 1;
    clearTimeout(timer);
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
   * Fold or unfold the files handled together with one unit.
   *
   * @param seq - Sequence number of the unit.
   */
  function toggle(seq: number) {
    if (collapsed.has(seq)) {
      collapsed.delete(seq);
    } else {
      collapsed.add(seq);
    }
  }

  /**
   * Pick the reason worth showing for an entry.
   *
   * Files that go with their unit are already listed under it, so they need no note.
   *
   * @param entry - Unit or file whose reason is shown.
   * @returns The translated reason, or an empty string when there is nothing to explain.
   */
  function notice(entry: MediaBatchEntry): string {
    return entry.reason && entry.reason !== 'follows_item' ? reasonLabel(entry.reason) : '';
  }

  /**
   * Shorten a file path to its part below the unit's folder.
   *
   * @param path - Library-relative path of a file handled with the unit.
   * @param unit - Library-relative path of the unit itself.
   * @returns The path below the unit, or below its parent folder when the unit is a file.
   */
  function childPath(path: string, unit: string): string {
    for (const base of [unit, unit.slice(0, Math.max(0, unit.lastIndexOf('/')))]) {
      if (base && path.startsWith(`${base}/`)) {
        return path.slice(base.length + 1);
      }
    }
    return path;
  }

  /**
   * Translate a reason code, keeping unknown codes readable.
   *
   * @param reason - Reason code stored with an entry.
   * @returns The translated reason.
   */
  function reasonLabel(reason: string): string {
    return $_(`media.batch.reason.${reason}`, { default: reason });
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

  /**
   * Apply the reviewed plan on the user's confirmation and follow its progress.
   */
  function apply() {
    const batchId = id;
    loading.start();
    api
      .post(`media/batch/${batchId}/apply`)
      .json<Resp<MediaBatchOutcome & { stale?: boolean; pending?: boolean }>>()
      .then(({ data }) => {
        if (data.stale || data.pending) {
          alert({ level: 'warning', message: data.stale ? 'media_batch_stale' : 'media_batch_pending' });
        } else if (data.applied !== false) {
          watchMediaBatch(batchId);
          if (data.outcome !== 'running') onfinish?.();
        }
        return load();
      })
      .finally(() => loading.end());
  }

  onDestroy(stopRefreshing);
</script>

{#snippet ownerTag(entry: MediaBatchEntry)}
  {#if entry.owner !== 'indexed'}
    <span class="ml-1 badge badge-ghost align-middle font-sans badge-xs">{$_(`media.batch.owner.${entry.owner}`)}</span>
  {/if}
{/snippet}

<Modal
  icon={icons.listCheck}
  title={$_('media.batch.title')}
  maxWidth="56rem"
  bind:this={modal}
  onclose={stopRefreshing}
>
  {#if failed}
    <p class="py-6 text-sm text-base-content/60">{$_('media.batch.unavailable')}</p>
  {:else if !batch}
    <div class="flex justify-center py-10"><span class="loading loading-md loading-spinner"></span></div>
  {:else}
    <div class="mt-2 space-y-1.5 text-sm">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span class="font-medium">{batch.library_name}</span>
        {#if !reviewed}
          <span class="badge badge-sm" class:badge-info={batch.state === 'running'}>
            {$_(`media.batch.state.${batch.state}`)}
          </span>
        {/if}
        {#if batch.reverts}
          <span class="text-base-content/60">{$_('media.batch.reverts', batch.reverts)}</span>
        {/if}
      </div>
      <p class="text-base-content/80">
        {#if reviewed}
          {$_(`media.batch.summary.${action}`, scope)}
        {:else}
          {actionLabel ? `${actionLabel} · ${scope}` : scope}
        {/if}
      </p>
      {#if blocked}
        <p class="flex items-start gap-1.5 text-base-content/80">
          <iconify-icon icon={icons.warning} class="mt-0.5 shrink-0 text-warning" width="1rem" aria-hidden="true"
          ></iconify-icon>
          <span>
            {blockedText}
          </span>
        </p>
      {/if}
      {#if batch.more}
        <p class="text-xs text-base-content/60">{$_('media.batch.more')}</p>
      {/if}
    </div>
    {#if !reviewed}
      <div class="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm" role="status">
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

    {#if filterable}
      <div class="mt-3 flex items-center gap-2">
        <label class="sr-only" for="batch-status">{$_('media.batch.column.status')}</label>
        <Select id="batch-status" class="w-auto select-sm" bind:value={status} onchange={() => show(1)}>
          <option value="all">{$_('media.batch.all')}</option>
          {#each groups as group (group.value)}
            <option value={group.value}>
              {$_('media.batch.reason_count', [$_(`media.batch.status.${group.value}`), $number(group.count)])}
            </option>
          {/each}
        </Select>
      </div>
    {/if}

    <div class="mt-3 max-h-[50vh] overflow-auto rounded-field border border-base-content/10">
      <table class="table table-pin-rows table-sm">
        <thead>
          <tr>
            <th>{$_('media.batch.column.path')}</th>
            <th class="text-right">{$_('media.batch.column.size')}</th>
            {#if !reviewed}
              <th>{$_('media.batch.column.status')}</th>
            {/if}
          </tr>
        </thead>
        <tbody>
          {#each entries as entry (entry.seq)}
            {@const children = entry.children ?? []}
            {@const open = children.length > 0 && !collapsed.has(entry.seq)}
            {@const style = STATUS_STYLES[entry.status]}
            {@const reason = notice(entry)}
            <!-- daisyUI draws row dividers on cells, so a group drops them until its last file -->
            <tr class="align-top {reason ? 'text-base-content/60' : ''} {open ? '[&>td]:border-b-0' : ''}">
              <td class={open ? 'relative' : ''}>
                {#if open}
                  <span class="absolute top-7.5 bottom-0 left-4.5 w-px bg-base-content/10" aria-hidden="true"></span>
                {/if}
                <div class="flex items-start gap-1.5">
                  {#if children.length}
                    <button
                      type="button"
                      class="-ml-1 flex size-5 shrink-0 items-center justify-center rounded-field text-base-content/60 transition-colors hover:bg-base-content/5 hover:text-base-content"
                      aria-expanded={open}
                      aria-label={$_('media.batch.files', [children.length])}
                      onclick={() => toggle(entry.seq)}
                    >
                      <iconify-icon
                        icon={icons.chevronRight}
                        class="transition-transform {open ? 'rotate-90' : ''}"
                        width="0.875rem"
                        aria-hidden="true"
                      ></iconify-icon>
                    </button>
                  {:else if tree}
                    <span class="-ml-1 size-5 shrink-0" aria-hidden="true"></span>
                  {/if}
                  <div class="min-w-0 pt-0.5">
                    <span class="font-mono text-xs break-all">{entry.src}</span>
                    {@render ownerTag(entry)}
                    {#if children.length && !open}
                      <span class="ml-1 text-xs whitespace-nowrap text-base-content/50">
                        {$_('media.batch.files', [children.length])}
                      </span>
                    {/if}
                    {#if entry.dst}
                      <span class="block font-mono text-xs break-all text-base-content/60">→ {entry.dst}</span>
                    {/if}
                    {#if reviewed && reason}
                      <span class="mt-0.5 flex items-center gap-1 text-xs text-base-content/70">
                        <iconify-icon icon={icons.warning} class="text-warning" width="0.875rem" aria-hidden="true"
                        ></iconify-icon>
                        {reason}
                      </span>
                    {/if}
                  </div>
                </div>
              </td>
              <td class="pt-2.5 text-right text-xs whitespace-nowrap">{formatBytes(entry.size)}</td>
              {#if !reviewed}
                <td class="pt-2.5 text-xs">
                  <span class="inline-flex items-center gap-1 whitespace-nowrap">
                    {#if entry.status === 'started'}
                      <span class="loading loading-xs loading-spinner {style.tone}"></span>
                    {:else}
                      <iconify-icon icon={style.icon} class={style.tone} width="0.875rem" aria-hidden="true"
                      ></iconify-icon>
                    {/if}
                    {$_(`media.batch.status.${entry.status}`)}
                  </span>
                  {#if reason}
                    <span class="block text-base-content/60">{reason}</span>
                  {/if}
                </td>
              {/if}
            </tr>
            {#if open}
              {#each children as child, index (child.seq)}
                {@const last = index === children.length - 1}
                <!-- the guide line continues from under the unit's chevron through its files; it is a 1px
                fill because the app recolors `border-l` with the stronger shared border token -->
                <tr class="text-base-content/60 {last ? '' : '[&>td]:border-b-0'}">
                  <td class="relative py-1 pl-10.5">
                    <span
                      class="absolute top-0 left-4.5 w-px bg-base-content/10 {last ? 'bottom-1.5' : 'bottom-0'}"
                      aria-hidden="true"
                    ></span>
                    <span class="font-mono text-xs break-all">{childPath(child.src, entry.src)}</span>
                    {@render ownerTag(child)}
                  </td>
                  <td class="py-1 text-right text-xs whitespace-nowrap">{formatBytes(child.size)}</td>
                  {#if !reviewed}
                    <td class="py-1"></td>
                  {/if}
                </tr>
              {/each}
            {/if}
          {:else}
            <tr>
              <td colspan={reviewed ? 2 : 3} class="py-6 text-center text-base-content/60">{$_('media.batch.empty')}</td
              >
            </tr>
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
  <div class="modal-action flex-wrap items-center">
    {#if batch?.confirmable && !failed && batch.operation === 'delete'}
      {@const permanent = batch.deletion === 'permanent'}
      <p class="mr-auto flex items-center gap-1.5 text-xs {permanent ? 'text-error' : 'text-base-content/60'}">
        <iconify-icon icon={permanent ? icons.warning : icons.info} width="0.875rem" aria-hidden="true"></iconify-icon>
        {$_(`media.batch.note.${permanent ? 'permanent' : 'trash'}`)}
      </p>
    {/if}
    {#if batch?.confirmable && !failed}
      <button
        type="button"
        class="btn {batch.deletion === 'permanent' ? 'btn-error' : 'btn-primary'}"
        disabled={$loading !== null}
        onclick={apply}
      >
        {#if $loading !== null}<span class="loading loading-xs loading-spinner"></span>{/if}
        {$_(`media.batch.confirm.${batch.deletion ?? 'apply'}`, $number(batch.planned.units))}
      </button>
    {/if}
    <button type="button" class="btn" onclick={() => modal.close()}>{$_('action.close', '')}</button>
  </div>
</Modal>
