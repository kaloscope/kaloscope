<script lang="ts">
  import { api } from '$lib/api';
  import {
    Button,
    Cell,
    confirm,
    DataView,
    HCell,
    Paginator,
    PlanEditor,
    Search,
    Select,
    Status,
    type PaginatorProps
  } from '$lib/components';
  import { createLoading, createSortField } from '$lib/helpers';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { DownloadPlan, FlowGraph, Page, Resp } from '$lib/types';
  import { toDatetimeLocal } from '$lib/utils';
  import { onMount, tick, untrack } from 'svelte';

  let plans: DownloadPlan[] = $state([]);
  let keyword: string = $state('');
  let graph: number | null = $state(null);
  let graphs: FlowGraph[] = $state([]);
  let creator: PlanEditor | null = $state(null);
  let updater: PlanEditor | null = $state(null);
  let selected: DownloadPlan | null = $state(null);

  const pagination: PaginatorProps = $state({ current: 1, size: 15, onchange: search });
  const ordering = createSortField();
  const loading = createLoading();

  /**
   * Search for download plans.
   *
   * @param page - The page number.
   * @param size - The page size.
   */
  function search(page: number = 1, size: number = pagination.size) {
    loading.start();
    api
      .get('download/plan/list', {
        searchParams: {
          page_num: page,
          page_size: size,
          ordering: $ordering,
          graph_id: graph || 0,
          keyword: keyword
        }
      })
      .json<Resp<Page<DownloadPlan>>>()
      .then((resp) => {
        pagination.current = page;
        pagination.size = size;
        pagination.total = resp.data.total;
        plans = resp.data.items;
      })
      .finally(() => loading.end());
  }

  /**
   * Delete a download plan by ID.
   *
   * @param id - The download plan ID.
   */
  function del(id: number) {
    loading.start();
    api
      .post('download/plan/delete', { json: { ids: [id] } })
      .then(() => search(pagination.current))
      .catch(() => loading.end());
  }

  $effect(() => {
    $ordering; // eslint-disable-line
    untrack(() => search());
  });

  onMount(() => {
    api
      .get('flow/graph/list', {
        searchParams: [
          ['page_num', 0],
          ['ordering', 'name'],
          ['category', 'indexer'],
          ['states', 'modified'],
          ['states', 'published']
        ]
      })
      .json<Resp<Page<FlowGraph>>>()
      .then((resp) => {
        const items = resp.data.items;
        graphs = items.filter((g) => g.node_types.includes('search_start') && !g.only_preview);
      });
  });
</script>

<DataView dvh loading={$loading} data={plans}>
  {#snippet filters()}
    <Select
      filter
      options={[{ value: '', label: 'enum.all' }, ...graphs.map((d) => ({ value: d.id, label: d.name }))]}
      bind:value={graph}
      label={$_('field.graph')}
      onchange={() => search()}
    />
    <Search label={$_('field.keyword')} bind:value={keyword} onsearch={() => search()} />
  {/snippet}
  {#snippet actions()}
    <Button
      size="md"
      icon={icons.addCircle}
      text={$_('action.add', $_('entity.plan'))}
      onclick={() => creator?.showModal()}
    />
  {/snippet}
  {#snippet header()}
    <HCell width={['15%', '50%']} text={$_('field.graph')} sort={ordering.bind('graph__name')} />
    <HCell width={['20%', '50%']} text={$_('field.keyword')} sort={ordering.bind('keyword')} />
    <HCell width={['20%', null]} text={$_('field.interval')} sort={ordering.bind('interval_num')} />
    <HCell width={['20%', null]} text={$_('field.last_exec')} sort={ordering.bind('last_exec')} />
    <HCell width={['15%', null]} text={$_('field.total_count')} />
    <HCell width={['10%', '4rem']} text={$_('field.status')} />
    <HCell actions />
  {/snippet}
  {#snippet row(plan)}
    <Cell>
      <div class="truncate">
        <div class="mb-2 truncate font-medium" title={plan.graph_name}>{plan.graph_name}</div>
        <div class="truncate text-xs opacity-50">ID: {plan.id}</div>
      </div>
    </Cell>
    <Cell text={plan.keyword} />
    <Cell text={$_('field.every', `${plan.interval_num} ${$_('duration.hours').toLowerCase()}`)} />
    <Cell text={$dateTime(plan.last_exec)} />
    <Cell text={`${plan.total_count}${plan.total_limit ? ` / ${plan.total_limit}` : ''}`} />
    <Cell>
      <Status rate={plan.running ? 1 : null} class="px-2" />
    </Cell>
    <Cell
      actions={[
        {
          icon: icons.edit,
          text: $_('action.edit', $_('entity.plan')),
          onclick: () => {
            selected = plan;
            tick().then(() => updater?.showModal());
          }
        },
        {
          icon: icons.deleteDismiss,
          text: $_('action.delete', $_('entity.plan')),
          onclick: () => {
            confirm({
              icon: icons.deleteDismiss,
              title: `${$_('action.delete', $_('entity.plan'))} [${plan.id}]`,
              onconfirm: () => del(plan.id)
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

<PlanEditor bind:this={creator} onsave={() => search()} />

{#if selected}
  <PlanEditor
    bind:this={updater}
    {...selected}
    interval_start={toDatetimeLocal(selected.interval_start)}
    interval_end={toDatetimeLocal(selected.interval_end)}
    onsave={() => search(pagination.current)}
  />
{/if}
