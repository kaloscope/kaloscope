<script lang="ts" module>
  import type { IconifyIcon } from 'iconify-icon';
  import type { Snippet } from 'svelte';
  import type { MouseEventHandler } from 'svelte/elements';

  export type BadgeProps = {
    /** The badge text snippet. */
    children: Snippet;
    /** The badge icon. */
    icon?: string | IconifyIcon;
    /** The badge icon color. */
    iconColor?: string | null;
    /** The color of a status dot before the text; no dot when omitted. */
    dot?: keyof typeof DOTS;
    /** Whether to use uppercase text. */
    uppercase?: boolean;
    /** Whether to draw a dashed outline instead of a fill, as for drafts. */
    dashed?: boolean;
    /** The class names for the badge. */
    class?: string;
    /** The class names for the icon. */
    iconClass?: string;
    /** The click event handler. */
    onclick?: MouseEventHandler<HTMLDivElement>;
  };

  // full class names so Tailwind can find them
  const DOTS = {
    success: 'bg-success',
    warning: 'bg-warning',
    error: 'bg-error',
    info: 'bg-info',
    neutral: 'bg-base-content/40'
  };
</script>

<script lang="ts">
  let {
    children,
    icon,
    iconColor,
    dot,
    uppercase = false,
    dashed = false,
    class: _class,
    iconClass,
    onclick
  }: BadgeProps = $props();
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div
  tabindex={onclick ? 0 : undefined}
  role={onclick ? 'button' : 'generic'}
  {onclick}
  class="tag {dashed ? 'tag-dashed' : ''} {uppercase ? 'uppercase' : ''} {onclick ? 'btn-scale' : ''} {_class}"
>
  {#if dot}
    <span class="size-1.5 shrink-0 rounded-full {DOTS[dot]}" aria-hidden="true"></span>
  {/if}
  {#if icon}
    <iconify-icon {icon} width="0.875rem" class="size-3.5 shrink-0 {iconClass}" style:color={iconColor}></iconify-icon>
  {/if}
  {@render children()}
</div>
