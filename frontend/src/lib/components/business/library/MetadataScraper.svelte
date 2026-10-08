<script lang="ts" module>
  import { LibType } from '$lib/enums';
  import type { MediaItem, Resp } from '$lib/types';

  type MetadataScraperProps = {
    item: MediaItem;
    onscrape?: () => void;
  };

  type ScrapeResult = {
    title: string | null;
    plot?: string | null;
    year?: number | null;
    rating?: number | string | null;
    authors?: string[];
  } & Record<string, unknown>;

  const NFO_TYPES: Partial<Record<keyof typeof LibType, string>> = {
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
  import { onDestroy } from 'svelte';

  let { item: _item, onscrape }: MetadataScraperProps = $props();

  // the media item
  let item: MediaItem | null = $state(null);
  const reading = $derived.by(() => item?.media_type === 'text' || item?.media_type === 'image');
  const comic = $derived.by(() => item?.lib?.lib_type === 'comic');

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
  let seriesTitle = $state('');
  let number = $state('');

  // the search results
  let results: ScrapeResult[] = $state([]);
  let index: number = $state(-1);
  const searching = createLoading();
  const confirming = createLoading();
  let initializing = $state(false);
  const busy = $derived(initializing || $searching !== null || $confirming !== null);
  let controller: AbortController | undefined;

  // the modal dialog instance
  let modal: Modal;
  export async function showModal() {
    if (busy) return;
    initializing = true;
    controller = new AbortController();
    const signal = controller.signal;
    try {
      await init(signal);
      if (!signal.aborted) modal.show();
    } catch (error) {
      if (!signal.aborted) console.error(error);
    } finally {
      initializing = false;
    }
  }

  /**
   * Initialize the component.
   *
   * @param signal - Cancels reads when this dialog is discarded.
   */
  async function init(signal: AbortSignal) {
    results = [];
    index = -1;

    // read current file metadata and library bindings on every open
    item = (await api.get(`media/${_item.id}`, { signal }).json<Resp<MediaItem>>()).data;

    // pre-fill the form with inferred metadata and workflow options
    graphId = item.lib?.triggers?.[0]?.graph_id ?? null;
    title = item.title ?? '';
    year = item.year ?? null;
    season = item.season ?? null;
    language = (reading ? item.metadata?.language : null) || item.lib?.language || '';
    seriesTitle = item.metadata?.series ?? item.parent?.title ?? item.parent?.name ?? '';
    number = item.metadata?.number ?? '';

    // if the title is still empty, try to infer it from the file path
    if (!title.trim()) {
      const resp = await api
        .get('media/title', {
          signal,
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
    if (busy || !item || !graphId || !title.trim()) {
      return;
    }
    searching.start();
    results = [];
    index = -1;
    controller?.abort();
    const request = new AbortController();
    controller = request;
    api
      .post(`flow/graph/${graphId}/execute`, {
        signal: request.signal,
        json: {
          $manual: true,
          ...(reading
            ? {
                item_id: item.id,
                lib_type: item.lib?.lib_type,
                item_role: item.item_role,
                series_title: comic ? seriesTitle.trim() || null : null,
                number: comic ? number.trim() || null : null
              }
            : {}),
          item_path: item.path,
          item_name: item.name,
          nfo_type: getNFOType(item.lib?.lib_type),
          language: language || null,
          title: title.trim(),
          year: year || null,
          season: reading ? null : (season ?? 1),
          page_num: 1,
          page_size: 5
        }
      })
      .json<Resp<ScrapeResult[]>>()
      .then(({ data }) => {
        if (!request.signal.aborted) results = Array.isArray(data) ? data : [];
      })
      .catch((error) => {
        if (!request.signal.aborted) console.error(error);
      })
      .finally(() => {
        if (controller === request) searching.end();
      });
  }

  /**
   * Confirm the selected metadata result.
   */
  function confirm() {
    if (busy || !item || !graphId || index < 0) {
      return;
    }
    const result = results[index];
    if (!result) {
      return;
    }
    confirming.start();
    api
      .post(`media/${item.id}/${reading ? 'metadata' : 'gen_nfo'}`, {
        json: { graph_id: graphId, metadata: result }
      })
      .then(() => {
        modal.close();
        onscrape?.();
      })
      .catch(console.error)
      .finally(() => {
        confirming.end();
      });
  }

  /** Cancel discarded previews without interrupting a confirmed file save. */
  function cancelPreview() {
    controller?.abort();
    controller = undefined;
    searching.end();
  }

  onDestroy(cancelPreview);
</script>

<Modal
  icon={icons.imageSearch}
  title={$_('action.scrape', $_('entity.metadata'))}
  maxWidth={reading ? '42rem' : '36rem'}
  onclose={() => {
    if (!modal.isOpen()) cancelPreview();
  }}
  bind:this={modal}
>
  <fieldset class="fieldset" disabled={busy}>
    <Label required>{$_('field.graph')}</Label>
    <Select
      options={graphOptions}
      bind:value={graphId}
      onchange={() => {
        results = [];
        index = -1;
      }}
      class="w-full"
    />
    <Label required>{$_('field.title')}</Label>
    <input placeholder={$_('field.title')} class="input w-full" bind:value={title} />
    <div class="px-1 text-xs opacity-50">{item?.path}</div>
    {#if comic}
      <div class="flex flex-wrap gap-2">
        <div class="min-w-0 flex-1 space-y-1.5">
          <Label>{$_('metadata.fields.series')}</Label>
          <input placeholder={$_('metadata.fields.series')} class="input w-full" bind:value={seriesTitle} />
        </div>
        <div class="min-w-0 flex-1 space-y-1.5">
          <Label>{$_('metadata.fields.number')}</Label>
          <input placeholder={$_('metadata.fields.number')} class="input w-full" bind:value={number} />
        </div>
      </div>
    {/if}
    <div class="flex flex-wrap gap-2">
      <div class="flex-1 space-y-1.5">
        <Label>{$_('field.year')}</Label>
        <input
          type="number"
          placeholder={$_('field.year')}
          class="input w-full"
          bind:value={year}
          min={reading ? 1 : 1900}
          max={reading ? 9999 : 2999}
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
          {#if language && !$locales.includes(language)}
            <option value={language}>{$_(language, { locale: 'languages', default: language })}</option>
          {/if}
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
        disabled={busy || !item || !graphId || !title.trim()}
        onclick={preview}
      />
    </div>
    <div class="relative mt-2 h-40 w-full overflow-y-auto rounded-box border">
      <Overlay loading={$searching} fixed={false} animation="spinner" />
      <table class="table table-pin-rows table-fixed table-xs">
        <thead>
          <tr class="text-xs font-semibold text-base-content/40 uppercase">
            <th class="w-6 sm:w-8"></th>
            <th class="w-1/4">{$_('field.title')}</th>
            {#if reading}
              <th class="w-1/5">{$_('metadata.fields.authors')}</th>
            {/if}
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
                onclick={() => {
                  if (!busy) index = index === i ? -1 : i;
                }}
              >
                <td><input type="radio" class="pointer-events-none radio radio-xs" checked={index === i} /></td>
                <td class="truncate font-medium text-base-content/80" title={result.title}>
                  {result.title || EMPTY_SIGN}
                </td>
                {#if reading}
                  <td class="truncate text-base-content/60" title={result.authors?.join(', ')}>
                    {result.authors?.join(', ') || EMPTY_SIGN}
                  </td>
                {/if}
                <td class="truncate text-base-content/60">{result.year ?? EMPTY_SIGN}</td>
                <td class="truncate text-base-content/60">{rating ?? EMPTY_SIGN}</td>
                <td class="truncate text-base-content/60" title={result.plot}>{result.plot || EMPTY_SIGN}</td>
              </tr>
            {/each}
          {:else if $searching === null}
            <tr>
              <td colspan={reading ? 6 : 5} class="h-32 text-center text-sm opacity-20">
                {$_('data.nodata')}
              </td>
            </tr>
          {/if}
        </tbody>
      </table>
    </div>
  </fieldset>
  <div class="modal-action">
    <button type="button" class="btn" onclick={() => modal.close()}>
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
