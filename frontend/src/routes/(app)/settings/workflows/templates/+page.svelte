<script lang="ts">
  import { goto } from '$app/navigation';
  import { api } from '$lib/api';
  import {
    Badge,
    Button,
    DataView,
    FlowRepos,
    Image,
    Paginator,
    prompt,
    Search,
    Select,
    type PaginatorProps
  } from '$lib/components';
  import { enumToOptions, GraphCategory } from '$lib/enums';
  import { createLoading, createSortField } from '$lib/helpers';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { FlowGraph, FlowRepo, FlowTemplate, OptionValue, Page, Resp } from '$lib/types';
  import { untrack } from 'svelte';

  let flowRepos: FlowRepos;
  let tmplRepos: FlowRepo[] = $state([]);
  let tmplRepo: OptionValue = $state(null);
  let tmplCategory: OptionValue = $state(null);
  let tmplName: string = $state('');
  let tmpls: FlowTemplate[] = $state([]);

  const pagination: PaginatorProps = $state({ current: 1, size: 16, onchange: search });
  const ordering = createSortField();
  const loading = createLoading();

  const btnLoading = createLoading();
  let refId: number | null = $state(null);
  let copyId: number | null = $state(null);

  /**
   * Search for flow templates.
   *
   * @param page - The page number.
   * @param size - The page size.
   */
  function search(page: number = 1, size: number = pagination.size) {
    loading.start();
    api
      .get('flow/tmpl/list', {
        searchParams: {
          page_num: page,
          page_size: size,
          ordering: $ordering,
          category: tmplCategory ?? '',
          repo: tmplRepo ?? '',
          name: tmplName,
          newest: true
        }
      })
      .json<Resp<Page<FlowTemplate>>>()
      .then(({ data }) => {
        pagination.current = page;
        pagination.size = size;
        pagination.total = data.total;
        tmpls = data.items;
      })
      .finally(() => {
        loading.end();
      });
  }

  /**
   * Get a unique name based on the template name.
   *
   * @param tmpl - The selected flow template.
   * @param callback - Callback function to handle the unique name.
   */
  function getUniqueName(tmpl: FlowTemplate, callback: (name: string) => void) {
    btnLoading.start();
    api
      .get('flow/graph/name', { searchParams: { name: tmpl.name } })
      .json<Resp<string>>()
      .then(({ data }) => {
        callback(data);
      })
      .finally(() => {
        refId = null;
        copyId = null;
        btnLoading.end();
      });
  }

  /**
   * Reference a flow template.
   *
   * @param id - The flow template ID.
   * @param name - The name of the new flow graph.
   */
  function refTmpl(id: number, name: string) {
    loading.start();
    api
      .post(`flow/tmpl/${id}/ref`, { json: { name } })
      .json<Resp<FlowGraph>>()
      .then(() => {
        goto('/settings/workflows/graphs');
      })
      .finally(() => {
        loading.end();
      });
  }

  /**
   * Copy a flow template.
   *
   * @param id - The flow template ID.
   * @param name - The name of the new flow graph.
   */
  function copyTmpl(id: number, name: string) {
    loading.start();
    api
      .post(`flow/tmpl/${id}/copy`, { json: { name } })
      .json<Resp<FlowGraph>>()
      .then(() => {
        goto('/settings/workflows/graphs');
      })
      .finally(() => {
        loading.end();
      });
  }

  $effect(() => {
    $ordering; // eslint-disable-line
    untrack(() => search());
  });
</script>

<DataView
  dvh
  mode="grid"
  data={tmpls}
  loading={$loading}
  gridClass="grid-cols-sparse"
  itemClass="mb-2 border overflow-hidden rounded-sm bg-base-100 hover:shadow-lg"
>
  {#snippet filters()}
    <Select
      filter
      options={[
        { value: '', label: $_('enum.all') },
        ...tmplRepos.map((r) => ({ value: r.repo_name, label: r.repo_name }))
      ]}
      bind:value={tmplRepo}
      label={$_('entity.source')}
      onchange={() => search()}
    />
    <Select
      translate
      filter
      options={enumToOptions(GraphCategory)}
      bind:value={tmplCategory}
      label={$_('field.category')}
      onchange={() => search()}
      class="max-lg:hidden"
    />
    <Search label={$_('field.name')} bind:value={tmplName} onsearch={() => search()} />
  {/snippet}
  {#snippet actions()}
    <Button
      size="md"
      icon={icons.cloudCube}
      text={$_('action.edit', $_('entity.sources'))}
      onclick={() => flowRepos.showModal()}
    />
  {/snippet}
  {#snippet item(tmpl)}
    {@const category = GraphCategory[tmpl.category]}
    <div class="relative p-4 pt-5">
      <div class="flex-center gap-2">
        <Image src={tmpl.icon} icon={icons.box3d} width="3.5rem" shadow transparent />
        <div class="w-full min-w-0 space-y-1">
          <div class="truncate text-lg font-bold opacity-90 text-shadow-xs" title={tmpl.name}>
            {tmpl.name}
          </div>
          <div class="flex flex-wrap items-baseline gap-1">
            <Image circle src={tmpl.repo.owner_avatar} width="1rem" class="my-auto opacity-90" />
            <a href={tmpl.repo.repo_url} target="_blank" class="link text-sm font-medium link-hover opacity-70">
              {tmpl.repo.repo_name.split('/')[1]}
            </a>
            {#if tmpl.repo.owner_name}
              <a href={tmpl.repo.owner_url} target="_blank" class="link text-xs link-hover opacity-40">
                @{tmpl.repo.owner_name}
              </a>
            {/if}
          </div>
        </div>
      </div>
      <Badge class="absolute top-1 right-1" icon={category.icon} iconColor={category.iconColor}>
        {$_(category.label)}
      </Badge>
    </div>
    <div class="m-4 mt-0 line-clamp-3 h-15 text-sm opacity-70" title={tmpl.description}>
      {tmpl.description}
    </div>
    <div class="divider mx-2 my-0 h-0"></div>
    <div class="flex justify-between gap-2 p-2">
      <div class="flex-center pl-2 text-xs font-semibold opacity-50">{$dateTime(tmpl.revision * 1000)}</div>
      <div class="flex-center gap-1">
        <Button
          icon={icons.link}
          text={$_('action.reference', $_('entity.template'))}
          loading={$btnLoading && refId === tmpl.id}
          disabled={tmpl.graphs.length > 0}
          onclick={() => {
            if (refId || copyId) {
              return;
            }
            refId = tmpl.id;
            getUniqueName(tmpl, (advice) => {
              prompt({
                advice: advice,
                icon: icons.link,
                title: $_('action.reference', $_('entity.template')),
                message: $_('flow.tmpl.confirm_name'),
                onconfirm: (name) => name && refTmpl(tmpl.id, name)
              });
            });
          }}
        />
        <Button
          icon={icons.documentCopy}
          text={$_('action.copy', $_('entity.template'))}
          loading={$btnLoading && copyId === tmpl.id}
          onclick={() => {
            if (refId || copyId) {
              return;
            }
            copyId = tmpl.id;
            getUniqueName(tmpl, (advice) => {
              prompt({
                advice: advice,
                icon: icons.documentCopy,
                title: $_('action.copy', $_('entity.template')),
                message: $_('flow.tmpl.confirm_name'),
                onconfirm: (name) => name && copyTmpl(tmpl.id, name)
              });
            });
          }}
        />
      </div>
    </div>
  {/snippet}
  {#snippet paginator(disabled)}
    <Paginator {disabled} {...pagination} />
  {/snippet}
</DataView>

<FlowRepos
  bind:this={flowRepos}
  onchange={(repos) => (tmplRepos = repos)}
  onclose={(changed) => {
    if (changed) {
      tmplCategory = null;
      tmplRepo = null;
      tmplName = '';
      search();
    }
  }}
/>
