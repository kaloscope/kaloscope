<script lang="ts">
  import { tooltip } from '$lib/actions';
  import { api } from '$lib/api';
  import {
    alert,
    Badge,
    Button,
    Cell,
    Checkbox,
    DataView,
    DownloadDelConfirm,
    downloadPrompt,
    HCell,
    Paginator,
    Search,
    Select,
    type PaginatorProps
  } from '$lib/components';
  import { DownloadState } from '$lib/enums';
  import { createLoading, createSortField } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { DownloadAction, Downloader, DownloadTask, Page, Resp } from '$lib/types';
  import { onMount, untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  /** An inline action supported by the active-download table. */
  type TaskAction = Extract<DownloadAction, 'pause' | 'resume' | 'retry'>;

  /** The non-completed states requested from the download-task API. */
  const activeStates = (Object.keys(DownloadState) as DownloadTask['state'][]).filter((state) => state !== 'completed');
  const phaseStates = new Set<DownloadTask['state']>(['remote', 'settling', 'pulling', 'verifying']);

  let tasks: DownloadTask[] = $state([]);
  let name: string = $state('');
  let downloader: number | null = $state(null);
  let downloaders: Downloader[] = $state([]);
  let deleteConfirm: DownloadDelConfirm;
  let headerCheckbox: Checkbox;
  let navigatorClipboard = $state(false);

  const pagination: PaginatorProps = $state({ current: 1, size: 50, onchange: search });
  const ordering = createSortField();
  const loading = createLoading();
  const loadingIds = new SvelteSet<number>();

  /**
   * Search for download tasks.
   *
   * @param page - The page number.
   * @param size - The page size.
   * @param auto - Whether to search automatically.
   */
  function search(page: number = 1, size: number = pagination.size, auto: boolean = false) {
    if (!auto) {
      loading.start();
    } else if ($loading !== null) {
      return;
    }
    api
      .get('download/list', {
        searchParams: [
          ['page_num', page],
          ['page_size', size],
          ['ordering', $ordering],
          ['downloader_id', downloader || 0],
          ['name', name],
          ...activeStates.map((state) => ['states', state] as [string, string])
        ]
      })
      .json<Resp<Page<DownloadTask>>>()
      .then(({ data }) => {
        pagination.current = page;
        pagination.size = size;
        pagination.total = data.total;
        tasks = data.items;
      })
      .finally(() => {
        loading.end();
        syncSelection(auto);
      });
  }

  /**
   * Keep selected task IDs aligned with the current page.
   *
   * @param preserve - Whether to preserve selection across automatic refresh.
   */
  function syncSelection(preserve: boolean = false) {
    if (!headerCheckbox) {
      return;
    }
    if (!preserve) {
      headerCheckbox.unselectAll();
      return;
    }
    const currentKeys = new Set(tasks.map((task) => String(task.id)));
    const selectedKeys = headerCheckbox.getSelectedKeys();
    const nextKeys = selectedKeys.filter((key) => currentKeys.has(key));
    selectedKeys.splice(0, selectedKeys.length, ...nextKeys);
  }

  /**
   * Delete selected download tasks.
   */
  function batchDelete() {
    const currentKeys = new Set(tasks.map((task) => String(task.id)));
    const ids = headerCheckbox
      .getSelectedKeys()
      .filter((key) => currentKeys.has(key))
      .map(Number);
    if (ids.length === 0) {
      alert({ message: 'select_delete_items' });
      return;
    }
    deleteConfirm.showModal(ids);
  }

  /**
   * Perform an action on a download task.
   *
   * @param task - The download task.
   * @param action - The action to perform.
   */
  function performTaskAction(task: DownloadTask, action: TaskAction) {
    loadingIds.add(task.id);
    const startTime = new Date().getTime();
    api
      .post(`download/${action === 'resume' ? 'start' : action}`, { json: { ids: [task.id] } })
      .then(() => {
        const timeout = Math.max(0, 450 - (new Date().getTime() - startTime));
        setTimeout(() => {
          loadingIds.delete(task.id);
          search(pagination.current, pagination.size, true);
        }, timeout);
      })
      .catch(() => {
        loadingIds.delete(task.id);
      });
  }

  /**
   * Build the deduplicated error tooltip for a download task.
   *
   * @param task - The download task.
   * @returns The localized structured error and optional driver message.
   */
  function errorTooltip(task: DownloadTask): string {
    const kind = task.error_kind ? $_(`download.error.${task.error_kind}`) : '';
    return [...new Set([kind, task.error_msg].filter(Boolean))].join('\n');
  }

  /**
   * Copy the magnet link of a download task.
   *
   * @param task - The download task.
   */
  async function copyMagnetLink(task: DownloadTask) {
    if (!task.magnet_link || !navigator.clipboard) {
      return;
    }
    try {
      await navigator.clipboard.writeText(task.magnet_link);
      alert({ level: 'success', message: 'magnet_link_copied' });
    } catch {
      return;
    }
  }

  $effect(() => {
    $ordering; // eslint-disable-line
    untrack(() => search());
  });

  onMount(() => {
    navigatorClipboard = !!navigator.clipboard;
    api
      .get('download/manager/list')
      .json<Resp<Downloader[]>>()
      .then(({ data }) => {
        downloaders = data;
      });
    // refresh every second
    const refreshInterval = setInterval(() => {
      search(pagination.current, pagination.size, true);
    }, 1000);
    return () => clearInterval(refreshInterval);
  });
</script>

<DataView dvh rowSelect loading={$loading} data={tasks} uniqueKey="id">
  {#snippet filters()}
    <Select
      filter
      options={[{ value: '', label: $_('enum.all') }, ...downloaders.map((d) => ({ value: d.id, label: d.name }))]}
      bind:value={downloader}
      label={$_('download.downloader.title')}
      onchange={() => search()}
    />
    <Search label={$_('field.name')} bind:value={name} onsearch={() => search()} />
  {/snippet}
  {#snippet actions()}
    <Button
      size="md"
      icon={icons.delete}
      text={$_('action.batch_delete', $_('entity.tasks'))}
      onclick={() => batchDelete()}
    />
    <Button
      size="md"
      icon={icons.addCircle}
      text={$_('action.add', $_('entity.download'))}
      onclick={() => downloadPrompt()}
    />
  {/snippet}
  {#snippet header()}
    <HCell width="2rem">
      <Checkbox batch={tasks.length} bind:this={headerCheckbox} />
    </HCell>
    <HCell width={['8rem', null]} text={$_('download.downloader.title')} sort={ordering.bind('downloader_id')} />
    <HCell width="100%" text={$_('field.name')} sort={ordering.bind('name')} />
    <HCell actions />
  {/snippet}
  {#snippet row(task)}
    {@const state = DownloadState[task.state]}
    <Cell>
      <Checkbox key={String(task.id)} />
    </Cell>
    <Cell class="max-lg:hidden">
      {@const downloader = downloaders.find((d) => d.id === task.downloader_id)}
      {#if downloader}
        <Badge>{downloader.name}</Badge>
      {/if}
    </Cell>
    <Cell>
      <div class="flex w-full flex-col gap-2 pr-2">
        <div class="flex items-start justify-between gap-2">
          <span class="min-w-0 text-sm wrap-break-word">{task.name}</span>
          <div class="flex shrink-0 items-center gap-1 opacity-80">
            {#if task.state === 'error' || task.error_kind || task.error_msg}
              <iconify-icon
                use:tooltip={{ content: errorTooltip(task), placement: 'left' }}
                icon={task.state === 'error' ? icons.info : icons.warning}
                width="1.25rem"
                class={task.state === 'error' ? 'text-error' : 'text-warning'}
              ></iconify-icon>
            {:else if phaseStates.has(task.state)}
              <iconify-icon
                use:tooltip={{ content: $_(state.label), placement: 'left' }}
                icon={state.icon}
                width="1.25rem"
                style:color={state.iconColor}
              ></iconify-icon>
            {/if}
          </div>
        </div>
        {#if !['paused', 'error', 'submit_unknown'].includes(task.state)}
          <progress class="progress progress-success" value={task.percentage ?? 0} max="100"></progress>
        {:else}
          <progress class="progress opacity-50" value={task.percentage ?? 0} max="100"></progress>
        {/if}
        <div class="flex justify-between gap-2 text-xs opacity-50">
          <span>{task.ratio || `${Math.round(task.percentage ?? 0)}%`}</span>
          <span>{task.estimate}</span>
        </div>
      </div>
    </Cell>
    <Cell
      actions={[
        {
          condition: task.capabilities.includes('pause'),
          loading: loadingIds.has(task.id),
          class: '[&_iconify-icon]:text-surface/80',
          icon: icons.pauseFilled,
          text: $_('action.pause', $_('entity.task')),
          onclick: () => performTaskAction(task, 'pause')
        },
        {
          condition: task.capabilities.includes('resume'),
          loading: loadingIds.has(task.id),
          class: '[&_iconify-icon]:text-surface/80',
          icon: icons.playFilled,
          text: $_('action.resume', $_('entity.task')),
          onclick: () => performTaskAction(task, 'resume')
        },
        {
          condition: task.capabilities.includes('retry'),
          loading: loadingIds.has(task.id),
          icon: icons.arrowRotateClockwise,
          text: $_('action.retry', $_('entity.task')),
          onclick: () => performTaskAction(task, 'retry')
        },
        {
          condition: !!task.magnet_link && navigatorClipboard,
          icon: icons.magnetUp,
          text: $_('action.copy', $_('field.link')),
          onclick: () => copyMagnetLink(task)
        },
        {
          condition: task.capabilities.includes('delete'),
          icon: icons.delete,
          text: $_('action.delete', $_('entity.task')),
          onclick: () => deleteConfirm.showModal(task.id)
        }
      ]}
    />
  {/snippet}
  {#snippet paginator(disabled)}
    <Paginator {disabled} {...pagination} />
  {/snippet}
</DataView>

<DownloadDelConfirm bind:this={deleteConfirm} onconfirm={() => search(pagination.current)} />
