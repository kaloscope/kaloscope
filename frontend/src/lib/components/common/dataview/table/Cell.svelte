<script lang="ts" module>
  import type { ButtonProps } from '$lib/components';
  import type { Snippet } from 'svelte';
  import type { MouseEventHandler } from 'svelte/elements';

  type Action = {
    /** Hidden the action button if the condition is false. */
    condition?: boolean;
  } & ButtonProps;

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
  import { Button, Dropdown } from '$lib/components';
  import { EMPTY_SIGN } from '$lib/constants';
  import { icons } from '$lib/icons';

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
      {@render dropdown(rest, 'max-lg:hidden')}
    {/if}
    {@render dropdown(actions, 'lg:hidden')}
  {/if}
{/snippet}

{#snippet button(action: Action, _class?: string)}
  <Button {...action} class="{action.class} {_class}" />
{/snippet}

{#snippet dropdown(actions: Action[], _class?: string)}
  <Dropdown contentWidth="10rem" class="dropdown-end {_class}">
    {#snippet trigger()}
      <div class="btn btn-square btn-subtle btn-sm">
        <iconify-icon icon={icons.moreVertical} width="1rem"></iconify-icon>
      </div>
    {/snippet}
    <ul class="menu gap-1">
      {#each actions as action, i (i)}
        <li class={action.disabled ? 'menu-disabled' : ''}>
          <button
            class="px-2 {action.class}"
            onclick={(event) => {
              !action.disabled && action.onclick?.(event);
            }}
          >
            <iconify-icon icon={action.icon} width="1rem" class="size-4"></iconify-icon>
            {action.text}
          </button>
        </li>
      {/each}
    </ul>
  </Dropdown>
{/snippet}
