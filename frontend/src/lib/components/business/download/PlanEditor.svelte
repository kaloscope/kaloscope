<script lang="ts" module>
  import { TransferMethod } from '$lib/enums';
  import type {
    DownloadDir,
    Downloader,
    DownloadPlan,
    Filter,
    FlowGraph,
    IndexerConfig,
    MediaLib,
    Option,
    Page,
    Resource,
    Resp
  } from '$lib/types';

  type PlanEditorProps = Partial<{
    id: number;
    graph_id: number;
    graph_name: string | null;
    downloader_id: number;
    dir: string;
    keyword: string;
    filters: Record<string, any> | null; // eslint-disable-line
    interval_num: number;
    interval_start: string | null;
    interval_end: string | null;
    batch_limit: number;
    total_limit: number | null;
    transfer_lib_id: number | null;
    transfer_method: keyof typeof TransferMethod | null;
    sub_pattern: string | null;
    sub_repl: string | null;
    onsave: (result: DownloadPlan) => void;
  }>;
</script>

<script lang="ts">
  import { enhance } from '$app/forms';
  import { api } from '$lib/api';
  import { FileTree, Label, Modal, Overlay, Search, Select } from '$lib/components';
  import { DEFAULT_SUB_PATTERN, DEFAULT_SUB_REPL, EMPTY_SIGN } from '$lib/constants';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { onMount } from 'svelte';

  let {
    id,
    graph_id,
    graph_name,
    downloader_id,
    dir = '',
    keyword = '',
    filters = null,
    interval_num = 1,
    interval_start = null,
    interval_end = null,
    batch_limit = 10,
    total_limit = null,
    transfer_lib_id = 0,
    transfer_method = null,
    sub_pattern = DEFAULT_SUB_PATTERN,
    sub_repl = DEFAULT_SUB_REPL,
    onsave
  }: PlanEditorProps = $props();

  // the modal dialog and file tree instance
  let modal: Modal;
  let fileTree: FileTree;
  export const showModal = () => {
    if (id) {
      graphOptions = [{ value: graph_id, label: graph_name! }];
      configure(graph_id);
      preview();
      fileTree.confirm(dir);
      transfer_lib_id = transfer_lib_id ?? 0;
      transfer_method = transfer_method ?? (supportsHardlink ? 'hardlink' : 'symlink');
    }
    modal.show();
  };

  // the graph options and preview resources
  let graphOptions: Option[] = $state([]);
  let querySchema: Record<string, Filter> | null = $state(null);
  let keywordRequired: boolean = $state(false);
  let resources: Resource[] = $state([]);
  let searching = createLoading();
  let abortController: AbortController | null = null;

  // the download directory and downloaders
  let directory: DownloadDir | null = $state(null);
  let downloaders: Downloader[] = $state([]);

  // the media libraries and transfer options
  let mediaLibs: MediaLib[] = $state([]);
  let supportsHardlink: boolean = $state(true);

  // the loading state and form schema
  const loading = createLoading();
  const schema = createFormSchema(({ datetime, number, text }) => ({
    interval_num: number().min(1),
    interval_start: datetime().required(false),
    interval_end: datetime().required(false),
    batch_limit: number().min(1),
    total_limit: number().min(1).required(false),
    sub_pattern: text().maxlength(512).required(false),
    sub_repl: text().maxlength(512).required(false)
  }));

  /**
   * Get the indexer config for the given graph.
   *
   * @param graphId - The flow graph ID.
   */
  function configure(graphId: number | undefined) {
    if (!graphId) {
      querySchema = null;
      return;
    }
    api
      .get(`flow/indexer/${graphId}/config`)
      .json<Resp<IndexerConfig>>()
      .then(({ data }) => {
        querySchema = data?.search?.filters ?? null;
        keywordRequired = data?.search?.keyword?.required ?? false;
      })
      .catch(() => {
        querySchema = null;
      });
  }

  /**
   * Search for preview resources using the selected flow graph.
   */
  function preview() {
    if ($searching !== null) {
      return;
    }
    resources = [];
    if (!graph_id || (keywordRequired && !keyword.trim())) {
      return;
    }
    searching.start();
    abortController = new AbortController();
    api
      .post(`flow/graph/${graph_id}/execute`, {
        signal: abortController.signal,
        json: {
          $start: 'search_start',
          page_num: 1,
          page_size: batch_limit,
          keyword: keyword,
          ...filters
        }
      })
      .json<Resp<Page<Resource>>>()
      .then(({ data }) => {
        resources = data?.items ?? [];
      })
      .catch((error) => {
        if (error.name !== 'AbortError') {
          resources = [];
        }
      })
      .finally(() => {
        searching.end();
      });
  }

  /**
   * Save or update the download plan.
   */
  function upsert() {
    loading.start();
    api
      .post('download/plan/upsert', {
        json: {
          id,
          graph_id,
          downloader_id,
          dir: directory?.path || dir,
          keyword,
          filters,
          interval_num,
          interval_start: interval_start || null,
          interval_end: interval_end || null,
          batch_limit,
          total_limit: total_limit || null,
          transfer_lib_id: transfer_lib_id || null,
          transfer_method: transfer_lib_id ? transfer_method : null,
          sub_pattern: transfer_lib_id ? sub_pattern : null,
          sub_repl: transfer_lib_id ? sub_repl : null
        }
      })
      .json<Resp<DownloadPlan>>()
      .then(({ data }) => {
        modal.close();
        onsave?.(data);
        // reset the form
        if (!id) {
          setTimeout(() => {
            keyword = '';
            filters = null;
            interval_num = 1;
            interval_start = null;
            interval_end = null;
            batch_limit = 10;
            total_limit = null;
            transfer_lib_id = 0;
            transfer_method = supportsHardlink ? 'hardlink' : 'symlink';
            sub_pattern = DEFAULT_SUB_PATTERN;
            sub_repl = DEFAULT_SUB_REPL;
            resources = [];
            abortController?.abort();
          }, 200);
        }
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
            ['category', 'indexer'],
            ['states', 'modified'],
            ['states', 'published']
          ]
        })
        .json<Resp<Page<FlowGraph>>>()
        .then(({ data }) => {
          graphOptions = data.items
            .filter((g) => g.node_types.includes('search_start') && !g.only_preview)
            .map((g) => ({ value: g.id, label: g.name }));
          graph_id = graphOptions[0]?.value as number | undefined;
          configure(graph_id);
        });
    }

    // load the downloaders and media libraries in parallel
    Promise.all([
      api
        .get('download/dir/list')
        .json<Resp<DownloadDir[]>>()
        .then((r) => r.data)
        .catch(() => []),
      api
        .get('download/manager/list')
        .json<Resp<Downloader[]>>()
        .then((r) => r.data)
        .catch(() => []),
      api
        .get('media/lib/list')
        .json<Resp<MediaLib[]>>()
        .then((r) => r.data)
        .catch(() => []),
      api
        .get('system/platform')
        .json<Resp<{ platform: string }>>()
        .then((r) => r.data.platform)
        .catch(() => '')
    ]).then(([_directories, _downloaders, _mediaLibs, _platform]) => {
      downloaders = _downloaders;
      mediaLibs = _mediaLibs;
      supportsHardlink = _platform !== 'win32';
      // set the initial values
      if (!directory) {
        directory = _directories[0] || null;
      }
      if (!downloader_id) {
        downloader_id = downloaders.find((d) => d.status !== 'down')?.id ?? 0;
      }
      if (!transfer_method) {
        transfer_method = supportsHardlink ? 'hardlink' : 'symlink';
      }
    });
  });
</script>

<Modal
  icon={icons.arrowRotateClockwise}
  title={$_(id ? 'action.edit' : 'action.add', $_('entity.plan'))}
  maxWidth="36rem"
  bind:this={modal}
>
  <form
    method="post"
    use:enhance={({ cancel }) => {
      cancel();
      upsert();
    }}
  >
    <fieldset class="fieldset">
      <!-- graph selection and preview -->
      <Label required>{$_('field.graph')}</Label>
      <div class="flex gap-2">
        <Select
          required
          options={graphOptions}
          bind:value={graph_id}
          onchange={() => {
            abortController?.abort();
            keyword = '';
            filters = null;
            resources = [];
            configure(graph_id);
          }}
          class="max-w-40 select-sm"
          disabled={!!id}
        />
        <Search
          required={keywordRequired}
          maxWidth="100%"
          class="shadow-none!"
          label={$_('field.keyword')}
          bind:value={keyword}
          onsearch={() => preview()}
          schema={querySchema}
          filters={filters ? JSON.stringify(filters) : null}
          onfilter={(value) => {
            filters = value ? JSON.parse(value) : null;
            preview();
          }}
        />
      </div>
      <div class="relative mt-2 h-34 overflow-y-auto rounded-box border">
        <Overlay loading={$searching} fixed={false} animation="spinner" />
        <table class="table-pin-rows table table-fixed table-xs">
          <thead>
            <tr class="text-xs font-semibold uppercase">
              <th>{$_('field.title')}</th>
              <th class="w-20 text-right">{$_('field.size')}</th>
            </tr>
          </thead>
          <tbody>
            {#if resources.length > 0}
              {#each resources as rsrc, i (i)}
                <tr>
                  <td class="truncate" title={rsrc.title}>{rsrc.title ?? EMPTY_SIGN}</td>
                  <td class="text-right opacity-70">{rsrc.size ?? EMPTY_SIGN}</td>
                </tr>
              {/each}
            {:else if !$searching}
              <tr>
                <td colspan="2" class="h-26 text-center text-sm opacity-20">
                  {$_('data.nodata')}
                </td>
              </tr>
            {/if}
          </tbody>
        </table>
      </div>

      <!-- interval and batch limit -->
      <Label required>{$_('field.interval')}</Label>
      <div class="flex gap-2">
        <input class="input w-1/2 input-sm" bind:value={interval_num} {...schema.interval_num} />
        <Select disabled options={[{ value: 'hours', label: $_('duration.hours') }]} class="w-1/2 select-sm" />
      </div>
      <div class="flex min-w-0 gap-2 *:min-w-0">
        <div class="w-1/2 space-y-1.5">
          <Label>{$_('field.interval_start')}</Label>
          <input class="input w-full input-sm" bind:value={interval_start} {...schema.interval_start} />
        </div>
        <div class="w-1/2 space-y-1.5">
          <Label>{$_('field.interval_end')}</Label>
          <input class="input w-full input-sm" bind:value={interval_end} {...schema.interval_end} />
        </div>
      </div>
      <div class="flex gap-2">
        <div class="w-1/2 space-y-1.5">
          <Label required>{$_('field.batch_limit')}</Label>
          <input class="input w-full input-sm" bind:value={batch_limit} {...schema.batch_limit} />
        </div>
        <div class="w-1/2 space-y-1.5">
          <Label>{$_('field.total_limit')}</Label>
          <input class="input w-full input-sm" bind:value={total_limit} {...schema.total_limit} />
        </div>
      </div>

      <!-- downloader and save directory -->
      <div class="mt-6 flex gap-2">
        <div class="w-[clamp(3rem,20rem,100%)] max-w-40 space-y-1.5">
          <Label required>{$_('download.downloader.title')}</Label>
          <Select
            required
            options={downloaders.map((d) => ({
              value: d.id,
              label: d.name,
              disabled: d.status === 'down'
            }))}
            bind:value={downloader_id}
            class="w-full select-sm"
          />
        </div>
        <div class="w-full space-y-1.5">
          <Label required>{$_('download.dir')}</Label>
          {#if directory}
            <button
              type="button"
              class="input w-full cursor-pointer input-sm"
              onclick={() => fileTree.showModal(directory?.path)}
            >
              <iconify-icon icon={icons.folder} width="1.5rem" class="opacity-50"></iconify-icon>
              <input
                type="text"
                autocomplete="off"
                class="grow cursor-pointer truncate text-left direction-rtl"
                value={directory.path.split('').reverse().join('')}
                readonly
              />
              <span class="italic-text max-sm:hidden">{directory.free}</span>
            </button>
          {:else}
            <label class="input w-full input-sm">
              <iconify-icon icon={icons.folder} width="1.5rem" class="opacity-50"></iconify-icon>
              <input type="text" class="grow" bind:value={dir} />
            </label>
          {/if}
        </div>
      </div>

      <!-- transfer options -->
      <Label>{$_('download.transfer.title')}</Label>
      <Select
        options={[
          { value: 0, label: $_('download.transfer.none') },
          ...mediaLibs.map((lib) => ({ value: lib.id, label: lib.name }))
        ]}
        bind:value={transfer_lib_id}
        class="w-full input-sm"
      />
      {#if transfer_lib_id}
        <Label required>{$_('download.transfer.method')}</Label>
        <div class="flex flex-wrap gap-4">
          {#each Object.entries(TransferMethod) as [value, method] (value)}
            {#if value !== 'hardlink' || supportsHardlink}
              <label class="flex cursor-pointer items-center gap-1.5">
                <input type="radio" class="radio radio-sm" {value} bind:group={transfer_method} />
                <span class="text-sm">{$_(method.label)}</span>
              </label>
            {/if}
          {/each}
        </div>
        <Label>{$_('download.transfer.substitution')}</Label>
        <div class="flex gap-2">
          <input
            placeholder={$_('download.transfer.sub_pattern')}
            class="input w-full truncate input-sm"
            bind:value={sub_pattern}
            {...schema.sub_pattern}
          />
          <input
            placeholder={$_('download.transfer.sub_repl')}
            class="input w-full truncate input-sm"
            bind:value={sub_repl}
            {...schema.sub_repl}
            disabled={!sub_pattern}
          />
        </div>
      {/if}
    </fieldset>
    <div class="modal-action">
      <button type="button" class="btn" onclick={() => modal.close()}>
        {$_('message.cancel')}
      </button>
      <button
        type="submit"
        class="btn btn-submit"
        disabled={$loading !== null || !graph_id || !downloader_id || (keywordRequired && !keyword.trim())}
      >
        {$_('message.confirm')}
        {#if $loading}
          <span class="loading loading-xs loading-dots"></span>
        {/if}
      </button>
    </div>
  </form>
</Modal>

<FileTree
  bind:this={fileTree}
  onlyDirs={true}
  onconfirm={(stats) => {
    directory = { path: stats.path, free: stats.free };
    dir = directory.path;
  }}
/>
