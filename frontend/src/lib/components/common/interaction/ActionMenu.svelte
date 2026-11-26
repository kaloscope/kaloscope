<script lang="ts" module>
  import type { ButtonProps } from '$lib/components';

  /** An action shown as a button or as an entry of an action menu. */
  export type Action = {
    /** Hide the action when the condition is false. */
    condition?: boolean;
  } & ButtonProps;

  export type ActionMenuProps = {
    /** The actions listed in the menu, leaving out those whose condition is false. */
    actions: Action[];
    /** The class names for the dropdown. */
    class?: string;
  };
</script>

<script lang="ts">
  import { Dropdown } from '$lib/components';
  import { icons } from '$lib/icons';

  let { actions, class: _class }: ActionMenuProps = $props();

  // the actions whose condition allows them
  let shown = $derived(actions.filter((action) => action.condition !== false));
</script>

<!-- a "more" button that opens the actions as a menu -->
<Dropdown contentWidth="10rem" class="dropdown-end {_class}">
  {#snippet trigger()}
    <div class="btn btn-square btn-subtle btn-sm">
      <iconify-icon icon={icons.moreVertical} width="1rem"></iconify-icon>
    </div>
  {/snippet}
  <ul class="menu gap-1">
    {#each shown as action, i (i)}
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
