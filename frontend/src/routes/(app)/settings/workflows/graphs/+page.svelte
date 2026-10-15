<script lang="ts" module>
  import type { PaginatorProps } from '$lib/components';
  import { createSortField } from '$lib/helpers';
  import type { FlowGraph, OptionValue, ScrollPosition } from '$lib/types';

  import type { GraphUpdate } from '$lib/components/business/flow/graphWrites';

  type ListQuery = {
    promise: Promise<void>;
    updates: Record<number, GraphUpdate>;
  };

  type ListData = {
    graphs: FlowGraph[];
    pagination: Pick<PaginatorProps, 'current' | 'size' | 'total'>;
    requestedPage: number;
    query: ListQuery | null;
    refreshes: Partial<Record<number, Promise<void>>>;
    mutations: Partial<Record<number, Promise<void>>>;
    searchVersion: number;
    revision: number;
    actions: number;
    reload: 'current' | 'first' | null;
  };

  type SavedList = {
    list: ListData;
    editorPath: string;
    graphId: number;
    userId: number | undefined;
    sessionToken: string | null;
    name: string;
    state: OptionValue;
    category: OptionValue;
    ordering: ReturnType<typeof createSortField>;
    position: ScrollPosition;
  };

  let savedList: SavedList | undefined;

  /**
   * Merge changed graph fields into their existing row without reordering it.
   *
   * @param graphs - Rows to update.
   * @param updated - Changed flow graph fields, including its ID.
   */
  function mergeGraph(graphs: FlowGraph[], updated: GraphUpdate) {
    const index = graphs.findIndex((item) => item.id === updated.id);
    if (index !== -1) {
      const graph = { ...graphs[index], ...updated };
      if (graph.newest_tmpl?.id === graph.tmpl_id) {
        graph.newest_tmpl = null;
      }
      graphs[index] = graph;
    }
  }
</script>

<script lang="ts">
  import { beforeNavigate, goto, replaceState } from '$app/navigation';
  import { navigating, page } from '$app/state';
  import { api } from '$lib/api';
  import {
    Badge,
    Button,
    Cell,
    Checkbox,
    DataView,
    FlowLogs,
    GraphEditor,
    HCell,
    Image,
    Paginator,
    Search,
    Select,
    Status,
    alert,
    confirm
  } from '$lib/components';
  import { setGraphListUpdater, trackGraphWrite, waitForGraphWrites } from '$lib/components/business/flow/graphWrites';
  import { GraphCategory, GraphState, enumToOptions } from '$lib/enums';
  import { createLoading } from '$lib/helpers';
  import { _, dateTime, milliseconds } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { token, user } from '$lib/stores';
  import type { FlowTemplate, Page, Resp } from '$lib/types';
  import { downloadBlob } from '$lib/utils/browser';
  import { onMount, tick, untrack } from 'svelte';

  const sessionToken = untrack(() => $token);
  const restoredList = untrack(() => {
    const saved = savedList;
    savedList = undefined;
    return saved &&
      saved.editorPath === navigating.from?.url.pathname &&
      saved.userId === $user?.id &&
      saved.sessionToken === sessionToken
      ? saved
      : undefined;
  });

  const list: ListData = $state(
    restoredList?.list ?? {
      graphs: [],
      pagination: { current: 1, size: 15 },
      requestedPage: 1,
      query: null,
      refreshes: {},
      mutations: {},
      searchVersion: 0,
      revision: 0,
      actions: 0,
      reload: null
    }
  );
  const graphs = $derived(list.graphs);
  let graphName: string = $state(restoredList?.name ?? '');
  let graphState: OptionValue = $state(restoredList?.state ?? null);
  let graphCategory: OptionValue = $state(restoredList?.category ?? null);
  let newGraphCategory: keyof typeof GraphCategory | undefined = $state();
  let dataView: DataView<FlowGraph>;
  let graphEditor: GraphEditor;
  let headerCheckbox: Checkbox;
  let zipInput: HTMLInputElement;
  let flowLogs: FlowLogs;

  const pagination = $derived(list.pagination);
  const ordering = restoredList?.ordering ?? createSortField();
  const loading = createLoading();
  const busy = $derived(
    !!list.query || Object.keys(list.refreshes).length > 0 || Object.keys(list.mutations).length > 0 || list.actions > 0
  );
  let skipInitialSearch = !!restoredList;
  let disposed = false;

  setGraphListUpdater(updateGraph);

  /**
   * Merge updated graph data into its existing row without changing the list order.
   *
   * @param updated - Changed flow graph fields, including its ID.
   */
  function updateGraph(updated: GraphUpdate) {
    mergeGraph(list.graphs, updated);
    if (list.query) {
      list.query.updates[updated.id] = { ...list.query.updates[updated.id], ...updated };
    }
    list.revision++;
  }

  /**
   * Refresh only the requested graph in the current list.
   *
   * @param id - Flow graph ID.
   * @returns A promise that resolves after the row is updated.
   */
  function refreshGraph(id: number): Promise<void> {
    const version = list.searchVersion;
    const request: Promise<void> = Promise.all([list.query?.promise, list.mutations[id], waitForGraphWrites(id)])
      .then(async () => {
        if (version === list.searchVersion && list.refreshes[id] === request) {
          const graph = list.graphs.find((item) => item.id === id);
          const { data } = await api.get(`flow/graph/${id}`).json<Resp<FlowGraph>>();
          if (
            version === list.searchVersion &&
            list.refreshes[id] === request &&
            graph === list.graphs.find((item) => item.id === id)
          ) {
            updateGraph(data);
          }
        }
      })
      .catch(() => {})
      .finally(() => {
        if (list.refreshes[id] === request) {
          delete list.refreshes[id];
        }
      });
    list.refreshes[id] = request;
    return request;
  }

  /**
   * Retain graph mutations across navigation and merge them into pending queries.
   *
   * @param id - Flow graph ID.
   * @param operation - Request that returns changed graph fields and honors session cancellation.
   */
  function mutateGraph(id: number, operation: (signal: AbortSignal) => Promise<GraphUpdate>) {
    delete list.refreshes[id];
    const request = trackGraphWrite(id, operation)
      .then(() => {})
      .catch(() => {})
      .finally(() => {
        if (list.mutations[id] === request) {
          delete list.mutations[id];
        }
      });
    list.mutations[id] = request;
  }

  /**
   * Search for flow graphs.
   *
   * @param page - Page number.
   * @param size - Page size.
   */
  function search(page: number = 1, size: number = pagination.size) {
    if (disposed) {
      return;
    }
    list.searchVersion++;
    list.requestedPage = page;
    list.refreshes = {};
    list.query = { promise: Promise.resolve(), updates: {} };
    const query = list.query;
    const searchParams = {
      page_num: page,
      page_size: size,
      ordering: $ordering,
      category: graphCategory ?? '',
      state: graphState ?? '',
      name: graphName
    };
    query.promise = waitForGraphWrites()
      .then(() => {
        if (list.query === query) {
          return api.get('flow/graph/list', { searchParams }).json<Resp<Page<FlowGraph>>>();
        }
      })
      .then((response) => {
        if (response && list.query === query) {
          const { data } = response;
          for (const updated of Object.values(query.updates)) {
            mergeGraph(data.items, updated);
          }
          list.graphs = data.items;
          Object.assign(list.pagination, { current: page, size, total: data.total });
          list.revision++;
        }
      })
      .catch(() => {})
      .finally(() => {
        if (list.query === query) {
          list.query = null;
          list.requestedPage = list.pagination.current;
        }
      });
  }

  /**
   * Retract a flow graph by ID.
   *
   * @param id - Flow graph ID.
   */
  function retract(id: number) {
    mutateGraph(id, (signal) =>
      api.post(`flow/graph/${id}/retract`, { signal }).then(() => ({ id, state: 'draft' as const }))
    );
  }

  /**
   * Delete a flow graph by ID.
   *
   * @param id - Flow graph ID.
   */
  function del(id: number) {
    list.actions++;
    trackGraphWrite(id, (signal) => api.post('flow/graph/delete', { json: { ids: [id] }, signal }).then(() => {}))
      .then(() => (list.reload ??= 'current'))
      .catch(() => {})
      .finally(() => list.actions--);
  }

  /**
   * Upgrade a flow graph by ID.
   *
   * @param id - Flow graph ID.
   * @param tmpl - Newest template to reference.
   */
  function upgrade(id: number, tmpl: FlowTemplate) {
    mutateGraph(id, (signal) =>
      api
        .post(`flow/graph/${id}/upgrade`, { json: tmpl, signal })
        .json<Resp<FlowGraph>>()
        .then(({ data }) => {
          alert({ level: 'success', message: 'upgrade_success' });
          return data;
        })
    );
  }

  /**
   * Export the flow graphs as a zip file.
   */
  function exportGraphs() {
    const selectedKeys = headerCheckbox.getSelectedKeys();
    if (selectedKeys.length === 0) {
      alert({ message: 'select_export_items' });
      return;
    }
    list.actions++;
    api
      .post('flow/graph/export', { json: { ids: selectedKeys } })
      .blob()
      .then((blob) => {
        if (blob.size === 0) {
          alert({ level: 'error', message: 'export_items_failed' });
          return;
        }
        downloadBlob(blob, `${new Date().getTime()}.zip`);
      })
      .catch(() => {})
      .finally(() => list.actions--);
  }

  /**
   * Import the flow graphs from a zip file.
   */
  function importGraphs() {
    const zip = zipInput.files?.[0];
    if (!zip) {
      return;
    }
    const formData = new FormData();
    formData.append('zip', zip);
    zipInput.value = '';
    list.actions++;
    api
      .post('flow/graph/import', { body: formData })
      .json<Resp<number>>()
      .then(({ data }) => {
        list.reload = 'first';
        if (data) {
          alert({ level: 'success', message: $_('alert.import_items_success', data) });
        }
      })
      .catch(() => {})
      .finally(() => list.actions--);
  }

  beforeNavigate(({ to }) => {
    if (to?.route.id !== '/(app)/settings/workflows/graphs/[[mode]]/[graph_id]') {
      return;
    }
    const graph = graphs.find((item) => item.id === Number(to.params?.graph_id));
    if (!graph) {
      return;
    }
    savedList = {
      list,
      editorPath: to.url.pathname,
      graphId: graph.id,
      userId: $user?.id,
      sessionToken,
      name: graphName,
      state: graphState,
      category: graphCategory,
      ordering,
      position: dataView.scrollPosition()
    };
  });

  onMount(() => {
    if (restoredList) {
      refreshGraph(restoredList.graphId);
      tick().then(() => {
        if (!disposed) {
          dataView.scrollTo(restoredList.position);
        }
      });
    }
    if (page.state.createScheduledGraph) {
      newGraphCategory = 'schedule';
      replaceState('', { ...page.state, createScheduledGraph: false });
      graphEditor.showModal();
    }
    return () => {
      disposed = true;
    };
  });

  $effect(() => {
    if (list.reload) {
      untrack(() => {
        const first = list.reload === 'first';
        list.reload = null;
        if (first) {
          graphName = '';
          graphState = null;
          graphCategory = null;
        }
        search(first ? 1 : list.requestedPage);
      });
    }
  });

  $effect(() => {
    if (busy) {
      loading.start();
    } else {
      loading.end();
    }
  });

  $effect(() => {
    list.revision; // eslint-disable-line @typescript-eslint/no-unused-expressions
    untrack(() => headerCheckbox?.unselectAll());
  });

  $effect(() => {
    $ordering; // eslint-disable-line
    untrack(() => {
      if (skipInitialSearch) {
        skipInitialSearch = false;
      } else {
        search();
      }
    });
  });
</script>

<DataView dvh rowSelect loading={$loading} data={graphs} uniqueKey="id" bind:this={dataView}>
  {#snippet filters()}
    <Select
      translate
      filter
      options={enumToOptions(GraphCategory)}
      bind:value={graphCategory}
      label={$_('field.category')}
      onchange={() => search()}
      class="max-md:hidden"
    />
    <Select
      translate
      filter
      options={enumToOptions(GraphState)}
      bind:value={graphState}
      label={$_('field.state')}
      onchange={() => search()}
      class="max-lg:hidden"
    />
    <Search label={$_('field.name')} bind:value={graphName} onsearch={() => search()} />
  {/snippet}
  {#snippet actions()}
    <Button
      size="md"
      icon={icons.boxArrowUp}
      text={$_('action.export', $_('entity.graphs'))}
      onclick={() => exportGraphs()}
    />
    <Button
      size="md"
      icon={icons.layerDiagonalAdd}
      text={$_('action.import', $_('entity.graphs'))}
      onclick={() => zipInput.click()}
    />
    <input type="file" class="hidden" accept="application/zip" bind:this={zipInput} onchange={importGraphs} />
    <Button
      size="md"
      icon={icons.documentAdd}
      text={$_('action.add', $_('entity.graph'))}
      onclick={() => graphEditor.showModal()}
    />
  {/snippet}
  {#snippet header()}
    <HCell width="2rem">
      <Checkbox batch={graphs.filter((g) => g.state !== 'draft').length} bind:this={headerCheckbox} />
    </HCell>
    <HCell width={['30%', '55%']} text={$_('field.name')} sort={ordering.bind('name')} />
    <HCell width={['15%', '45%']} text={$_('field.category')} sort={ordering.bind('category')} />
    <HCell width={['20%', null]} text={$_('field.updated')} sort={ordering.bind('updated_at')} />
    <HCell width={['20%', null]} text={$_('field.average_time')} />
    <HCell width={['15%', '4rem']} text={$_('field.state')} sort={ordering.bind('state')} />
    <HCell actions />
  {/snippet}
  {#snippet row(graph)}
    {@const draft = graph.state === 'draft'}
    <Cell>
      <Checkbox key={String(graph.id)} disabled={draft} />
    </Cell>
    <Cell>
      {@const nameClass = 'mb-2 flex max-w-fit items-center gap-1 **:-mb-1'}
      <Image transparent src={graph.icon} icon={icons.documentFlowchart} class="max-sm:hidden" />
      <div class="truncate">
        {#if graph.tmpl && !graph.editable}
          <!-- svelte-ignore a11y_click_events_have_key_events -->
          <div
            tabindex="0"
            role="button"
            class="{graph.newest_tmpl ? 'hover-link' : 'pb-px'} {nameClass}"
            title={graph.name}
            onclick={() => {
              if (!graph.newest_tmpl) {
                return;
              }
              confirm({
                icon: icons.arrowBigUp,
                title: `${$_('action.upgrade', $_('entity.graph'))} [${graph.name}]`,
                message: $_('flow.tmpl.confirm_upgrade'),
                onconfirm: () => upgrade(graph.id, graph.newest_tmpl!)
              });
            }}
          >
            <iconify-icon icon={icons.link}></iconify-icon>
            <span class="truncate">{graph.name}</span>
          </div>
        {:else if !graph.editable}
          <div class="opacity-70 {nameClass}" title={graph.name}>
            <iconify-icon icon={icons.unlink}></iconify-icon>
            <span class="truncate">{graph.name}</span>
          </div>
        {:else}
          <div class={nameClass} title={graph.name}>
            <span class="truncate">{graph.name}</span>
          </div>
        {/if}
        <div class="truncate text-xs opacity-50" title={graph.description}>{graph.description}</div>
      </div>
    </Cell>
    <Cell>
      {@const category = GraphCategory[graph.category]}
      <Badge icon={category.icon} iconColor={category.iconColor}>{$_(category.label)}</Badge>
    </Cell>
    <Cell text={$dateTime(graph.updated_at)} />
    <Cell class="max-lg:hidden">
      <Status rate={graph.success_rate} />
      {$milliseconds(graph.average_time)}
    </Cell>
    <Cell>
      {@const state = GraphState[graph.state]}
      <Badge icon={state.icon} iconColor={state.iconColor} dashed={draft}>
        <span class="max-xl:hidden">{$_(state.label)}</span>
      </Badge>
    </Cell>
    <Cell
      actions={[
        {
          icon: icons.documentEdit,
          text: $_('action.edit', $_('entity.graph')),
          onclick: () => goto(`/settings/workflows/graphs/${graph.editable ? '' : 'r/'}${graph.id}`)
        },
        {
          condition: !draft,
          icon: icons.back,
          text: $_('action.retract', $_('entity.graph')),
          onclick: () => {
            confirm({
              icon: icons.back,
              title: `${$_('action.retract', $_('entity.graph'))} [${graph.name}]`,
              onconfirm: () => retract(graph.id)
            });
          }
        },
        {
          condition: draft,
          icon: icons.deleteDismiss,
          text: $_('action.delete', $_('entity.graph')),
          onclick: () => {
            confirm({
              icon: icons.deleteDismiss,
              title: `${$_('action.delete', $_('entity.graph'))} [${graph.name}]`,
              onconfirm: () => del(graph.id)
            });
          }
        },
        {
          condition: !!graph.last_exec,
          icon: icons.documentSearch,
          text: $_('action.view', $_('entity.logs')),
          onclick: () => flowLogs.showModal(graph.id)
        }
      ]}
    />
  {/snippet}
  {#snippet paginator(disabled)}
    <Paginator {disabled} {...pagination} bind:current={list.requestedPage} onchange={search} />
  {/snippet}
</DataView>

<GraphEditor
  bind:this={graphEditor}
  category={newGraphCategory}
  onsave={(result) => goto(`${page.url.pathname}/${result.id}`)}
/>

<FlowLogs bind:this={flowLogs} />
