<script lang="ts">
  import { api } from '$lib/api';
  import { Button, Dropdown, Empty, EntityCard, Grid, MediaLibEditor, confirm } from '$lib/components';
  import { LibType } from '$lib/enums';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { MediaLib, Resp } from '$lib/types';
  import { debounce } from '$lib/utils/timing';
  import { onMount, tick } from 'svelte';

  let libs: MediaLib[] = $state([]);
  let creator: MediaLibEditor | null = $state(null);
  let updater: MediaLibEditor | null = $state(null);
  let selected: MediaLib | null = $state(null);
  const loading = createLoading();

  let disposed = false;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * Get media libraries.
   *
   * @param retry - Whether this search is a retry attempt.
   */
  function getAll(retry: boolean = false) {
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (!retry) loading.start();
    api
      .get('media/lib/list')
      .json<Resp<MediaLib[]>>()
      .then(({ data }) => {
        if (disposed) return;
        libs = data;
        // keep polling until queued scans finish, including their retries
        if (libs.some((lib) => lib.scanning)) {
          retryTimer = setTimeout(() => getAll(true), 2000);
        }
      })
      .finally(() => loading.end());
  }

  /**
   * Scan a media library by ID.
   *
   * @param id - Media library ID.
   */
  function scan(id: number) {
    loading.start();
    api
      .get(`media/lib/${id}/scan`)
      .then(() => getAll())
      .catch(() => loading.end());
  }

  /**
   * Delete a media library by ID.
   *
   * @param id - Media library ID.
   */
  function del(id: number) {
    loading.start();
    api
      .post('media/lib/delete', { json: { ids: [id] } })
      .then(() => getAll())
      .catch(() => loading.end());
  }

  /**
   * Sort the media libraries.
   */
  const sort = debounce(() => {
    const ids = libs.map((lib) => lib.id);
    api.post('media/lib/sort', { json: { ids } });
  });

  onMount(() => {
    getAll();
    // show settings the assistant saved; an open editor keeps its own form
    const changed = () => getAll();
    window.addEventListener('media-library-changed', changed);
    return () => {
      disposed = true;
      window.removeEventListener('media-library-changed', changed);
      if (retryTimer) clearTimeout(retryTimer);
    };
  });
</script>

{#snippet addTile()}
  <button class="flex-center size-full min-h-20 cursor-pointer gap-2 p-4 text-sm" onclick={() => creator?.showModal()}>
    <iconify-icon icon={icons.addCircle} width="1.25rem" aria-hidden="true"></iconify-icon>
    {$_('action.add', $_('entity.media_lib'))}
  </button>
{/snippet}

<Grid
  data={libs}
  loading={$loading}
  uniqueKey="id"
  class="pull-to-refresh"
  gridClass="grid-cols-cards"
  itemClass="z-1 card-surface card-hover"
  tail={libs.length ? addTile : undefined}
  tailClass="card-add"
  skeleton={{ ratio: '3/1', lines: 3 }}
  ondragged={(data) => {
    libs = data;
    sort();
  }}
>
  {#snippet item(lib, index)}
    {@const libType = LibType[lib.lib_type]}
    <EntityCard title={lib.name} subtitle={lib.dir} mono truncateStart icon={libType.icon} tone={libType.iconColor}>
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
                  const temp = libs[index];
                  libs[index] = libs[index - 1];
                  libs[index - 1] = temp;
                  sort();
                }}
              >
                <iconify-icon icon={icons.arrowUp} width="1rem"></iconify-icon>
                {$_('action.move_up')}
              </button>
            </li>
            <li class={index === libs.length - 1 ? 'menu-disabled' : ''}>
              <button
                class="px-2"
                onclick={() => {
                  const temp = libs[index];
                  libs[index] = libs[index + 1];
                  libs[index + 1] = temp;
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
                    title: `${$_('action.delete', $_('entity.media_lib'))} [${lib.name}]`,
                    onconfirm: () => del(lib.id)
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
        {#if lib.scanning}
          <li class="card-tag">
            <span class="loading loading-xs loading-spinner text-primary" aria-hidden="true"></span>
            {$_('media.scanning')}
          </li>
        {/if}
        {#if lib.language}
          <li class="card-tag">
            <iconify-icon icon={icons.language} width="0.875rem" aria-hidden="true"></iconify-icon>
            {$_(lib.language, { locale: 'languages' })}
          </li>
        {/if}
        <li class="card-tag" title={lib.triggers.map((trigger) => trigger.graph_name).join('\n')}>
          <iconify-icon icon={icons.flowchart} width="0.875rem" aria-hidden="true"></iconify-icon>
          {$_('flow.trigger.bound_count', lib.triggers.length)}
        </li>
        {#if lib.scan_on_startup}
          <li class="card-tag">
            <iconify-icon icon={icons.arrowSyncCircle} width="0.875rem" aria-hidden="true"></iconify-icon>
            {$_('media.scan_on_startup')}
          </li>
        {/if}
        {#if lib.danmaku_server}
          <li class="card-tag" title={lib.danmaku_server}>
            <iconify-icon icon={icons.danmaku} width="0.875rem" aria-hidden="true"></iconify-icon>
            {$_('media.danmaku.settings')}
          </li>
        {/if}
      </ul>
      {#snippet footer()}
        <span class="flex items-center gap-1 text-xs text-base-content/50" title={$_('action.drag_sort')}>
          <iconify-icon icon={icons.sort} width="0.875rem" aria-hidden="true"></iconify-icon>
          #{index + 1}
        </span>
        <div class="flex items-center gap-1">
          <Button
            square={false}
            disabled={lib.scanning}
            icon={icons.folderSearch}
            text={$_('action.scan')}
            onclick={() => {
              scan(lib.id);
            }}
          />
          <Button
            square={false}
            icon={icons.edit}
            text={$_('action.edit')}
            onclick={() => {
              selected = lib;
              tick().then(() => updater?.showModal());
            }}
          />
        </div>
      {/snippet}
    </EntityCard>
  {/snippet}
  {#snippet empty()}
    <Empty
      title={$_('empty.medialibs.title')}
      description={$_('empty.medialibs.setup')}
      class="min-h-[calc(var(--ks-svh)-2rem)]"
    >
      <button class="btn h-12 gap-3 font-normal" onclick={() => creator?.showModal()}>
        <iconify-icon icon={icons.addCircle} width="1.5rem" aria-hidden="true"></iconify-icon>
        {$_('action.add', $_('entity.media_lib'))}
      </button>
    </Empty>
  {/snippet}
</Grid>

<MediaLibEditor bind:this={creator} onsave={() => getAll()} />

{#if selected}
  <MediaLibEditor bind:this={updater} {...selected} onsave={() => getAll()} />
{/if}
