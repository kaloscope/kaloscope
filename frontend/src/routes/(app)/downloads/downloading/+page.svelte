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
  import { createLoading, createSortField } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { Downloader, DownloadTask, Page, Resp } from '$lib/types';
  import { onMount, untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

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
          ['states', 'downloading'],
          ['states', 'paused'],
          ['states', 'error']
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
   * Pause download task by ID.
   *
   * @param task - The download task.
   */
  function pause(task: DownloadTask) {
    loadingIds.add(task.id);
    const startTime = new Date().getTime();
    api
      .post('download/pause', { json: { ids: [task.id] } })
      .then(() => {
        const timeout = Math.max(0, 450 - (new Date().getTime() - startTime));
        setTimeout(() => {
          task.state = 'paused';
          loadingIds.delete(task.id);
        }, timeout);
      })
      .catch(() => {
        loadingIds.delete(task.id);
      });
  }

  /**
   * Start download task by ID.
   *
   * @param task - The download task.
   */
  function start(task: DownloadTask) {
    loadingIds.add(task.id);
    const startTime = new Date().getTime();
    api
      .post('download/start', { json: { ids: [task.id] } })
      .then(() => {
        const timeout = Math.max(0, 450 - (new Date().getTime() - startTime));
        setTimeout(() => {
          task.state = 'downloading';
          loadingIds.delete(task.id);
        }, timeout);
      })
      .catch(() => {
        loadingIds.delete(task.id);
      });
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
        <div class="flex items-center justify-between gap-1">
          <span class="min-w-0 text-sm wrap-break-word">{task.name}</span>
          {#if task.state === 'error'}
            <iconify-icon
              use:tooltip={{ content: task.error_msg || '', placement: 'left' }}
              icon={icons.info}
              width="1rem"
              class="text-error"
            ></iconify-icon>
          {/if}
        </div>
        {#if task.state === 'downloading'}
          <progress class="progress progress-success" value={task.percentage || undefined} max="100"></progress>
        {:else}
          <progress class="progress opacity-50" value={task.percentage || 0} max="100"></progress>
        {/if}
        <div class="flex justify-between gap-2 text-xs opacity-50">
          <span>{task.ratio}</span>
          <span>{task.estimate}</span>
        </div>
      </div>
    </Cell>
    <Cell
      actions={[
        {
          condition: task.state === 'downloading',
          loading: loadingIds.has(task.id),
          class: '[&_iconify-icon]:text-surface/80',
          icon: icons.pauseFilled,
          text: $_('action.pause', $_('entity.task')),
          onclick: () => pause(task)
        },
        {
          condition: task.state === 'paused',
          loading: loadingIds.has(task.id),
          class: '[&_iconify-icon]:text-surface/80',
          icon: icons.playFilled,
          text: $_('action.start', $_('entity.task')),
          onclick: () => start(task)
        },
        {
          condition: !!task.magnet_link && navigatorClipboard,
          icon: icons.magnetUp,
          text: $_('action.copy', $_('field.link')),
          onclick: () => copyMagnetLink(task)
        },
        {
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
