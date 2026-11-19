<script lang="ts" module>
  import type { IconifyIcon } from 'iconify-icon';
  import type { Snippet } from 'svelte';

  export type EntityCardProps = {
    /** The card title. */
    title: string;
    /** The tooltip of the title, which defaults to the title itself. */
    tip?: string | null;
    /** The secondary line under the title. */
    subtitle?: string | null;
    /** Whether to show the subtitle in a monospaced font. */
    mono?: boolean;
    /** Whether to cut the subtitle at its start, keeping the end of a path visible. */
    truncateStart?: boolean;
    /** The icon shown in the leading tile when no `media` snippet is given. */
    icon?: string | IconifyIcon;
    /** The color that tints the leading tile. */
    tone?: string | null;
    /** The custom content of the leading tile, such as a logo. */
    media?: Snippet;
    /** The trailing header content, such as a menu or a badge. */
    actions?: Snippet;
    /** The card body, such as tags or metadata. */
    children?: Snippet;
    /** The card footer, laid out as a row with space between its items. */
    footer?: Snippet;
    /** The class names for the card. */
    class?: string;
  };
</script>

<script lang="ts">
  let {
    title,
    tip,
    subtitle,
    mono = false,
    truncateStart = false,
    icon,
    tone,
    media,
    actions,
    children,
    footer,
    class: _class
  }: EntityCardProps = $props();
</script>

<!-- the surface comes from the container, so dragged grid items keep their placeholder -->
<div class="flex h-full min-w-0 flex-col {_class}">
  <div class="flex flex-1 flex-col gap-4 p-4">
    <div class="flex min-w-0 items-center gap-3">
      <div class="flex-center size-10 shrink-0 icon-tile" style:--tone={tone}>
        {#if media}
          {@render media()}
        {:else if icon}
          <iconify-icon {icon} width="1.5rem" aria-hidden="true"></iconify-icon>
        {/if}
      </div>
      <div class="min-w-0 flex-1">
        <h3 class="truncate leading-snug font-medium" title={tip ?? title}>{title}</h3>
        {#if subtitle}
          <p
            class="mt-0.5 truncate text-xs text-base-content/60 {mono ? 'font-mono' : ''}"
            class:truncate-start={truncateStart}
            title={subtitle}
          >
            {#if truncateStart}
              <bdi dir="ltr">{subtitle}</bdi>
            {:else}
              {subtitle}
            {/if}
          </p>
        {/if}
      </div>
      {#if actions}
        <div class="flex shrink-0 items-center gap-1">
          {@render actions()}
        </div>
      {/if}
    </div>
    {#if children}
      {@render children()}
    {/if}
  </div>
  {#if footer}
    <div class="flex min-h-12 items-center justify-between gap-2 border-t border-base-content/8 py-2 pr-2 pl-4">
      {@render footer()}
    </div>
  {/if}
</div>

<style>
  /* an RTL box puts the ellipsis before the text, while `bdi` keeps the text itself LTR */
  .truncate-start {
    direction: rtl;
    text-align: left;
  }
</style>
