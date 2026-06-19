<script lang="ts" module>
  import type { MediaItem } from '$lib/types';
  import type { IconifyIcon } from 'iconify-icon';
  import type { MouseEventHandler } from 'svelte/elements';

  export type MediaActionsProps = {
    item: MediaItem;
    class?: string;
    triggerClass?: string;
    onclick?: () => void;
    onscrape?: () => void;
    ondelete?: () => void;
  };
</script>

<script lang="ts">
  import { Dropdown, MediaDelConfirm, MetadataScraper } from '$lib/components';
  import { closeDropdowns } from '$lib/components/common/interaction/Dropdown.svelte';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';

  let { item, class: _class, triggerClass, onclick, onscrape, ondelete }: MediaActionsProps = $props();
  let scraper: MetadataScraper | null = $state(null);
  let deleter: MediaDelConfirm | null = $state(null);
</script>

{#snippet action(icon: IconifyIcon, text: string, onclick: MouseEventHandler<HTMLElement>)}
  <li>
    <button
      class="px-2"
      onclick={(event) => {
        event.stopPropagation();
        closeDropdowns();
        onclick?.(event);
      }}
    >
      <iconify-icon {icon} width="1rem" class="size-4"></iconify-icon>
      {text}
    </button>
  </li>
{/snippet}

<Dropdown
  contentWidth="8rem"
  contentClass="shadow-lg!"
  class={_class}
  onclick={(event) => {
    event.stopPropagation();
    closeDropdowns(event.currentTarget);
    onclick?.();
  }}
>
  {#snippet trigger()}
    <div class="btn btn-circle border-0 btn-subtle btn-sm {triggerClass}">
      <iconify-icon icon={icons.moreVertical} width="1.25rem"></iconify-icon>
    </div>
  {/snippet}
  <ul class="menu gap-1">
    {#if onscrape}
      {@render action(icons.boxMultipleSearch, $_('action.scrape'), () => {
        // scrape metadata for the media item
        scraper?.showModal();
      })}
    {/if}
    {#if ondelete}
      {@render action(icons.delete, $_('action.delete'), () => {
        // show delete confirm dialog
        deleter?.showModal(item);
      })}
    {/if}
  </ul>
</Dropdown>

{#if onscrape}
  <MetadataScraper bind:this={scraper} {item} {onscrape} />
{/if}

{#if ondelete}
  <MediaDelConfirm bind:this={deleter} onconfirm={ondelete} />
{/if}
