<script lang="ts">
  import { api } from '$lib/api';
  import { Button, Dropdown, Grid, Image, MediaLibEditor, confirm } from '$lib/components';
  import { LibType } from '$lib/enums';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { MediaLib, Resp } from '$lib/types';
  import { debounce } from '$lib/utils';
  import { onMount, tick } from 'svelte';

  let libs: MediaLib[] = $state([]);
  let creator: MediaLibEditor | null = $state(null);
  let updater: MediaLibEditor | null = $state(null);
  let selected: MediaLib | null = $state(null);
  const loading = createLoading();

  let disposed = false;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * Get the media libraries.
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
   * @param id - The media library ID.
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
   * @param id - The media library ID.
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
    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
    };
  });
</script>

{#snippet term(name: string, description: string | null, hiddenLeft?: boolean)}
  <div class="flex items-center justify-between gap-4 py-2" title={description}>
    <dt class="whitespace-nowrap">{name}:</dt>
    <dd class="truncate opacity-70 {hiddenLeft ? 'direction-rtl' : ''}">
      {hiddenLeft && description ? description.split('').reverse().join('') : description}
    </dd>
  </div>
{/snippet}

<Grid
  data={libs}
  loading={$loading}
  uniqueKey="id"
  class="pull-to-refresh"
  gridClass="grid-cols-sparse"
  itemClass="z-1 rounded-field border shadow-sm hover:shadow-lg"
  tailClass="rounded-field border-1 border-dashed duration-300 opacity-20 hover:opacity-50 hover:shadow-lg"
  ondragged={(data) => {
    libs = data;
    sort();
  }}
>
  {#snippet item(lib, index)}
    {@const libType = LibType[lib.lib_type]}
    <div class="flex justify-between gap-2 rounded-t-field bg-base-200 p-4">
      <div class="grid grid-flow-col items-center gap-2">
        <Image transparent icon={libType.icon} width="2rem" class="[&_iconify-icon]:opacity-70!" />
        <div class="truncate text-base">{lib.name}</div>
      </div>
      <div class="flex items-center gap-1">
        <Button
          loading={lib.scanning}
          icon={icons.folderSearch}
          text={$_('action.scan')}
          onclick={() => {
            scan(lib.id);
          }}
        />
        <Button
          icon={icons.edit}
          onclick={() => {
            selected = lib;
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
      </div>
    </div>
    <dl class="rounded-b-field bg-base-100 p-4 text-sm">
      {@render term($_('field.dir'), lib.dir, true)}
      <div class="divider my-0"></div>
      {@render term($_('field.language'), $_(lib.language, { locale: 'languages' }))}
      <div class="divider my-0"></div>
      {@render term($_('flow.trigger.count'), String(lib.triggers.length))}
    </dl>
  {/snippet}
  {#snippet tail()}
    <button class="flex-col-center size-full cursor-pointer gap-2 p-4" onclick={() => creator?.showModal()}>
      <iconify-icon icon={icons.addCircle} width="2.5rem"></iconify-icon>
      <span class="text-2xl">{$_('action.add')}</span>
    </button>
  {/snippet}
</Grid>

<MediaLibEditor bind:this={creator} onsave={() => getAll()} />

{#if selected}
  <MediaLibEditor bind:this={updater} {...selected} onsave={() => getAll()} />
{/if}
