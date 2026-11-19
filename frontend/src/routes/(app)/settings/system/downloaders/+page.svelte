<script lang="ts">
  import { api } from '$lib/api';
  import { Button, DownloaderEditor, Dropdown, Empty, EntityCard, Grid, Image, confirm } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { Downloader, DownloadSource, Resp } from '$lib/types';
  import { debounce } from '$lib/utils/timing';
  import type { IconifyIcon } from 'iconify-icon';
  import { onMount, tick } from 'svelte';

  let downloaders: Downloader[] = $state([]);
  let refreshKey: number = $state(0);
  let creator: DownloaderEditor | null = $state(null);
  let updater: DownloaderEditor | null = $state(null);
  let selected: Downloader | null = $state(null);
  const loading = createLoading();

  // the dot colors of the downloader states
  const STATUS_DOTS: Record<Downloader['status'], string> = {
    up: 'bg-success',
    down: 'bg-error',
    unknown: 'bg-base-content/40'
  };

  // the icons of the accepted source formats
  const SOURCE_ICONS: Record<DownloadSource, IconifyIcon> = {
    raw: icons.link,
    magnet: icons.magnetUp,
    torrent: icons.document
  };

  /**
   * Format the network address of a downloader.
   *
   * @param downloader - Downloader to describe.
   * @returns The host with its port, or `null` without a host.
   */
  function address(downloader: Downloader): string | null {
    if (!downloader.host) return null;
    return downloader.port ? `${downloader.host}:${downloader.port}` : downloader.host;
  }

  /**
   * Get download clients.
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
   * @param id - Downloader ID.
   */
  function del(id: number) {
    loading.start();
    api
      .post('download/manager/delete', { json: { ids: [id] } })
      .then(() => getAll())
      .catch(() => loading.end());
  }

  /**
   * Sort the download clients.
   */
  const sort = debounce(() => {
    const ids = downloaders.map((downloader) => downloader.id);
    api.post('download/manager/sort', { json: { ids } });
  });

  onMount(() => {
    getAll();
  });
</script>

{#snippet addTile()}
  <button class="flex-center size-full min-h-20 cursor-pointer gap-2 p-4 text-sm" onclick={() => creator?.showModal()}>
    <iconify-icon icon={icons.addCircle} width="1.25rem" aria-hidden="true"></iconify-icon>
    {$_('action.add', $_('download.downloader.title'))}
  </button>
{/snippet}

<Grid
  data={downloaders}
  loading={$loading}
  uniqueKey="id"
  class="pull-to-refresh"
  gridClass="grid-cols-cards"
  itemClass="z-1 card-surface card-hover"
  tail={downloaders.length ? addTile : undefined}
  tailClass="card-add"
  skeleton={{ ratio: '3/1', lines: 3 }}
  ondragged={(data) => {
    downloaders = data;
    sort();
  }}
>
  {#snippet item(downloader, index)}
    <EntityCard title={downloader.name} subtitle={address(downloader)} mono>
      {#snippet media()}
        <Image
          transparent
          preset={downloader.preset?.toLowerCase()}
          icon={icons.box3dScanFill}
          width="1.5rem"
          class="[&_iconify-icon]:opacity-100!"
        />
      {/snippet}
      {#snippet actions()}
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
      {/snippet}
      <ul class="flex flex-wrap gap-2">
        <li class="card-tag">
          <span class="size-1.5 shrink-0 rounded-full {STATUS_DOTS[downloader.status]}" aria-hidden="true"></span>
          {$_(`status.${downloader.status}`)}
        </li>
        {#if downloader.version}
          <li class="card-tag font-mono" title={downloader.version}>
            <span class="truncate">{downloader.version}</span>
          </li>
        {/if}
        {#each downloader.source_types as source (source)}
          <li class="card-tag">
            <iconify-icon icon={SOURCE_ICONS[source]} width="0.875rem" aria-hidden="true"></iconify-icon>
            {$_(`download.source.${source}`)}
          </li>
        {/each}
      </ul>
      {#snippet footer()}
        <span class="flex items-center gap-1 text-xs text-base-content/50" title={$_('action.drag_sort')}>
          <iconify-icon icon={icons.sort} width="0.875rem" aria-hidden="true"></iconify-icon>
          #{index + 1}
        </span>
        <Button
          square={false}
          icon={icons.edit}
          text={$_('action.edit')}
          onclick={() => {
            selected = downloader;
            tick().then(() => updater?.showModal());
          }}
        />
      {/snippet}
    </EntityCard>
  {/snippet}
  {#snippet empty()}
    <Empty
      title={$_('empty.downloaders.title')}
      description={$_('empty.downloaders.setup')}
      class="min-h-[calc(var(--ks-svh)-2rem)]"
    >
      <button class="btn h-12 gap-3 font-normal" onclick={() => creator?.showModal()}>
        <iconify-icon icon={icons.addCircle} width="1.5rem" aria-hidden="true"></iconify-icon>
        {$_('action.add', $_('download.downloader.title'))}
      </button>
    </Empty>
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
