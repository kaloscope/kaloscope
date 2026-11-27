<script lang="ts">
  import { api } from '$lib/api';
  import {
    Button,
    Cell,
    DataView,
    HCell,
    Paginator,
    Search,
    VariableEditor,
    confirm,
    type PaginatorProps
  } from '$lib/components';
  import { copyText, createLoading, createSortField } from '$lib/helpers';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { GlobalVariable, Page, Resp } from '$lib/types';
  import { tick, untrack } from 'svelte';

  let variables: GlobalVariable[] = $state([]);
  let key: string = $state('');
  let creator: VariableEditor | null = $state(null);
  let updater: VariableEditor | null = $state(null);
  let selected: GlobalVariable | null = $state(null);

  const pagination: PaginatorProps = $state({ current: 1, size: 15, onchange: search });
  const ordering = createSortField();
  const loading = createLoading();

  /**
   * Search for global variables.
   *
   * @param page - Page number.
   * @param size - Page size.
   */
  function search(page: number = 1, size: number = pagination.size) {
    loading.start();
    api
      .get('variable/list', {
        searchParams: {
          page_num: page,
          page_size: size,
          ordering: $ordering,
          key: key
        }
      })
      .json<Resp<Page<GlobalVariable>>>()
      .then(({ data }) => {
        pagination.current = page;
        pagination.size = size;
        pagination.total = data.total;
        variables = data.items;
      })
      .finally(() => {
        loading.end();
      });
  }

  /**
   * Delete a global variable by ID.
   *
   * @param id - Global variable ID.
   */
  function del(id: number) {
    loading.start();
    api
      .post('variable/delete', { json: { ids: [id] } })
      .then(() => search(pagination.current))
      .catch(() => loading.end());
  }

  $effect(() => {
    $ordering; // eslint-disable-line
    untrack(() => search());
  });
</script>

<DataView dvh loading={$loading} data={variables}>
  {#snippet filters()}
    <Search label={$_('field.name')} bind:value={key} onsearch={() => search()} />
  {/snippet}
  {#snippet actions()}
    <Button
      size="md"
      icon={icons.addCircle}
      text={$_('action.add', $_('entity.variable'))}
      onclick={() => creator?.showModal()}
    />
  {/snippet}
  {#snippet header()}
    <HCell width={['25%', '50%']} text={$_('field.name')} sort={ordering.bind('key')} />
    <HCell width={['25%', '50%']} text={$_('field.value')} />
    <HCell width={['25%', null]} text={$_('field.created')} sort={ordering.bind('created_at')} />
    <HCell width={['25%', null]} text={$_('field.updated')} sort={ordering.bind('updated_at')} />
    <HCell actions />
  {/snippet}
  {#snippet row(variable)}
    <Cell>
      <button class="group relative flex-center hover-dashed px-2 py-0.5" onclick={() => copyText(variable.key)}>
        <span class="truncate text-base">{variable.key}</span>
        <div class="invisible absolute flex-center size-full bg-blur-70 group-hover:visible">
          <iconify-icon icon={icons.documentCopy} width="1.5rem" class="opacity-30 group-active:scale-95">
          </iconify-icon>
        </div>
      </button>
    </Cell>
    <Cell>
      <iconify-icon icon={variable.encrypted ? icons.lockClosed : icons.lockOpen} width="1.25rem" class="opacity-70">
      </iconify-icon>
      <div
        class="truncate"
        class:text-clip={variable.encrypted}
        title={variable.encrypted ? undefined : variable.value}
      >
        {variable.encrypted ? '•'.repeat(variable.value_length) : variable.value}
      </div>
    </Cell>
    <Cell text={$dateTime(variable.created_at)} />
    <Cell text={$dateTime(variable.updated_at)} />
    <Cell
      actions={[
        {
          icon: icons.edit,
          text: $_('action.edit', $_('entity.variable')),
          onclick: () => {
            selected = variable;
            tick().then(() => updater?.showModal());
          }
        },
        {
          icon: icons.delete,
          text: $_('action.delete', $_('entity.variable')),
          onclick: () => {
            confirm({
              icon: icons.delete,
              title: `${$_('action.delete', $_('entity.variable'))} [${variable.key}]`,
              onconfirm: () => del(variable.id)
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

<VariableEditor bind:this={creator} onsave={() => search()} />

{#if selected}
  <VariableEditor
    bind:this={updater}
    {...selected}
    value={selected.encrypted ? '' : selected.value}
    onsave={() => search(pagination.current)}
  />
{/if}
