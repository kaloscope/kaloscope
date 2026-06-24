<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
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
    confirm,
    type PaginatorProps
  } from '$lib/components';
  import { GraphCategory, GraphState, enumToOptions } from '$lib/enums';
  import { createLoading, createSortField } from '$lib/helpers';
  import { _, dateTime, milliseconds } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { FlowGraph, FlowTemplate, OptionValue, Page, Resp } from '$lib/types';
  import { untrack } from 'svelte';

  let graphs: FlowGraph[] = $state([]);
  let graphName: string = $state('');
  let graphState: OptionValue = $state(null);
  let graphCategory: OptionValue = $state(null);
  let graphEditor: GraphEditor;
  let headerCheckbox: Checkbox;
  let zipInput: HTMLInputElement;
  let flowLogs: FlowLogs;

  const pagination: PaginatorProps = $state({ current: 1, size: 15, onchange: search });
  const ordering = createSortField();
  const loading = createLoading();

  /**
   * Search for flow graphs.
   *
   * @param page - The page number.
   * @param size - The page size.
   */
  function search(page: number = 1, size: number = pagination.size) {
    loading.start();
    api
      .get('flow/graph/list', {
        searchParams: {
          page_num: page,
          page_size: size,
          ordering: $ordering,
          category: graphCategory ?? '',
          state: graphState ?? '',
          name: graphName
        }
      })
      .json<Resp<Page<FlowGraph>>>()
      .then(({ data }) => {
        pagination.current = page;
        pagination.size = size;
        pagination.total = data.total;
        graphs = data.items;
      })
      .finally(() => {
        loading.end();
        headerCheckbox.unselectAll();
      });
  }

  /**
   * Retract a flow graph by ID.
   *
   * @param id - The flow graph ID.
   */
  function retract(id: number) {
    loading.start();
    api
      .post(`flow/graph/${id}/retract`)
      .then(() => search(pagination.current))
      .catch(() => loading.end());
  }

  /**
   * Delete a flow graph by ID.
   *
   * @param id - The flow graph ID.
   */
  function del(id: number) {
    loading.start();
    api
      .post('flow/graph/delete', { json: { ids: [id] } })
      .then(() => search(pagination.current))
      .catch(() => loading.end());
  }

  /**
   * Upgrade a flow graph by ID.
   *
   * @param id - The flow graph ID.
   * @param tmpl - The newest template to reference.
   */
  function upgrade(id: number, tmpl: FlowTemplate) {
    loading.start();
    api
      .post(`flow/graph/${id}/upgrade`, { json: tmpl })
      .then(() => {
        alert({ level: 'success', message: 'upgrade_success' });
        search(pagination.current);
      })
      .catch(() => loading.end());
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
    loading.start();
    api
      .post('flow/graph/export', { json: { ids: selectedKeys } })
      .blob()
      .then((blob) => {
        if (blob.size === 0) {
          alert({ level: 'error', message: 'export_items_failed' });
          return;
        }
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${new Date().getTime()}.zip`;
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      })
      .finally(() => loading.end());
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
    loading.start();
    api
      .post('flow/graph/import', { body: formData })
      .json<Resp<number>>()
      .then(({ data }) => {
        graphName = '';
        graphState = null;
        graphCategory = null;
        search();
        if (data) {
          alert({ level: 'success', message: $_('alert.import_items_success', data) });
        }
      })
      .catch(() => loading.end());
  }

  $effect(() => {
    $ordering; // eslint-disable-line
    untrack(() => search());
  });
</script>

<DataView dvh rowSelect loading={$loading} data={graphs}>
  {#snippet filters()}
    <Select
      filter
      options={enumToOptions(GraphCategory)}
      bind:value={graphCategory}
      label={$_('field.category')}
      onchange={() => search()}
      class="max-md:hidden"
    />
    <Select
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
          icon: icons.slideSearch,
          text: $_('action.view', $_('entity.logs')),
          onclick: () => flowLogs.showModal(graph.id)
        }
      ]}
    />
  {/snippet}
  {#snippet paginator(disabled)}
    <Paginator {disabled} {...pagination} />
  {/snippet}
</DataView>

<GraphEditor bind:this={graphEditor} onsave={(result) => goto(`${page.url.pathname}/${result.id}`)} />

<FlowLogs bind:this={flowLogs} />
