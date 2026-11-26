<script lang="ts">
  import { api } from '$lib/api';
  import { registerAssistantContext } from '$lib/assistant/context';
  import { downloadPlansPath } from '$lib/assistant/navigation';
  import {
    Button,
    Cell,
    confirm,
    DataView,
    DownloaderEmpty,
    Empty,
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
  import { user } from '$lib/stores.svelte';
  import type { AssistantPageContext, Downloader, DownloadPlan, FlowGraph, Page, Resp } from '$lib/types';
  import { describePlanExecution } from '$lib/utils/plan-execution';
  import { toDatetimeLocal } from '$lib/utils/datetime';
  import { onMount, tick, untrack } from 'svelte';

  let plans: DownloadPlan[] = $state([]);
  let keyword: string = $state('');
  let graph: number | null = $state(null);
  let graphs: FlowGraph[] = $state([]);
  let creator: PlanEditor | null = $state(null);
  let updater: PlanEditor | null = $state(null);
  let selected: DownloadPlan | null = $state(null);
  // whether the downloader list has loaded without any entry; every plan needs a downloader
  let noDownloader: boolean = $state(false);

  const pagination: PaginatorProps = $state({ current: 1, size: 15, onchange: search });
  const ordering = createSortField();
  const loading = createLoading();

  type PlanFilters = Extract<AssistantPageContext, { page_type: 'download_plans' }>['filters'];

  // retain the filters of the displayed results, independently of unsubmitted input
  let displayed: PlanFilters | null = null;
  let listRequest: AbortController | null = null;

  /**
   * Search for download plans.
   *
   * @param page - Page number.
   * @param size - Page size.
   * @param base - Filters to reuse instead of the current inputs.
   * @param quiet - Refresh visible results without interrupting the user.
   */
  function search(
    page: number = 1,
    size: number = pagination.size,
    base: Omit<PlanFilters, 'page_num' | 'page_size'> | null = null,
    quiet = false
  ) {
    const filters = {
      ...(base ?? { workflow_id: graph || null, keyword, ordering: $ordering }),
      page_num: page,
      page_size: size
    };
    listRequest?.abort();
    const current = new AbortController();
    listRequest = current;
    if (!quiet) loading.start();
    api
      .get('download/plan/list', {
        signal: current.signal,
        context: { silentErrors: quiet },
        searchParams: {
          page_num: page,
          page_size: size,
          ordering: filters.ordering,
          graph_id: filters.workflow_id || 0,
          keyword: filters.keyword
        }
      })
      .json<Resp<Page<DownloadPlan>>>()
      .then(({ data }) => {
        if (current.signal.aborted) return;
        pagination.current = page;
        pagination.size = size;
        pagination.total = data.total;
        plans = data.items;
        displayed = filters;
      })
      .catch(() => {})
      .finally(() => {
        if (listRequest === current) {
          listRequest = null;
          loading.end();
        }
      });
  }

  /**
   * Delete a download plan by ID.
   *
   * @param id - Download plan ID.
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
    const refresh = () => search();
    /**
     * Reload the displayed page after an assistant change, keeping its filters.
     */
    const changed = () => {
      if (!displayed) return;
      const { page_num, page_size, ...base } = displayed;
      search(page_num, page_size, base);
    };
    window.addEventListener('download-plan-created', refresh);
    window.addEventListener('download-plan-changed', changed);
    let timer: ReturnType<typeof setTimeout>;
    /**
     * Refresh the results on screen without applying unsubmitted filters.
     */
    function poll(): void {
      if (!document.hidden && !listRequest && displayed) {
        const { page_num, page_size, ...base } = displayed;
        search(page_num, page_size, base, true);
      }
      const pending = plans.some((plan) =>
        plan.last_execution
          ? plan.last_execution.stage === 'completion_unknown'
          : plan.running && plan.first_execution?.status === 'queued'
      );
      timer = setTimeout(poll, pending ? 3000 : 30000);
    }
    timer = setTimeout(poll, 3000);
    const unregister = registerAssistantContext(() => {
      if (!displayed) return null;
      const current = plans.find((plan) => plan.id === selected?.id);
      return {
        page_type: 'download_plans',
        route: downloadPlansPath,
        filters: { ...displayed },
        selected_plan: current ? { plan_id: current.id, workflow_id: current.graph_id } : null
      };
    });
    api
      .get('download/manager/list')
      .json<Resp<Downloader[]>>()
      .then(({ data }) => {
        noDownloader = data.length === 0;
      });
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
      .then(({ data }) => {
        const items = data.items;
        graphs = items.filter((g) => g.node_types.includes('search_start') && !g.only_preview);
      });
    return () => {
      window.removeEventListener('download-plan-created', refresh);
      window.removeEventListener('download-plan-changed', changed);
      clearTimeout(timer);
      listRequest?.abort();
      unregister();
    };
  });
</script>

<DataView dvh loading={$loading} data={plans}>
  {#snippet empty()}
    {#if noDownloader}
      <DownloaderEmpty />
    {:else}
      <Empty />
    {/if}
  {/snippet}
  {#snippet filters()}
    <Select
      filter
      options={[{ value: '', label: $_('enum.all') }, ...graphs.map((d) => ({ value: d.id, label: d.name }))]}
      bind:value={graph}
      label={$_('field.graph')}
      onchange={() => search()}
    />
    <Search label={$_('field.keyword')} bind:value={keyword} onsearch={() => search()} />
  {/snippet}
  {#snippet actions()}
    {#if user.current?.role === 'admin'}
      <Button
        size="md"
        icon={icons.addCircle}
        text={$_('action.add', $_('entity.plan'))}
        onclick={() => creator?.showModal()}
      />
    {/if}
  {/snippet}
  {#snippet header()}
    <HCell width={['15%', '50%']} text={$_('field.graph')} sort={ordering.bind('graph__name')} />
    <HCell width={['20%', '50%']} text={$_('field.keyword')} sort={ordering.bind('keyword')} />
    <HCell width={['20%', null]} text={$_('field.interval')} sort={ordering.bind('interval_num')} />
    <HCell width={['20%', null]} text={$_('field.last_exec')} sort={ordering.bind('last_exec')} />
    <HCell width={['15%', null]} text={$_('field.total_count')} />
    <HCell width={['10%', '4rem']} text={$_('field.status')} />
    {#if user.current?.role === 'admin'}<HCell actions />{/if}
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
    <Cell>
      <div>
        <div>{$dateTime(plan.last_execution?.started_at ?? plan.last_exec)}</div>
        {#if plan.last_execution}
          <p class="mt-1 text-xs text-base-content/60">{describePlanExecution(plan.last_execution, $_)}</p>
        {:else if plan.last_exec}
          <p class="mt-1 text-xs text-base-content/60">{$_('assistant.plan.execution.unavailable')}</p>
        {/if}
      </div>
    </Cell>
    <Cell text={`${plan.total_count}${plan.total_limit ? ` / ${plan.total_limit}` : ''}`} />
    <Cell>
      <Status rate={plan.running ? 1 : null} class="px-2" />
    </Cell>
    {#if user.current?.role === 'admin'}<Cell
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
            icon: icons.delete,
            text: $_('action.delete', $_('entity.plan')),
            onclick: () => {
              confirm({
                icon: icons.delete,
                title: `${$_('action.delete', $_('entity.plan'))} [${plan.id}]`,
                onconfirm: () => del(plan.id)
              });
            }
          }
        ]}
      />{/if}
  {/snippet}
  {#snippet paginator(disabled)}
    <Paginator {disabled} {...pagination} />
  {/snippet}
</DataView>

{#if user.current?.role === 'admin'}
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
{/if}
