<script lang="ts" module>
  import { enumToOptions, IntervalUnit, JobTrigger } from '$lib/enums';
  import type { FlowGraph, FlowJob, Option, Page, Resp } from '$lib/types';

  type JobEditorProps = Partial<{
    id: number;
    graph_id: number;
    graph_name: string | null;
    trigger: keyof typeof JobTrigger;
    bootparams: Record<string, any> | null; // eslint-disable-line
    run_date: string | null;
    cron_expr: string | null;
    interval_num: number | null;
    interval_unit: keyof typeof IntervalUnit | null;
    interval_start: string | null;
    interval_end: string | null;
    onsave: (result: FlowJob) => void;
  }>;
</script>

<script lang="ts">
  import { enhance } from '$app/forms';
  import { api } from '$lib/api';
  import { alert, CodeMirror, Label, Modal, Select } from '$lib/components';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { json } from '@codemirror/lang-json';
  import { onMount } from 'svelte';

  let {
    id,
    graph_id,
    graph_name,
    trigger = 'cron',
    bootparams = null,
    run_date = null,
    cron_expr = null,
    interval_num = null,
    interval_unit = null,
    interval_start = null,
    interval_end = null,
    onsave
  }: JobEditorProps = $props();

  // the modal dialog instance
  let modal: Modal;
  export const showModal = () => {
    if (id) {
      graphOptions = [{ value: graph_id, label: graph_name! }];
    }
    modal.show();
  };

  // the graph options for the dropdown
  let graphOptions: Option[] = $state([]);

  // the JSON string for boot parameters
  let jsonParams = $derived(bootparams ? JSON.stringify(bootparams, null, 2) : '{}');

  // the loading state and form schema
  const loading = createLoading();
  const schema = createFormSchema(({ datetime, text, number }) => ({
    run_date: datetime(),
    cron_expr: text().maxlength(255).pattern('\\S+(\\s+\\S+){4}'),
    interval_num: number().min(1),
    interval_start: datetime().required(false),
    interval_end: datetime().required(false)
  }));

  /**
   * Save or update the flow job.
   */
  function upsert() {
    // validate the boot parameters
    let params = null;
    try {
      params = JSON.parse(jsonParams);
    } catch (error) {
      console.error(error);
    }
    if (typeof params !== 'object' || Array.isArray(params) || params === null) {
      alert({ level: 'error', message: 'invalid_boot_params' });
      return;
    }
    // send the request
    loading.start();
    const json: Record<string, unknown> = { id, graph_id, trigger, bootparams: params };
    if (trigger === 'date') {
      json.run_date = run_date || null;
    } else if (trigger === 'cron') {
      json.cron_expr = cron_expr;
    } else if (trigger === 'interval') {
      json.interval_num = interval_num;
      json.interval_unit = interval_unit;
      json.interval_start = interval_start || null;
      json.interval_end = interval_end || null;
    }
    api
      .post('flow/job/upsert', { json })
      .json<Resp<FlowJob>>()
      .then(({ data }) => {
        modal.close();
        onsave?.(data);
        // reset the form
        setTimeout(() => {
          trigger = 'cron';
          bootparams = null;
          run_date = null;
          cron_expr = null;
          interval_num = null;
          interval_unit = null;
          interval_start = null;
          interval_end = null;
        }, 200);
      })
      .finally(() => loading.end());
  }

  onMount(() => {
    // load the available flow graphs
    if (!id) {
      api
        .get('flow/graph/list', {
          searchParams: [
            ['page_num', 0],
            ['ordering', 'name'],
            ['category', 'schedule'],
            ['states', 'modified'],
            ['states', 'published']
          ]
        })
        .json<Resp<Page<FlowGraph>>>()
        .then(({ data }) => {
          graphOptions = data.items.map((g) => ({ value: g.id, label: g.name }));
        });
    }
  });
</script>

<Modal icon={icons.clock} title={$_(id ? 'action.edit' : 'action.add', $_('entity.job'))} bind:this={modal}>
  <form
    method="post"
    use:enhance={({ cancel }) => {
      cancel();
      upsert();
    }}
  >
    <fieldset class="fieldset">
      <Label required>{$_('field.graph')}</Label>
      <Select options={graphOptions} bind:value={graph_id} name="graph_id" class="w-full" disabled={!!id} />
      <Label>{$_('field.bootparams')}</Label>
      <CodeMirror
        darkMode
        minWidth="100%"
        maxWidth="100%"
        maxHeight="10rem"
        language={json()}
        title={$_('field.bootparams')}
        bind:document={jsonParams}
      />
      <Label required>{$_('field.trigger')}</Label>
      <Select translate options={enumToOptions(JobTrigger, false)} bind:value={trigger} name="trigger" class="w-full" />
      {#if trigger === 'date'}
        <Label required>{$_('field.run_date')}</Label>
        <input class="input w-full" bind:value={run_date} {...schema.run_date} />
      {:else if trigger === 'cron'}
        <Label required>{$_('field.cron_expr')}</Label>
        <input placeholder="0 * * * *" class="input w-full" bind:value={cron_expr} {...schema.cron_expr} />
      {:else if trigger === 'interval'}
        <Label required>{$_('field.interval')}</Label>
        <div class="flex gap-2">
          <input class="input w-1/2" bind:value={interval_num} {...schema.interval_num} />
          <Select
            translate
            required
            options={enumToOptions(IntervalUnit, false)}
            bind:value={interval_unit}
            name="interval_unit"
            class="w-1/2"
          />
        </div>
        <Label>{$_('field.interval_start')}</Label>
        <input class="input w-full" bind:value={interval_start} {...schema.interval_start} />
        <Label>{$_('field.interval_end')}</Label>
        <input class="input w-full" bind:value={interval_end} {...schema.interval_end} />
      {/if}
    </fieldset>
    <div class="modal-action">
      <button type="button" class="btn" onclick={() => modal.close()}>
        {$_('message.cancel')}
      </button>
      <button type="submit" class="btn btn-submit" disabled={$loading !== null || !graph_id}>
        {$_('message.confirm')}
        {#if $loading}
          <span class="loading loading-xs loading-dots"></span>
        {/if}
      </button>
    </div>
  </form>
</Modal>
