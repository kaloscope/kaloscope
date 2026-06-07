<script lang="ts" module>
  import type { ChangeEventHandler } from 'svelte/elements';

  export type CheckboxProps = {
    /** The key of the sub-checkbox. */
    key?: string;
    /** The size of the sub-checkboxes. */
    batch?: number;
    /** Whether the checkbox is checked. */
    checked?: boolean;
    /** Whether the checkbox is disabled. */
    disabled?: boolean;
    /** The checkbox change event handler. */
    onchange?: ChangeEventHandler<HTMLInputElement>;
  };

  /** The class name for the sub-checkboxes. */
  const subClass = 'sub-checkbox';

  /** The selected keys. */
  export const selectedKeys: string[] = $state([]);
</script>

<script lang="ts">
  import { onDestroy } from 'svelte';

  let { key, batch, checked = $bindable(false), disabled = false, onchange }: CheckboxProps = $props();
  let indeterminate = $state(false);

  /**
   * Get the selected keys.
   *
   * @returns The selected keys.
   */
  export function getSelectedKeys(): string[] {
    return selectedKeys;
  }

  /**
   * Select all sub-checkboxes.
   */
  export function selectAll() {
    selectedKeys.splice(0, selectedKeys.length);
    document.querySelectorAll(`.${subClass}`).forEach((element) => {
      const checkbox = element as HTMLInputElement;
      if (!checkbox.disabled) {
        checkbox.checked = true;
        selectedKeys.push(checkbox.name);
      }
    });
  }

  /**
   * Unselect all sub-checkboxes.
   */
  export function unselectAll() {
    selectedKeys.splice(0, selectedKeys.length);
    document.querySelectorAll(`.${subClass}`).forEach((element) => {
      const checkbox = element as HTMLInputElement;
      checkbox.checked = false;
    });
  }

  // handle the indeterminate state for the header checkbox
  $effect(() => {
    if (batch !== undefined) {
      if (selectedKeys.length === 0) {
        indeterminate = false;
        checked = false;
      } else if (selectedKeys.length === batch) {
        indeterminate = false;
        checked = true;
      } else {
        indeterminate = true;
        checked = false;
      }
    }
  });

  // unselect all sub-checkboxes when the header checkbox is destroyed
  onDestroy(() => {
    batch && unselectAll();
  });
</script>

<input
  type="checkbox"
  class="checkbox checkbox-sm {key ? subClass : ''}"
  name={key}
  bind:checked
  {disabled}
  {indeterminate}
  onchange={(event) => {
    if (key) {
      // handle the sub-checkbox change event
      if (checked) {
        selectedKeys.push(key);
      } else {
        selectedKeys.splice(selectedKeys.indexOf(key), 1);
      }
    }
    if (batch) {
      // handle the header checkbox change event
      checked ? selectAll() : unselectAll();
    }
    onchange?.(event);
  }}
/>
