<script lang="ts">
  import { api } from '$lib/api';
  import { Button, DownloaderEditor, Dropdown, Grid, Image, Status, confirm } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { Downloader, Resp } from '$lib/types';
  import { debounce } from '$lib/utils';
  import { onMount, tick } from 'svelte';

  let downloaders: Downloader[] = $state([]);
  let refreshKey: number = $state(0);
  let creator: DownloaderEditor | null = $state(null);
  let updater: DownloaderEditor | null = $state(null);
  let selected: Downloader | null = $state(null);
  const loading = createLoading();

  /**
   * Get the downloaders.
   */
  function getAll() {
    loading.start();
    api
      .get('download/manager/list')
      .json<Resp<Downloader[]>>()
      .then(({ data }) => (downloaders = data))
      .finally(() => loading.end());
  }

  /**
   * Delete a downloader by ID.
   *
   * @param id - The downloader ID.
   */
  function del(id: number) {
    loading.start();
    api
      .post('download/manager/delete', { json: { ids: [id] } })
      .then(() => getAll())
      .catch(() => loading.end());
  }

  /**
   * Sort the downloaders.
   */
  const sort = debounce(() => {
    const ids = downloaders.map((downloader) => downloader.id);
    api.post('download/manager/sort', { json: { ids } });
  });

  onMount(() => {
    getAll();
  });
</script>

{#snippet term(name: string, description: string | number | null, status: Downloader['status'] | null = null)}
  <div class="flex items-center justify-between gap-4 py-2" title={String(description ?? '')}>
    <dt class="whitespace-nowrap">{name}:</dt>
    <dd class="flex items-center gap-2 truncate opacity-70">
      <span class="truncate">{description}</span>
      {#if status}
        <Status rate={status === 'up' ? 1 : status === 'down' ? 0 : null} class="px-1" />
      {/if}
    </dd>
  </div>
{/snippet}

<Grid
  data={downloaders}
  loading={$loading}
  uniqueKey="id"
  class="pull-to-refresh"
  gridClass="grid-cols-sparse"
  itemClass="z-1 rounded-field border shadow-sm hover:shadow-lg"
  tailClass="rounded-field border-1 border-dashed duration-300 opacity-20 hover:opacity-50 hover:shadow-lg"
  ondragged={(data) => {
    downloaders = data;
    sort();
  }}
>
  {#snippet item(downloader, index)}
    <div class="flex justify-between gap-2 rounded-t-field bg-base-200 p-4">
      <div class="grid grid-flow-col items-center gap-2" title={downloader.version}>
        <Image
          transparent
          preset={downloader.preset?.toLowerCase()}
          icon={icons.box3dScanFill}
          width="2rem"
          class="[&_iconify-icon]:opacity-70!"
        />
        <div class="flex items-baseline gap-1 overflow-hidden">
          <span class="text-base {downloader.version ? '' : 'truncate'}">
            {downloader.name}
          </span>
          {#if downloader.version}
            <span class="truncate italic-text">{downloader.version}</span>
          {/if}
        </div>
      </div>
      <div class="flex items-center gap-1">
        <Button
          icon={icons.edit}
          onclick={() => {
            selected = downloader;
            tick().then(() => updater?.showModal());
          }}
        />
        <Dropdown contentWidth="10rem" class="dropdown-end">
          {#snippet trigger()}
            <div class="btn btn-square btn-subtle btn-sm">
              <iconify-icon icon={icons.moreVertical} width="1rem"></iconify-icon>
            </div>
          {/snippet}
          <ul class="menu gap-1">
            <li class={index === 0 ? 'menu-disabled' : ''}>
              <button
                class="px-2"
                onclick={() => {
                  const temp = downloaders[index];
                  downloaders[index] = downloaders[index - 1];
                  downloaders[index - 1] = temp;
                  sort();
                }}
              >
                <iconify-icon icon={icons.arrowUp} width="1rem"></iconify-icon>
                {$_('action.move_up')}
              </button>
            </li>
            <li class={index === downloaders.length - 1 ? 'menu-disabled' : ''}>
              <button
                class="px-2"
                onclick={() => {
                  const temp = downloaders[index];
                  downloaders[index] = downloaders[index + 1];
                  downloaders[index + 1] = temp;
                  sort();
                }}
              >
                <iconify-icon icon={icons.arrowDown} width="1rem"></iconify-icon>
                {$_('action.move_down')}
              </button>
            </li>
            <li>
              <button
                class="px-2"
                onclick={() => {
                  confirm({
                    icon: icons.delete,
                    title: `${$_('action.delete', $_('download.downloader.config'))} [${downloader.name}]`,
                    onconfirm: () => del(downloader.id)
                  });
                }}
              >
                <iconify-icon icon={icons.delete} width="1rem"></iconify-icon>
                {$_('action.delete')}
              </button>
            </li>
          </ul>
        </Dropdown>
      </div>
    </div>
    <dl class="rounded-b-field bg-base-100 p-4 text-sm">
      {@render term($_('field.host'), downloader.host)}
      <div class="divider my-0"></div>
      {@render term($_('field.port'), downloader.port)}
      <div class="divider my-0"></div>
      {@render term($_('field.status'), $_(`status.${downloader.status}`), downloader.status)}
    </dl>
  {/snippet}
  {#snippet tail()}
    <button class="flex-col-center size-full cursor-pointer gap-2 p-4" onclick={() => creator?.showModal()}>
      <iconify-icon icon={icons.addCircle} width="2.5rem"></iconify-icon>
      <span class="text-2xl">{$_('action.add')}</span>
    </button>
  {/snippet}
</Grid>

{#key refreshKey}
  <DownloaderEditor
    bind:this={creator}
    onsave={() => {
      getAll();
      refreshKey = new Date().getTime();
    }}
  />
{/key}

{#if selected}
  <DownloaderEditor bind:this={updater} {...selected} preset={selected.preset ?? ''} onsave={() => getAll()} />
{/if}
