<script lang="ts" module>
  import type { GraphCategory } from '$lib/enums';
  import type { FlowGraph, FlowTrigger, Page, Resp } from '$lib/types';

  type FlowTriggersProps = {
    category: keyof typeof GraphCategory;
    triggers?: FlowTrigger[];
    onchange?: (triggers: FlowTrigger[]) => void;
    class?: string;
  };
</script>

<script lang="ts">
  import { api } from '$lib/api';
  import { Button } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { onMount, tick } from 'svelte';
  import { flip } from 'svelte/animate';

  let { category, triggers: _triggers, onchange, class: _class }: FlowTriggersProps = $props();

  // the snapshot is used to avoid reactivity issues with the initial triggers
  let triggers: FlowTrigger[] = $derived($state.snapshot(_triggers) ?? []);

  // the currently selected trigger graph ID and element
  let triggerGraphId: number = $state(0);
  let triggerElement: HTMLLIElement | null = $state(null);

  // the state for appending a new trigger
  let appending: boolean = $state(false);
  let appendingElement: HTMLLIElement | null = $state(null);

  // the unbound graphs that can be selected for new triggers
  let graph: FlowGraph | null = $state(null);
  let graphs: FlowGraph[] = $state([]);
  let unboundGraphs: FlowGraph[] = $derived(graphs.filter((g) => !triggers.some((t) => t.graph_id === g.id)));

  /**
   * Scrolls the given HTMLLIElement into view.
   *
   * @param element - The selected HTMLLIElement.
   * @param delay - Optional delay in milliseconds before scrolling.
   */
  function scrollIntoView(element: HTMLLIElement | null, delay: number = 0) {
    if (!element) {
      return;
    }
    const scroll = () => {
      element.scrollIntoView({
        behavior: 'smooth',
        block: 'nearest'
      });
    };
    delay ? setTimeout(scroll, delay) : scroll();
  }

  onMount(() => {
    // load the available graphs for the given category
    api
      .get('flow/graph/list', {
        searchParams: [
          ['page_num', 0],
          ['ordering', 'name'],
          ['category', category],
          ['states', 'modified'],
          ['states', 'published']
        ]
      })
      .json<Resp<Page<FlowGraph>>>()
      .then(({ data }) => {
        graphs = data.items;
      });
  });
</script>

<div class="overflow-hidden rounded-box border {_class}">
  <div
    class="flex items-center justify-between bg-gradient px-3 py-1"
    style="border-bottom: 1px inset var(--color-border)"
  >
    <span class="text-sm text-base-content/60">{$_('flow.trigger.bound')}</span>
    <span class="flex-center gap-1">
      <Button
        size="xs"
        icon={icons.addCircle}
        text={$_('action.append')}
        class={appending || unboundGraphs.length === 0 ? 'btn-disabled' : 'text-green-900'}
        onclick={() => {
          if (appending || unboundGraphs.length === 0) {
            return;
          }
          graph = unboundGraphs[0];
          appending = true;
          triggerGraphId = 0;
          triggerElement = null;
          tick().then(() => scrollIntoView(appendingElement));
        }}
      />
      <Button
        size="xs"
        icon={icons.subtractCircle}
        text={$_('action.delete')}
        class={appending || triggerGraphId === 0 ? 'btn-disabled' : 'text-red-900'}
        onclick={() => {
          if (appending || triggerGraphId === 0) {
            return;
          }
          triggers = triggers.filter((t) => t.graph_id !== triggerGraphId);
          onchange?.(triggers);
          triggerGraphId = 0;
          triggerElement = null;
        }}
      />
      <Button
        size="xs"
        icon={icons.arrowBigUp}
        text={$_('action.move_up')}
        class={appending || triggerGraphId === 0 ? 'btn-disabled' : 'text-surface'}
        onclick={() => {
          if (appending || triggerGraphId === 0) {
            return;
          }
          const index = triggers.findIndex((t) => t.graph_id === triggerGraphId);
          if (index > 0) {
            const temp = triggers[index];
            triggers[index] = triggers[index - 1];
            triggers[index - 1] = temp;
            onchange?.(triggers);
          }
          scrollIntoView(triggerElement, 200);
        }}
      />
      <Button
        size="xs"
        icon={icons.arrowBigDown}
        text={$_('action.move_down')}
        class={appending || triggerGraphId === 0 ? 'btn-disabled' : 'text-surface'}
        onclick={() => {
          if (appending || triggerGraphId === 0) {
            return;
          }
          const index = triggers.findIndex((t) => t.graph_id === triggerGraphId);
          if (index < triggers.length - 1) {
            const temp = triggers[index];
            triggers[index] = triggers[index + 1];
            triggers[index + 1] = temp;
            onchange?.(triggers);
          }
          scrollIntoView(triggerElement, 200);
        }}
      />
    </span>
  </div>
  <ul class="list max-h-72 min-h-12 overflow-y-auto">
    {#each triggers as trigger, index (trigger.graph_id)}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <li
        class="list-row items-center rounded-none py-2 hover:bg-base-300 {appending ? '' : 'cursor-pointer'}"
        onclick={(event) => {
          if (appending) {
            return;
          }
          if (triggerGraphId === trigger.graph_id) {
            // deselect the clicked row
            triggerGraphId = 0;
            triggerElement = null;
          } else {
            // select the clicked row
            triggerGraphId = trigger.graph_id;
            triggerElement = event.currentTarget as HTMLLIElement;
          }
        }}
        animate:flip={{ duration: 200 }}
      >
        <span class="flex-center gap-1">
          <input
            type="radio"
            class="pointer-events-none radio radio-xs"
            checked={triggerGraphId === trigger.graph_id}
          />
          <span class="text-lg font-thin {triggerGraphId === trigger.graph_id ? '' : 'opacity-30'}">{index + 1}</span>
        </span>
        <span class="font-semibold text-surface/80 list-col-grow">{trigger.graph_name}</span>
        <Button
          icon={trigger.asynchronous ? icons.arrowNarrowDownDashed : icons.arrowNarrowDown}
          text={trigger.asynchronous ? $_('flow.trigger.async_mode') : $_('flow.trigger.sync_mode')}
          tipTheme="plain"
          ghost={!trigger.asynchronous}
          class="btn-circle {trigger.asynchronous ? 'border-dashed text-base-content/70' : ''}"
          onclick={(event) => {
            event.stopPropagation();
            trigger.asynchronous = !trigger.asynchronous;
            onchange?.(triggers);
          }}
        />
      </li>
    {/each}
    {#if appending}
      <li class="list-row items-center rounded-none py-2 hover:bg-base-300" bind:this={appendingElement}>
        <span class="flex-center gap-1">
          <input type="radio" class="radio radio-xs" checked={true} />
          <span class="text-lg font-thin">{triggers.length + 1}</span>
        </span>
        <select class="select appearance-none list-col-grow select-sm" bind:value={graph}>
          {#each unboundGraphs as graph (graph.id)}
            <option value={graph}>{graph.name}</option>
          {/each}
        </select>
        <span class="flex-center gap-1">
          <Button
            icon={icons.checkmark}
            text={$_('message.confirm')}
            class="text-green-900"
            onclick={() => {
              if (graph) {
                const newTrigger: FlowTrigger = {
                  graph_id: graph.id,
                  graph_name: graph.name,
                  asynchronous: false
                };
                triggers = [...triggers, newTrigger];
                onchange?.(triggers);
                appending = false;
              }
            }}
          />
          <Button
            icon={icons.dismiss}
            text={$_('message.cancel')}
            class="text-red-900"
            onclick={() => {
              appending = false;
            }}
          />
        </span>
      </li>
    {/if}
  </ul>
</div>
