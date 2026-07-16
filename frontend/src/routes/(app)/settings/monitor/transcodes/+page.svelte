<script lang="ts">
  import { tooltip } from '$lib/actions';
  import { api } from '$lib/api';
  import { alert, Badge, Button, Cell, Checkbox, confirm, DataView, HCell, Search, Select } from '$lib/components';
  import { enumToOptions, TranscodeState } from '$lib/enums';
  import { createLoading, createSortField } from '$lib/helpers';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { OptionValue, Resp, TranscodeTask } from '$lib/types';
  import { onMount, untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  let tasks: TranscodeTask[] = $state([]);
  let keyword: string = $state('');
  let taskState: OptionValue = $state(null);
  let headerCheckbox: Checkbox;

  let deletableIds = $derived(
    new Set(tasks.filter((task) => task.state !== 'running' && task.state !== 'stopping').map((task) => task.id))
  );

  const ordering = createSortField();
  const loading = createLoading();
  const loadingIds = new SvelteSet<string>();

  /**
   * Fetch transcode tasks using the current filters and sorting.
   *
   * @param auto - Whether the request is an automatic refresh.
   */
  function search(auto: boolean = false) {
    if (!auto) {
      loading.start();
    } else if ($loading !== null) {
      return;
    }
    api
      .get('media/transcode/list', {
        searchParams: {
          ordering: $ordering,
          state: taskState ?? '',
          keyword
        }
      })
      .json<Resp<TranscodeTask[]>>()
      .then(({ data }) => {
        tasks = data;
      })
      .finally(() => {
        loading.end();
        syncSelection(auto);
      });
  }

  /**
   * Keep selected task IDs aligned with the refreshed task list.
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
    const selectedKeys = headerCheckbox.getSelectedKeys();
    const nextKeys = selectedKeys.filter((key) => deletableIds.has(key));
    selectedKeys.splice(0, selectedKeys.length, ...nextKeys);
  }

  /**
   * Delete the selected transcode tasks after confirmation.
   */
  function batchDelete() {
    const ids = headerCheckbox.getSelectedKeys().filter((key) => deletableIds.has(key));
    if (ids.length === 0) {
      alert({ message: 'select_delete_items' });
      return;
    }
    confirm({
      icon: icons.delete,
      title:
        ids.length > 1 ? $_('transcode.batch_delete_title', ids.length) : $_('action.delete', $_('entity.transcode')),
      onconfirm: () => deleteTasks(ids)
    });
  }

  /**
   * Confirm deletion of a single transcode task.
   *
   * @param task - The transcode task to delete.
   */
  function confirmDelete(task: TranscodeTask) {
    confirm({
      icon: icons.delete,
      title: $_('action.delete', $_('entity.transcode')),
      onconfirm: () => deleteTasks([task.id])
    });
  }

  /**
   * Delete transcode tasks and refresh the task list.
   *
   * @param ids - The task IDs to delete.
   */
  function deleteTasks(ids: string[]) {
    for (const id of ids) {
      loadingIds.add(id);
    }
    api
      .post('media/transcode/delete', { json: { ids } })
      .then(() => search(true))
      .finally(() => {
        for (const id of ids) {
          loadingIds.delete(id);
        }
      });
  }

  /**
   * Stop a running transcode task.
   *
   * @param task - The transcode task to stop.
   */
  function stopTask(task: TranscodeTask) {
    loadingIds.add(task.id);
    api
      .post('media/transcode/stop', { json: { ids: [task.id] } })
      .then(() => search(true))
      .finally(() => loadingIds.delete(task.id));
  }

  /**
   * Format the progress and encoded segment count of a transcode task.
   *
   * @param task - The transcode task to format.
   */
  function formatProgress(task: TranscodeTask): string {
    const segments = $_('transcode.segments', task.encoded_segments);
    if (task.progress === null) {
      return segments;
    }
    return `${task.progress}% · ${segments}`;
  }

  $effect(() => {
    $ordering; // eslint-disable-line
    untrack(() => search());
  });

  onMount(() => {
    // refresh every second
    const refreshInterval = setInterval(() => {
      search(true);
    }, 1000);
    return () => clearInterval(refreshInterval);
  });
</script>

<DataView dvh rowSelect loading={$loading} data={tasks} uniqueKey="id">
  {#snippet filters()}
    <Select
      translate
      filter
      options={enumToOptions(TranscodeState)}
      bind:value={taskState}
      label={$_('field.state')}
      onchange={() => search()}
    />
    <Search label={$_('field.name')} bind:value={keyword} onsearch={() => search()} />
  {/snippet}
  {#snippet actions()}
    <Button
      size="md"
      icon={icons.delete}
      text={$_('action.batch_delete', $_('entity.tasks'))}
      onclick={() => batchDelete()}
    />
  {/snippet}
  {#snippet header()}
    <HCell width="2rem">
      <Checkbox batch={deletableIds.size} disabled={deletableIds.size === 0} bind:this={headerCheckbox} />
    </HCell>
    <HCell width="100%" text={$_('field.name')} sort={ordering.bind('name')} />
    <HCell width={['8rem', null]} text={$_('field.state')} sort={ordering.bind('state')} />
    <HCell width={['8rem', null]} text={$_('field.size')} sort={ordering.bind('encoded_size')} />
    <HCell actions />
  {/snippet}
  {#snippet row(task)}
    {@const state = TranscodeState[task.state]}
    {@const quality = task.quality ? $_(`transcode.quality.${task.quality}`) : null}
    {@const resolution = task.resolution ? $_(`transcode.resolution.${task.resolution}`) : null}
    {@const hwaccel = task.hwaccel ? $_(`transcode.hwaccel.${task.hwaccel}`) : 'CPU'}
    {@const deletable = deletableIds.has(task.id)}
    <Cell>
      <Checkbox key={task.id} disabled={!deletable} />
    </Cell>
    <Cell>
      <div class="flex w-full flex-col gap-2 pr-2">
        <div class="flex items-center justify-between gap-1">
          <div class="min-w-0">
            <div class="text-sm wrap-break-word">
              {task.title ?? task.name}
              {#if task.subtitle}
                <span class="italic-text opacity-70">[{task.subtitle}]</span>
              {/if}
            </div>
            {#if task.path}
              <div class="truncate text-xs opacity-50">{task.path}</div>
            {/if}
          </div>
          {#if task.state === 'error'}
            <iconify-icon
              use:tooltip={{ content: task.error_msg || '', placement: 'left' }}
              icon={icons.info}
              width="1rem"
              class="text-error"
            ></iconify-icon>
          {/if}
        </div>
        <div class="flex flex-wrap gap-1">
          {#if quality}
            <Badge shadow={false}>{quality}</Badge>
          {/if}
          {#if resolution}
            <Badge shadow={false}>{resolution}</Badge>
          {/if}
          {#if hwaccel}
            <Badge shadow={false}>{hwaccel}</Badge>
          {/if}
        </div>
        {#if task.state === 'running'}
          <progress class="progress progress-success" value={task.progress || undefined} max="100"></progress>
        {:else if task.state === 'finished'}
          <progress class="progress progress-success opacity-50" value={task.progress || 100} max="100"></progress>
        {:else}
          <progress class="progress opacity-50" value={task.progress || 0} max="100"></progress>
        {/if}
        <div class="flex flex-wrap justify-between gap-2 text-xs opacity-50">
          <span>{formatProgress(task)}</span>
          <span>
            {task.started_at ? $dateTime(task.started_at) : task.finished_at ? $dateTime(task.finished_at) : ''}
          </span>
        </div>
      </div>
    </Cell>
    <Cell class="max-lg:hidden">
      <Badge icon={state.icon} iconColor={state.iconColor} iconClass={task.state === 'running' ? 'animate-spin' : ''}>
        {$_(state.label)}
      </Badge>
    </Cell>
    <Cell class="max-lg:hidden" text={task.encoded_size_text} />
    <Cell
      actions={[
        {
          condition: task.state === 'running',
          loading: loadingIds.has(task.id),
          class: '[&_iconify-icon]:text-surface/80',
          icon: icons.pauseFilled,
          text: $_('action.stop', $_('entity.task')),
          onclick: () => stopTask(task)
        },
        {
          condition: deletable,
          loading: loadingIds.has(task.id),
          icon: icons.delete,
          text: $_('action.delete', $_('entity.task')),
          onclick: () => confirmDelete(task)
        }
      ]}
    />
  {/snippet}
</DataView>
