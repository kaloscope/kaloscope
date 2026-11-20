<script lang="ts">
  import { api } from '$lib/api';
  import { Modal } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { FlowGraph, MediaLib, Page, Resp } from '$lib/types';

  // the modal dialog instance
  let modal: Modal;
  let userId = $state(0);
  /**
   * Load selected user permissions before opening the editor.
   *
   * @param uid - User ID whose permissions will be edited.
   */
  export const showModal = (uid: number) => {
    userId = uid;
    init().then(() => modal.show());
  };

  // the permissions data and selected ids
  let indexers: FlowGraph[] = $state([]);
  let indexerIds: number[] = $state([]);
  let mediaLibs: MediaLib[] = $state([]);
  let mediaLibIds: number[] = $state([]);

  // the loading state
  const loading = createLoading();

  /**
   * Initialize the component.
   */
  async function init() {
    type Perm = { rel_type: string; rel_id: number };
    const [_indexers, _mediaLibs, _perms] = await Promise.all([
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
        .then((r) => r.data.items)
        .catch(() => []),
      api
        .get('media/lib/list')
        .json<Resp<MediaLib[]>>()
        .then((r) => r.data)
        .catch(() => []),
      api
        .get(`user/${userId}/permissions`)
        .json<Resp<Perm[]>>()
        .then((r) => r.data)
        .catch(() => [])
    ]);
    indexers = _indexers;
    mediaLibs = _mediaLibs;
    indexerIds = _perms.filter((p) => p.rel_type === 'indexer').map((p) => p.rel_id);
    mediaLibIds = _perms.filter((p) => p.rel_type === 'media_lib').map((p) => p.rel_id);
  }

  /**
   * Toggle an ID in the selected list.
   *
   * @param ids - Current selected IDs.
   * @param id - The ID to toggle.
   */
  function toggle(ids: number[], id: number): number[] {
    return ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id];
  }

  /**
   * Save permissions.
   */
  async function save() {
    loading.start();
    try {
      await api.post(`user/${userId}/permissions`, {
        json: { indexer_ids: indexerIds, media_lib_ids: mediaLibIds }
      });
      modal.close();
    } finally {
      loading.end();
    }
  }
</script>

{#snippet permissions(
  title: string,
  items: { id: number; name: string }[],
  ids: number[],
  onToggleOne: (id: number) => void,
  onToggleAll: (checked: boolean) => void
)}
  {@const checked = items.length > 0 && items.length === ids.length}
  {@const indeterminate = ids.length > 0 && ids.length < items.length}
  <div class="my-2">
    <label class="label w-fit {items.length > 0 ? 'cursor-pointer' : 'cursor-not-allowed'}">
      <input
        type="checkbox"
        class="checkbox checkbox-sm"
        {checked}
        {indeterminate}
        disabled={items.length === 0}
        onchange={(event) => onToggleAll(event.currentTarget.checked)}
      />
      <span class="text-sm font-medium text-base-content">{title}</span>
    </label>
    <div class="mt-2 ml-6 flex flex-col gap-2.5">
      {#each items as item (item.id)}
        <label class="label max-w-fit">
          <input
            type="checkbox"
            class="checkbox checkbox-sm"
            checked={ids.includes(item.id)}
            onchange={() => onToggleOne(item.id)}
          />
          <span class="truncate text-sm text-base-content/80">{item.name}</span>
        </label>
      {/each}
    </div>
  </div>
{/snippet}

<Modal icon={icons.personEdit} title={$_('action.assign_permissions')} bind:this={modal}>
  {@render permissions(
    $_('nav.websearch.indexers'),
    indexers,
    indexerIds,
    (id: number) => (indexerIds = toggle(indexerIds, id)),
    (checked: boolean) => (indexerIds = checked ? indexers.map((g) => g.id) : [])
  )}

  {@render permissions(
    $_('entity.media_libs'),
    mediaLibs,
    mediaLibIds,
    (id: number) => (mediaLibIds = toggle(mediaLibIds, id)),
    (checked: boolean) => (mediaLibIds = checked ? mediaLibs.map((l) => l.id) : [])
  )}

  <div class="modal-action">
    <button type="button" class="btn" onclick={() => modal.close()}>
      {$_('message.cancel')}
    </button>
    <button class="btn btn-submit" onclick={save} disabled={$loading !== null}>
      {$_('action.save')}
      {#if $loading}
        <span class="loading loading-xs loading-dots"></span>
      {/if}
    </button>
  </div>
</Modal>
