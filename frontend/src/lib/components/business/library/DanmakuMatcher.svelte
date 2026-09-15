<script lang="ts">
  import { api } from '$lib/api';
  import { alert, Button, Label, Modal, Overlay } from '$lib/components';
  import { EMPTY_SIGN } from '$lib/constants';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { DanmakuAnime, MediaItem, Resp } from '$lib/types';

  let { item: _item }: { item: MediaItem } = $props();
  let item: MediaItem | null = $state(null);
  let title = $state('');
  let results: DanmakuAnime[] = $state([]);
  let index = $state(-1);
  const searching = createLoading();
  const confirming = createLoading();
  const busy = $derived($searching !== null || $confirming !== null);
  let modal: Modal;

  /**
   * Open the matcher with the media item's title and library configuration.
   */
  export async function showModal() {
    if (busy) {
      return;
    }
    results = [];
    index = -1;
    item = _item.lib ? _item : (await api.get(`media/${_item.id}`).json<Resp<MediaItem>>()).data;
    title = item.title ?? '';
    if (!title.trim()) {
      const { data } = await api
        .get('media/title', { searchParams: { path: item.path } })
        .json<Resp<{ title: string }>>();
      title = data.title || item.name;
    }
    modal.show();
  }

  /**
   * Search anime candidates for the selected media item.
   */
  function search() {
    if (busy || !item || !title.trim()) {
      return;
    }
    if (!item.lib?.danmaku_server) {
      alert({ level: 'warning', message: 'danmaku_server_required' });
      return;
    }
    searching.start();
    results = [];
    index = -1;
    api
      .post('danmaku/anime/search', { json: { path: item.path, title: title.trim() } })
      .json<Resp<DanmakuAnime[]>>()
      .then(({ data }) => {
        results = data;
      })
      .finally(() => searching.end());
  }

  /**
   * Apply the selected anime to all episodes of the media item.
   */
  function confirm() {
    const result = results[index];
    if (busy || !item || !result) {
      return;
    }
    confirming.start();
    api
      .post('danmaku/anime/confirm', { json: { path: item.path, metadata: result } })
      .json<Resp<boolean>>()
      .then(({ data }) => {
        if (data) {
          modal.close();
        } else {
          alert({ level: 'error', message: 'danmaku_match_failed' });
        }
      })
      .finally(() => confirming.end());
  }
</script>

<Modal icon={icons.slideSearch} title={$_('media.danmaku.settings')} maxWidth="36rem" bind:this={modal}>
  <div class="fieldset">
    <Label required>{$_('field.title')}</Label>
    <input
      placeholder={$_('field.title')}
      class="input w-full"
      bind:value={title}
      disabled={busy}
      onkeydown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          search();
        }
      }}
    />
    <div class="px-1 text-xs opacity-50">{item?.path}</div>
    <div class="mt-2 flex justify-end">
      <Button
        ghost={false}
        square={false}
        icon={icons.search}
        text={$_('action.search')}
        class="btn-submit"
        disabled={busy || !title.trim()}
        onclick={search}
      />
    </div>
    <div class="relative mt-2 h-40 overflow-y-auto rounded-box border">
      <Overlay loading={$searching} fixed={false} animation="spinner" />
      <table class="table table-pin-rows table-fixed table-xs">
        <thead>
          <tr class="text-xs font-semibold uppercase">
            <th class="w-8"></th>
            <th>{$_('field.title')}</th>
            <th class="w-28">{$_('field.type')}</th>
          </tr>
        </thead>
        <tbody>
          {#each results as result, i (i)}
            <tr
              class="cursor-pointer hover:bg-base-300 {index === i ? 'bg-primary/15' : ''}"
              onclick={() => {
                if (!busy) {
                  index = index === i ? -1 : i;
                }
              }}
            >
              <td><input type="radio" class="pointer-events-none radio radio-xs" checked={index === i} /></td>
              <td class="truncate font-semibold" title={result.anime_title}>{result.anime_title ?? EMPTY_SIGN}</td>
              <td class="truncate opacity-70" title={result.type_description}>
                {result.type_description || result.type || EMPTY_SIGN}
              </td>
            </tr>
          {:else}
            {#if !$searching}
              <tr>
                <td colspan="3" class="h-32 text-center text-sm opacity-20">{$_('data.nodata')}</td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>
  </div>
  <div class="modal-action">
    <button type="button" class="btn" disabled={$confirming !== null} onclick={() => modal.close()}>
      {$_('message.cancel')}
    </button>
    <button type="button" class="btn btn-submit" disabled={busy || index < 0} onclick={confirm}>
      {$_('message.confirm')}
      {#if $confirming}
        <span class="loading loading-xs loading-dots"></span>
      {/if}
    </button>
  </div>
</Modal>
