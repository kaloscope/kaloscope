<script lang="ts" module>
  import type { MediaItem } from '$lib/types';
  import type { IconifyIcon } from 'iconify-icon';
  import type { MouseEventHandler } from 'svelte/elements';

  export type MediaActionsProps = {
    item: MediaItem;
    /** Library holding the item, needed to review its deletion. */
    libraryId: number;
    class?: string;
    triggerClass?: string;
    onclick?: () => void;
    onscrape?: () => void;
    ondelete?: () => void;
    danmaku?: boolean;
  };
</script>

<script lang="ts">
  import { BatchPlanDialog, DanmakuMatcher, Dropdown, MetadataScraper, alert } from '$lib/components';
  import { closeDropdowns } from '$lib/components/common/interaction/Dropdown.svelte';
  import { _, locale, number } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { describeEmptyReview, reviewItemDeletion } from '$lib/utils/media-batches';
  import { mediaTitle } from '$lib/utils/media';
  import { tick } from 'svelte';

  let {
    item,
    libraryId,
    class: _class,
    triggerClass,
    onclick,
    onscrape,
    ondelete,
    danmaku = false
  }: MediaActionsProps = $props();
  let matcher: DanmakuMatcher | null = $state(null);
  let scraper: MetadataScraper | null = $state(null);
  let deleter: BatchPlanDialog | null = $state(null);
  // the plan dialog mounts on the first deletion, not once per card
  let deleting = $state(false);

  /**
   * Review deleting this item, keeping its files by default, and show the plan.
   */
  async function reviewDeletion() {
    const outcome = await reviewItemDeletion(libraryId, [item.id]);
    if (!outcome.batch_id) {
      alert({ level: 'warning', message: describeEmptyReview(outcome, $_, $number, $locale) });
      return;
    }
    deleting = true;
    await tick();
    deleter?.showModal(outcome.batch_id, { title: mediaTitle(item) });
  }
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
    {#if danmaku}
      {@render action(icons.slideSearch, $_('media.danmaku.settings'), () => {
        matcher?.showModal();
      })}
    {/if}
    {#if onscrape}
      {@render action(icons.imageSearch, $_('action.scrape'), () => {
        scraper?.showModal();
      })}
    {/if}
    {#if ondelete}
      {@render action(icons.delete, $_('action.delete'), () => {
        void reviewDeletion();
      })}
    {/if}
  </ul>
</Dropdown>

{#if danmaku}
  <DanmakuMatcher bind:this={matcher} {item} />
{/if}

{#if onscrape}
  <MetadataScraper bind:this={scraper} {item} {onscrape} />
{/if}

{#if ondelete && deleting}
  <BatchPlanDialog bind:this={deleter} onfinish={ondelete} />
{/if}
