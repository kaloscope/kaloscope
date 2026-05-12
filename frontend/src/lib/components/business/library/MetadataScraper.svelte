<script lang="ts" module>
  import { LibType } from '$lib/enums';
  import type { MediaItem, Resp } from '$lib/types';

  type MetadataScraperProps = {
    item: MediaItem;
    onscrape?: () => void;
  };

  type ScrapeResult = {
    title: string | null;
    plot: string | null;
    year: number | null;
    rating: number | null;
  } & Record<string, unknown>;

  const NFO_TYPES: Record<keyof typeof LibType, string> = {
    movie: 'movie',
    tv_show: 'tvshow'
  };

  /**
   * Get the corresponding NFO type for the given library type.
   *
   * @param libType - The library type.
   * @returns The corresponding NFO type.
   */
  function getNFOType(libType: keyof typeof LibType | null | undefined): string | null {
    if (!libType) {
      return null;
    }
    return NFO_TYPES[libType] ?? null;
  }
</script>

<script lang="ts">
  import { api } from '$lib/api';
  import { Button, Label, Modal, Overlay, Select } from '$lib/components';
  import { EMPTY_SIGN } from '$lib/constants';
  import { createLoading } from '$lib/helpers';
  import { _, locales } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { fixedNumber } from '$lib/utils';

  let { item: _item, onscrape }: MetadataScraperProps = $props();

  // the media item
  let item: MediaItem | null = $state(null);

  // the graph options
  let graphOptions = $derived.by(() => {
    const triggers = item?.lib?.triggers ?? [];
    return triggers.map((t) => ({ value: t.graph_id, label: t.graph_name }));
  });

  // the query conditions
  let graphId: number | null = $state(null);
  let title: string = $state('');
  let year: number | null = $state(null);
  let season: number | null = $state(null);
  let language: string = $state('');

  // the search results
  let results: ScrapeResult[] = $state([]);
  let index: number = $state(-1);
  const searching = createLoading();
  const confirming = createLoading();

  // the modal dialog instance
  let modal: Modal;
  export const showModal = () => {
    init().then(() => modal.show());
  };

  /**
   * Initialize the component.
   */
  async function init() {
    results = [];
    index = -1;

    // try to fetch the complete media item if only partial data is provided
    try {
      item = _item.lib ? _item : (await api.get(`media/${_item.id}`).json<Resp<MediaItem>>()).data;
    } catch (error) {
      console.error(error);
      item = _item;
    }

    // pre-fill the form with inferred metadata and workflow options
    graphId = item.lib?.triggers?.[0]?.graph_id ?? null;
    title = item.title ?? '';
    year = item.year ?? null;
    season = item.season ?? null;
    language = item.lib?.language ?? '';

    // if the title is still empty, try to infer it from the file path
    if (!title.trim()) {
      const resp = await api
        .get('media/title', {
          searchParams: { path: item.path }
        })
        .json<Resp<{ title: string }>>();
      title = resp.data?.title ?? '';
    }
  }

  /**
   * Search metadata candidates with the selected ingest workflow.
   */
  function preview() {
    if ($searching !== null || !item || !graphId || !title.trim()) {
      return;
    }
    searching.start();
    results = [];
    index = -1;
    api
      .post(`flow/graph/${graphId}/execute`, {
        json: {
          $manual: true,
          item_path: item.path,
          item_name: item.name,
          nfo_type: getNFOType(item.lib?.lib_type),
          language: language || null,
          title: title.trim(),
          year: year || null,
          season: season ?? 1,
          page_num: 1,
          page_size: 5
        }
      })
      .json<Resp<ScrapeResult[]>>()
      .then((resp) => {
        results = resp.data;
      })
      .finally(() => {
        searching.end();
      });
  }

  /**
   * Confirm the selected metadata result.
   */
  function confirm() {
    if ($confirming !== null || index < 0) {
      return;
    }
    const result = results[index];
    if (!result) {
      return;
    }
    confirming.start();
    api
      .post(`media/${item?.id}/gen_nfo`, {
        json: { graph_id: graphId, metadata: result }
      })
      .then(() => {
        modal.close();
        onscrape?.();
      })
      .finally(() => {
        confirming.end();
      });
  }
</script>

<Modal
  icon={icons.boxMultipleSearch}
  title={$_('action.scrape', $_('entity.metadata'))}
  maxWidth="36rem"
  bind:this={modal}
>
  <div class="fieldset">
    <Label required>{$_('field.graph')}</Label>
    <Select options={graphOptions} bind:value={graphId} class="w-full" />
    <Label required>{$_('field.title')}</Label>
    <input placeholder={$_('field.title')} class="input w-full" bind:value={title} />
    <div class="px-1 text-xs opacity-50">{item?.path}</div>
    <div class="flex flex-wrap gap-2">
      <div class="flex-1 space-y-1.5">
        <Label>{$_('field.year')}</Label>
        <input
          type="number"
          placeholder={$_('field.year')}
          class="input w-full"
          bind:value={year}
          min={1900}
          max={2999}
        />
      </div>
      {#if item?.lib?.lib_type === 'tv_show'}
        <div class="flex-1 space-y-1.5">
          <Label>{$_('field.season')}</Label>
          <input
            type="number"
            placeholder={$_('field.season')}
            class="input w-full"
            bind:value={season}
            min={0}
            max={99}
          />
        </div>
      {/if}
      <div class="flex-1 space-y-1.5">
        <Label>{$_('field.language')}</Label>
        <Select bind:value={language} class="w-full">
          <option value="">{$_('enum.none')}</option>
          {#each $locales.filter((l) => l !== 'languages') as code (code)}
            <option value={code}>{$_(code, { locale: 'languages' })}</option>
          {/each}
        </Select>
      </div>
    </div>
    <div class="mt-2 flex justify-end">
      <Button
        ghost={false}
        square={false}
        icon={icons.search}
        text={$_('action.search')}
        class="btn-submit"
        disabled={$searching || !item || !graphId || !title.trim()}
        onclick={preview}
      />
    </div>
    <div class="relative mt-2 h-40 overflow-y-auto rounded-box border">
      <Overlay loading={$searching} fixed={false} animation="spinner" />
      <table class="table-pin-rows table table-fixed table-xs">
        <thead>
          <tr class="text-xs font-semibold uppercase">
            <th class="w-8"></th>
            <th class="w-1/4">{$_('field.title')}</th>
            <th class="w-16">{$_('field.year')}</th>
            <th class="w-16">{$_('field.rating')}</th>
            <th>{$_('field.plot')}</th>
          </tr>
        </thead>
        <tbody>
          {#if results.length > 0}
            {#each results as result, i (i)}
              {@const rating = fixedNumber(result.rating, 1, 0, 10)}
              <tr
                class="cursor-pointer hover:bg-base-300 {index === i ? 'bg-primary/15' : ''}"
                onclick={() => (index = index === i ? -1 : i)}
              >
                <td><input type="radio" class="pointer-events-none radio radio-xs" checked={index === i} /></td>
                <td class="truncate font-semibold" title={result.title}>{result.title ?? EMPTY_SIGN}</td>
                <td class="truncate opacity-70">{result.year ?? EMPTY_SIGN}</td>
                <td class="truncate opacity-70">{rating ?? EMPTY_SIGN}</td>
                <td class="truncate opacity-70" title={result.plot}>{result.plot || EMPTY_SIGN}</td>
              </tr>
            {/each}
          {:else if !$searching}
            <tr>
              <td colspan="5" class="h-32 text-center text-sm opacity-20">
                {$_('data.nodata')}
              </td>
            </tr>
          {/if}
        </tbody>
      </table>
    </div>
  </div>
  <div class="modal-action">
    <button type="button" class="btn" onclick={() => modal.close()}>
      {$_('message.cancel')}
    </button>
    <button type="button" class="btn btn-submit" disabled={$confirming !== null || index < 0} onclick={confirm}>
      {$_('message.confirm')}
      {#if $confirming}
        <span class="loading loading-xs loading-dots"></span>
      {/if}
    </button>
  </div>
</Modal>
