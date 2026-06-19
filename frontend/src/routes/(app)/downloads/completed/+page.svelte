<script lang="ts">
  import { api } from '$lib/api';
  import {
    Badge,
    Cell,
    DataView,
    DownloadDelConfirm,
    HCell,
    Paginator,
    Search,
    Select,
    type PaginatorProps
  } from '$lib/components';
  import { createLoading, createSortField } from '$lib/helpers';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { Downloader, DownloadTask, Page, Resp } from '$lib/types';
  import { onMount, untrack } from 'svelte';

  let tasks: DownloadTask[] = $state([]);
  let name: string = $state('');
  let downloader: number | null = $state(null);
  let downloaders: Downloader[] = $state([]);
  let deleteConfirm: DownloadDelConfirm;

  const pagination: PaginatorProps = $state({ current: 1, size: 50, onchange: search });
  const ordering = createSortField();
  const loading = createLoading();

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
        searchParams: {
          page_num: page,
          page_size: size,
          ordering: $ordering,
          downloader_id: downloader || 0,
          name: name,
          state: 'completed'
        }
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
      });
  }

  $effect(() => {
    $ordering; // eslint-disable-line
    untrack(() => search());
  });

  onMount(() => {
    api
      .get('download/manager/list')
      .json<Resp<Downloader[]>>()
      .then(({ data }) => {
        downloaders = data;
      });
    // refresh every 5 seconds
    const refreshInterval = setInterval(() => {
      search(pagination.current, pagination.size, true);
    }, 5000);
    return () => clearInterval(refreshInterval);
  });
</script>

<DataView dvh loading={$loading} data={tasks}>
  {#snippet filters()}
    <Select
      filter
      options={[{ value: '', label: 'enum.all' }, ...downloaders.map((d) => ({ value: d.id, label: d.name }))]}
      bind:value={downloader}
      label={$_('download.downloader.title')}
      onchange={() => search()}
    />
    <Search label={$_('field.name')} bind:value={name} onsearch={() => search()} />
  {/snippet}
  {#snippet header()}
    <HCell width={['8rem', null]} text={$_('download.downloader.title')} sort={ordering.bind('downloader_id')} />
    <HCell width="100%" text={$_('field.name')} sort={ordering.bind('name')} />
    <HCell actions />
  {/snippet}
  {#snippet row(task)}
    <Cell class="max-lg:hidden">
      {@const downloader = downloaders.find((d) => d.id === task.downloader_id)}
      {#if downloader}
        <Badge>{downloader.name}</Badge>
      {/if}
    </Cell>
    <Cell>
      <div class="flex w-full flex-col gap-2 pr-2">
        <div class="flex items-center justify-between gap-2">
          <span class="text-sm">{task.name}</span>
        </div>
        <div class="flex justify-between gap-2 text-xs opacity-50">
          <span>{task.ratio.slice(0, -6)}</span>
          <span>{$dateTime(task.completed_at)}</span>
        </div>
      </div>
    </Cell>
    <Cell
      actions={[
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
