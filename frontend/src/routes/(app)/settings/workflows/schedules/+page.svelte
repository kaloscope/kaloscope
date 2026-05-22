<script lang="ts">
  import { api } from '$lib/api';
  import {
    Badge,
    Button,
    Cell,
    confirm,
    DataView,
    HCell,
    JobEditor,
    Paginator,
    Search,
    Select,
    type PaginatorProps
  } from '$lib/components';
  import { enumToOptions, JobState, JobTrigger, IntervalUnit } from '$lib/enums';
  import { createLoading, createSortField } from '$lib/helpers';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { FlowJob, OptionValue, Page, Resp } from '$lib/types';
  import { toDatetimeLocal } from '$lib/utils';
  import { tick, untrack } from 'svelte';

  let jobs: FlowJob[] = $state([]);
  let jobName: string = $state('');
  let jobState: OptionValue = $state(null);
  let jobTrigger: OptionValue = $state(null);
  let creator: JobEditor | null = $state(null);
  let updater: JobEditor | null = $state(null);
  let selected: FlowJob | null = $state(null);

  let retryCount = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const pagination: PaginatorProps = $state({ current: 1, size: 15, onchange: search });
  const ordering = createSortField();
  const loading = createLoading();

  /**
   * Search for flow jobs.
   *
   * @param page - The page number.
   * @param size - The page size.
   * @param retry - Whether this search is a retry attempt.
   */
  function search(page: number = 1, size: number = pagination.size, retry: boolean = false) {
    if (!retry) {
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      retryCount = 0;
    }
    loading.start();
    api
      .get('flow/job/list', {
        searchParams: {
          page_num: page,
          page_size: size,
          ordering: $ordering,
          trigger: jobTrigger ?? '',
          state: jobState ?? '',
          name: jobName
        }
      })
      .json<Resp<Page<FlowJob>>>()
      .then((resp) => {
        pagination.current = page;
        pagination.size = size;
        pagination.total = resp.data.total;
        jobs = resp.data.items;
        // if any job is pending, retry search after some time
        if (jobs.some((job) => !JobState[job.state]) && retryCount < 5) {
          retryTimer = setTimeout(
            () => {
              retryTimer = null;
              search(page, size, true);
            },
            1000 * Math.pow(2, retryCount)
          );
          retryCount++;
        } else {
          retryCount = 0;
        }
      })
      .finally(() => loading.end());
  }

  /**
   * Pause a flow job by ID.
   *
   * @param id - The flow job ID.
   */
  function pause(id: number) {
    loading.start();
    api
      .post(`flow/job/${id}/pause`)
      .then(() => search(pagination.current))
      .catch(() => loading.end());
  }

  /**
   * Resume a flow job by ID.
   *
   * @param id - The flow job ID.
   */
  function resume(id: number) {
    loading.start();
    api
      .post(`flow/job/${id}/resume`)
      .then(() => search(pagination.current))
      .catch(() => loading.end());
  }

  /**
   * Delete a flow job by ID.
   *
   * @param id - The flow job ID.
   */
  function del(id: number) {
    loading.start();
    api
      .post(`flow/job/${id}/delete`)
      .then(() => search(pagination.current))
      .catch(() => loading.end());
  }

  /**
   * Format the trigger description for display.
   *
   * @param job - The flow job.
   */
  function triggerDesc(job: FlowJob): string {
    switch (job.trigger) {
      case 'date':
        return $dateTime(job.run_date);
      case 'cron':
        return job.cron_expr ?? '';
      case 'interval': {
        if (!job.interval_num || !job.interval_unit) {
          return '';
        }
        const unit = IntervalUnit[job.interval_unit];
        return $_('field.every', `${job.interval_num} ${$_(unit.label).toLowerCase()}`);
      }
      default:
        return '';
    }
  }

  $effect(() => {
    $ordering; // eslint-disable-line
    untrack(() => search());
  });
</script>

<DataView dvh loading={$loading} data={jobs}>
  {#snippet filters()}
    <Select
      filter
      options={enumToOptions(JobTrigger)}
      bind:value={jobTrigger}
      label={$_('field.trigger')}
      onchange={() => search()}
      class="max-md:hidden"
    />
    <Select
      filter
      options={enumToOptions(JobState)}
      bind:value={jobState}
      label={$_('field.state')}
      onchange={() => search()}
      class="max-lg:hidden"
    />
    <Search label={$_('field.name')} bind:value={jobName} onsearch={() => search()} />
  {/snippet}
  {#snippet actions()}
    <Button
      size="md"
      icon={icons.addCircle}
      text={$_('action.add', $_('entity.job'))}
      onclick={() => creator?.showModal()}
    />
  {/snippet}
  {#snippet header()}
    <HCell width={['15%', '50%']} text={$_('field.graph')} sort={ordering.bind('graph__name')} />
    <HCell width={['25%', '50%']} text={$_('field.trigger')} sort={ordering.bind('trigger')} />
    <HCell width={['25%', null]} text={$_('field.description')} />
    <HCell width={['20%', null]} text={$_('field.updated')} sort={ordering.bind('updated_at')} />
    <HCell width={['15%', '4rem']} text={$_('field.state')} sort={ordering.bind('state')} />
    <HCell actions />
  {/snippet}
  {#snippet row(job)}
    <Cell>
      <div class="truncate">
        <div class="mb-2 truncate font-medium" title={job.graph_name}>{job.graph_name}</div>
        <div class="truncate text-xs opacity-50">ID: {job.id}</div>
      </div>
    </Cell>
    <Cell>
      {@const trigger = JobTrigger[job.trigger]}
      <Badge icon={trigger.icon} iconColor={trigger.iconColor}>{$_(trigger.label)}</Badge>
    </Cell>
    <Cell text={triggerDesc(job)} />
    <Cell text={$dateTime(job.updated_at)} />
    <Cell>
      {@const state = JobState[job.state]}
      {#if state}
        <Badge icon={state.icon} iconColor={state.iconColor}>
          <span class="max-xl:hidden">{$_(state.label)}</span>
        </Badge>
      {:else}
        <span class="loading loading-xs loading-spinner"></span>
      {/if}
    </Cell>
    <Cell
      actions={[
        {
          icon: icons.edit,
          text: $_('action.edit', $_('entity.job')),
          onclick: () => {
            selected = job;
            tick().then(() => updater?.showModal());
          }
        },
        {
          condition: job.state === 'running',
          icon: icons.pause,
          text: $_('action.pause', $_('entity.job')),
          onclick: () => {
            confirm({
              icon: icons.pause,
              title: `${$_('action.pause', $_('entity.job'))} [${job.id}]`,
              onconfirm: () => pause(job.id)
            });
          }
        },
        {
          condition: job.state === 'paused',
          icon: icons.play,
          text: $_('action.resume', $_('entity.job')),
          onclick: () => {
            confirm({
              icon: icons.play,
              title: `${$_('action.resume', $_('entity.job'))} [${job.id}]`,
              onconfirm: () => resume(job.id)
            });
          }
        },
        {
          icon: icons.deleteDismiss,
          text: $_('action.delete', $_('entity.job')),
          onclick: () => {
            confirm({
              icon: icons.deleteDismiss,
              title: `${$_('action.delete', $_('entity.job'))} [${job.id}]`,
              onconfirm: () => del(job.id)
            });
          }
        }
      ]}
    />
  {/snippet}
  {#snippet paginator(disabled)}
    <Paginator {disabled} {...pagination} />
  {/snippet}
</DataView>

<JobEditor bind:this={creator} onsave={() => search()} />

{#if selected}
  <JobEditor
    bind:this={updater}
    {...selected}
    run_date={toDatetimeLocal(selected.run_date)}
    interval_start={toDatetimeLocal(selected.interval_start)}
    interval_end={toDatetimeLocal(selected.interval_end)}
    onsave={() => search(pagination.current)}
  />
{/if}
