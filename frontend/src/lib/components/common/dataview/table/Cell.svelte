<script lang="ts" module>
  import type { Action } from '$lib/components';
  import type { Snippet } from 'svelte';
  import type { MouseEventHandler } from 'svelte/elements';

  export type CellProps = Partial<{
    /** Whether to render the cell. */
    condition: boolean;
    /** The cell snippet to render. */
    children: Snippet;
    /** The cell text to display. */
    text: string | null;
    /** The link to open when the cell text is clicked. */
    link: string | null;
    /** Whether the cell text should wrap. */
    wrap: boolean;
    /** The action buttons. */
    actions: Action[];
    /** The class names for the cell. */
    class: string;
    /** The click event handler. */
    onclick: MouseEventHandler<HTMLTableCellElement>;
  }>;
</script>

<script lang="ts">
  import { ActionMenu, Button } from '$lib/components';
  import { EMPTY_SIGN } from '$lib/constants';

  let {
    condition = true,
    children,
    text,
    link,
    wrap = false,
    actions: _actions,
    class: _class,
    onclick
  }: CellProps = $props();

  // the filtered action buttons based on their condition
  let buttons: Action[] | undefined = $derived(_actions?.filter((a) => a.condition !== false));
</script>

{#if condition}
  <td title={text} class="px-0" {onclick}>
    <div class="flex items-center gap-2 pl-2 {buttons ? 'justify-center pr-2' : ''} {_class}">
      {#if children}
        {@render children()}
      {:else if buttons}
        {@render actions(buttons)}
      {:else if link}
        <a href={link} target="_blank" class="link link-hover opacity-80 {wrap ? 'overflow-hidden' : 'truncate'}">
          {text ?? EMPTY_SIGN}
        </a>
      {:else}
        <span class="opacity-80 {wrap ? 'overflow-hidden' : 'truncate'}">{text ?? EMPTY_SIGN}</span>
      {/if}
    </div>
  </td>
{/if}

{#snippet actions(actions: Action[])}
  {#if actions.length === 1}
    <!-- single action -->
    {@render button(actions[0])}
  {:else if actions.length > 1}
    <!-- multiple actions -->
    {@const [primary, ...rest] = actions}
    {@render button(primary, 'max-lg:hidden')}
    {#if rest.length === 1}
      {@render button(rest[0], 'max-lg:hidden')}
    {:else if rest.length > 1}
      <ActionMenu actions={rest} class="max-lg:hidden" />
    {/if}
    <ActionMenu {actions} class="lg:hidden" />
  {/if}
{/snippet}

{#snippet button(action: Action, _class?: string)}
  <Button {...action} class="{action.class} {_class}" />
{/snippet}
