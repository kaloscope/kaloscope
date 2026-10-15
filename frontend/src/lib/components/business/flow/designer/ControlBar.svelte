<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { tooltip } from '$lib/actions';
  import { api } from '$lib/api';
  import { Badge, Button, GraphEditor, Image, PageHeader, alert, confirm } from '$lib/components';
  import { GraphState } from '$lib/enums';
  import { _, time } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { mediaQuery } from '$lib/stores';
  import type { FlowGraph, FlowGraphContext, Resp } from '$lib/types';
  import { debounce } from '$lib/utils/timing';
  import { MiniMap, useStore, type Edge, type Node } from '@xyflow/svelte';
  import { getContext, hasContext, onMount } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import { fade } from 'svelte/transition';
  import { v4 as uuidv4 } from 'uuid';
  import { trackGraphWrite } from '../graphWrites';

  let {
    nodes: _nodes = $bindable(),
    edges = $bindable(),
    graph,
    jsonGraph,
    readonly,
    interactive
  }: {
    nodes: Node[];
    edges: Edge[];
    graph: FlowGraph | null;
    jsonGraph: string | null;
    readonly: boolean;
    interactive: boolean;
  } = $props();

  let graphEditor: GraphEditor | null = $state(null);
  const minimap = mediaQuery('(min-width: 40rem)');
  const store = useStore();

  // ignore some properties of nodes to avoid unnecessary history records
  let nodes = $derived(_nodes.map((node) => ({ ...node, selected: false, measured: {} })));

  /**
   * Toggle on/off the interactivity.
   *
   * @param value - Specific value to set, or toggle if not provided.
   */
  function toggleInteractivity(value?: boolean) {
    interactive = value ?? !interactive;
    store.nodesDraggable = interactive;
    store.nodesConnectable = interactive;
    store.elementsSelectable = interactive;
  }

  /**
   * Toggle on/off the minimap.
   *
   * @param value - Specific value to set, or toggle if not provided.
   */
  function toggleMinimap(value?: boolean) {
    $minimap = value ?? !$minimap;
  }

  type History = { nodes: Node[]; edges: Edge[] };
  const histories: Map<string, History> = new SvelteMap<string, History>();
  let skipRecord: boolean = false;
  let historyId: string = $state('');
  let savedHistoryId: string = $state('');
  let firstHistoryId: string = $state('');
  let lastHistoryId: string = $state('');

  /**
   * Undo the previous operation.
   */
  function undo() {
    const keys = Array.from(histories.keys());
    const index = keys.indexOf(historyId);
    if (index > 0) {
      skipRecord = true;
      historyId = keys[index - 1];
      const record = $state.snapshot(histories.get(historyId) as unknown) as History;
      _nodes = record.nodes;
      edges = record.edges;
    }
  }

  /**
   * Redo the next operation.
   */
  function redo() {
    const keys = Array.from(histories.keys());
    const index = keys.indexOf(historyId);
    if (index < keys.length - 1) {
      skipRecord = true;
      historyId = keys[index + 1];
      const record = $state.snapshot(histories.get(historyId) as unknown) as History;
      _nodes = record.nodes;
      edges = record.edges;
    }
  }

  /**
   * Truncate the histories before saving a new history.
   */
  function truncateHistories() {
    // delete all histories after the current one
    let keys = Array.from(histories.keys());
    const index = keys.indexOf(historyId);
    if (index !== -1) {
      keys = keys.slice(index + 1);
      keys.forEach((key) => histories.delete(key));
    }
    // keep only the latest 10 histories
    if (histories.size > 10) {
      histories.delete(firstHistoryId);
      firstHistoryId = histories.keys().next().value as string;
    }
  }

  /**
   * Record the operation history.
   */
  const recordHistory = debounce((nodes: Node[], edges: Edge[]) => {
    if (graph === null || jsonGraph === null) {
      return;
    }
    if (skipRecord) {
      skipRecord = false;
      return;
    }
    // skip if the content is identical to the current history record
    const current = histories.get(historyId);
    if (current && JSON.stringify(current) === JSON.stringify({ nodes, edges })) {
      return;
    }
    // save the new history record and truncate the old histories if needed
    truncateHistories();
    historyId = uuidv4();
    if (histories.size === 0) {
      savedHistoryId = historyId;
      firstHistoryId = historyId;
    } else {
      lastHistoryId = historyId;
    }
    histories.set(historyId, $state.snapshot({ nodes: nodes, edges: edges } as unknown) as History);
  });

  $effect(() => {
    recordHistory(nodes, edges);
  });

  let autoSaveMode: boolean = $state(false);
  let autoSaveSignal: boolean = $state(false);
  let autoSaveCountdown: number = $state(0);
  let autoSaveInterval: ReturnType<typeof setInterval>;

  /**
   * Enable or disable the auto-save mode.
   *
   * @param mode - Whether to enable the auto-save mode.
   */
  function autoSave(mode: boolean) {
    autoSaveMode = mode;
    if (autoSaveMode) {
      autoSaveSignal = false;
      autoSaveCountdown = 0;
      autoSaveInterval = setInterval(() => {
        // countdown to save the graph or monitor the changes
        if (autoSaveCountdown > 0) {
          autoSaveCountdown--;
          return;
        }
        if (autoSaveSignal) {
          // save the graph
          save();
        } else {
          // monitor the changes
          autoSaveSignal = jsonGraph !== JSON.stringify({ nodes, edges });
          autoSaveCountdown = autoSaveSignal ? 5 : 1;
        }
      }, 1000);
    } else {
      clearInterval(autoSaveInterval);
    }
  }

  const validators = hasContext('flow/graph') && (getContext('flow/graph') as FlowGraphContext).validators;
  let saving: boolean = $state(false);
  let publishing: boolean = $state(false);
  let savable: boolean = $derived(!(publishing || readonly || savedHistoryId === historyId));
  let publishable: boolean = $derived(!(saving || (readonly && graph?.state === 'published') || nodes.length === 0));

  /**
   * Save flow graph.
   */
  function save() {
    if (!graph || saving || !savable) {
      return;
    }
    const id = graph.id;
    const data = $state.snapshot({ nodes, edges } as unknown) as History;
    const submittedHistoryId = historyId;
    const startTime = new Date().getTime();
    saving = true;
    trackGraphWrite(id, (signal) =>
      api
        .post(`flow/graph/${id}/save`, { json: data, signal })
        .json<Resp<FlowGraph>>()
        .then(({ data }) => data)
    )
      .then((result) => {
        graph = result;
        jsonGraph = JSON.stringify(data);
        savedHistoryId = submittedHistoryId;
        autoSaveSignal = false;
        const timeout = Math.max(0, 450 - (new Date().getTime() - startTime));
        setTimeout(() => {
          saving = false;
        }, timeout);
      })
      .catch(() => {
        autoSaveSignal = false;
        saving = false;
      });
  }

  /**
   * Function to be called before the flow graph is published.
   */
  function prepublish() {
    if (publishing || !publishable) {
      return;
    }
    // validate the flow graph before publishing
    if (validators) {
      for (const valid of validators) {
        if (!valid()) {
          alert({ level: 'error', message: 'invalid_flow_graph' });
          return;
        }
      }
    }
    // confirm the publishing action
    publishing = true;
    confirm({
      icon: icons.directionUpRight,
      title: $_('action.publish'),
      onconfirm: () => publish(),
      oncancel: () => {
        publishing = false;
      }
    });
  }

  /**
   * Publish the flow graph.
   */
  function publish() {
    if (!graph) {
      publishing = false;
      return;
    }
    // save and publish the flow graph
    const id = graph.id;
    const data = $state.snapshot({ nodes, edges } as unknown) as History;
    const submittedHistoryId = historyId;
    const startTime = new Date().getTime();
    trackGraphWrite(id, (signal) =>
      api
        .post(`flow/graph/${id}/publish`, { json: data, signal })
        .json<Resp<FlowGraph>>()
        .then(({ data }) => data)
    )
      .then((result) => {
        graph = result;
        jsonGraph = JSON.stringify(data);
        savedHistoryId = submittedHistoryId;
        const timeout = Math.max(0, 450 - (new Date().getTime() - startTime));
        setTimeout(() => {
          publishing = false;
        }, timeout);
      })
      .catch(() => {
        publishing = false;
      });
  }

  let leaving = false;
  let confirming = false;
  beforeNavigate(({ to, type, cancel }) => {
    if (leaving || readonly || savedHistoryId === historyId) {
      // no confirmation needed
      return;
    }
    // check if the graph has been modified
    if (jsonGraph && jsonGraph !== JSON.stringify({ nodes, edges })) {
      cancel();
      if (!confirming && type !== 'leave') {
        confirming = true;
        confirm({
          icon: icons.backSolid,
          title: $_('message.leave.title'),
          message: $_('message.leave.content'),
          shallow: false,
          oncancel: () => {
            confirming = false;
          },
          onconfirm: () => {
            leaving = true;
            confirming = false;
            to && goto(to.url);
          }
        });
      }
    }
  });

  onMount(() => {
    if (readonly) {
      toggleInteractivity(false);
    }
    return () => {
      clearInterval(autoSaveInterval);
    };
  });
</script>

<svelte:window
  onkeydown={(event) => {
    if (event.ctrlKey || event.metaKey) {
      if (event.code === 'KeyS') {
        event.preventDefault();
        save();
      } else if (event.code === 'KeyD') {
        event.preventDefault();
        prepublish();
      } else if (event.code === 'KeyU') {
        event.shiftKey ? redo() : undo();
      }
    }
  }}
/>

<PageHeader>
  {#if graph}
    <button
      class="group relative flex-center hover-dashed gap-2 px-2"
      onclick={() => graphEditor && graphEditor.showModal()}
      use:tooltip={{
        zIndex: 9999,
        content: graph.description ?? '',
        theme: 'description',
        followCursor: true,
        delay: [800, 0]
      }}
    >
      <Image transparent src={graph.icon} icon={icons.flowchartFilled} />
      <span class="overflow-hidden">
        <div class="truncate text-sm opacity-90">{graph.name}</div>
        <div class="mt-0.5 flex-center">
          <Badge shadow={false} dashed={graph.state === 'draft'}>
            {@const state = GraphState[graph.state]}
            <span class="size-4">
              {#if autoSaveMode && autoSaveSignal}
                <iconify-icon
                  icon={icons.arrowRotateClockwise}
                  width="1rem"
                  class="animate-[spin_5s_linear_infinite]"
                  style:color={state.iconColor}
                ></iconify-icon>
              {:else}
                <iconify-icon icon={state.icon} width="1rem" style:color={state.iconColor}></iconify-icon>
              {/if}
            </span>
            {#if autoSaveMode && autoSaveSignal}
              {$_('flow.autosaving', autoSaveCountdown)}
            {:else}
              {`${$_(state.label)} (${$time(graph.updated_at)})`}
            {/if}
          </Badge>
        </div>
      </span>
      <div class="invisible absolute flex-center size-full bg-blur-90 group-hover:visible">
        <iconify-icon icon={icons.edit} width="2rem" class="opacity-30 group-active:scale-95"></iconify-icon>
      </div>
    </button>
  {/if}
</PageHeader>

{#if graph}
  <GraphEditor bind:this={graphEditor} {...graph} onsave={(result) => (graph = result)} />
  <div class="svelte-flow__panel right-0 flex h-8 rounded-field border bg-blur-70 px-2 py-1 shadow-md">
    <div class="flex-center gap-2">
      <Badge dashed>{store.viewport.zoom.toFixed(1)}</Badge>
      <Button
        size="xs"
        icon={icons.pageFit}
        text={$_('flow.fitview')}
        onclick={() => store.fitView({ duration: 500 })}
      />
      <Button
        size="xs"
        icon={$minimap ? icons.map : icons.mapOff}
        text={$_('flow.minimap')}
        onclick={() => toggleMinimap()}
      />
      <Button
        size="xs"
        icon={interactive ? icons.lockOpen : icons.lockClosed}
        text={$_('flow.interactivity')}
        disabled={readonly}
        onclick={() => toggleInteractivity()}
      />
    </div>
    {#if !readonly}
      <div class="divider mx-0! divider-horizontal"></div>
      <div class="flex-center gap-2">
        <Button
          size="xs"
          icon={icons.arrowHookUpLeft}
          text={$_('action.undo')}
          disabled={histories.size <= 1 || historyId === firstHistoryId}
          onclick={() => {
            window.dispatchEvent(
              new KeyboardEvent('keydown', {
                key: 'u',
                code: 'KeyU',
                ctrlKey: true,
                metaKey: true,
                shiftKey: false
              })
            );
          }}
        />
        <Button
          size="xs"
          icon={icons.arrowHookUpRight}
          text={$_('action.redo')}
          disabled={histories.size <= 1 || historyId === lastHistoryId}
          onclick={() => {
            window.dispatchEvent(
              new KeyboardEvent('keydown', {
                key: 'u',
                code: 'KeyU',
                ctrlKey: true,
                metaKey: true,
                shiftKey: true
              })
            );
          }}
        />
      </div>
    {/if}
    <div class="divider mx-0! divider-horizontal"></div>
    <div class="flex-center gap-2">
      {#if autoSaveMode}
        <Button size="xs" text={$_('flow.autosave.off')} class="group" onclick={() => autoSave(false)}>
          <iconify-icon
            icon={icons.arrowRotateClockwise}
            width="1.125rem"
            class="animate-[spin_5s_linear_infinite] group-hover:hidden"
          ></iconify-icon>
          <iconify-icon
            icon={icons.pauseFilled}
            width="1.125rem"
            class="hidden group-hover:inline"
            style:color="var(--color-error)"
          ></iconify-icon>
        </Button>
      {:else}
        <Button
          size="xs"
          icon={icons.arrowRotateClockwise}
          text={$_('flow.autosave.on')}
          disabled={readonly}
          onclick={() => autoSave(true)}
        />
      {/if}
      <Button
        size="xs"
        icon={icons.save}
        text={$_('action.save')}
        loading={saving}
        disabled={!savable}
        onclick={() => save()}
      />
      <Button
        size="xs"
        icon={icons.directionUpRight}
        text={$_('action.publish')}
        loading={publishing}
        disabled={!publishable}
        onclick={() => prepublish()}
      />
    </div>
  </div>

  {#if $minimap}
    <div transition:fade={{ duration: 200 }}>
      <MiniMap pannable={true} zoomable={true} class="overflow-hidden rounded-box border bg-blur-70! shadow-md" />
    </div>
  {/if}
{/if}
