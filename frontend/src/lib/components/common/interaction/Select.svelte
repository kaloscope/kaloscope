<script lang="ts" module>
  import type { Option, OptionValue } from '$lib/types';
  import type { Snippet } from 'svelte';
  import type { EventHandler } from 'svelte/elements';

  export type SelectProps = Partial<{
    /** Whether to use the native select element. */
    native: boolean;
    /** The options snippet to render. */
    children: Snippet;
    /** List of options. */
    options: Option[];
    /** The selected value. */
    value: OptionValue;
    /** The label of the item to select. */
    label: string;
    /** The placeholder text for the select. */
    placeholder: string;
    /** The name of the select. */
    name: string;
    /** Whether the select is required. */
    required: boolean;
    /** Whether the select is disabled. */
    disabled: boolean;
    /** Whether to use the filter style. */
    filter: boolean;
    /** The class names for the select. */
    class: string;
    /** The select change event handler. */
    onchange: EventHandler;
  }>;
</script>

<script lang="ts">
  import { Dropdown } from '$lib/components';
  import { _ } from '$lib/i18n';

  let {
    native = true,
    children,
    options: _options,
    value = $bindable(),
    label,
    placeholder: _placeholder,
    name,
    required = false,
    disabled = false,
    filter = false,
    class: _class,
    onchange
  }: SelectProps = $props();

  // the placeholder text
  let placeholder = $derived(_placeholder || (label ? $_('action.select', label.toLocaleLowerCase()) : ''));

  // the options to render
  let options = $derived.by(() => {
    if (placeholder) {
      return [{ value: null, label: placeholder, disabled: true }, ...(_options || [])];
    } else {
      return _options;
    }
  });

  // null value is used to represent the placeholder
  // empty string is used to represent the all options
  $effect(() => {
    if (placeholder && value === '') {
      value = null;
    }
  });

  // the dynamic class names
  let filterClass = $derived(filter ? 'max-w-[min(40%,10rem)] select-sm shadow-sm' : '');
  let selectClass = $derived(`select appearance-none ${filterClass} ${value ? '' : 'text-base-content/50'}`);
</script>

{#if native}
  <!-- native select element -->
  <select class="{selectClass} {_class}" bind:value {name} {required} {disabled} {onchange}>
    {#if children}
      {@render children()}
    {:else if options}
      {#each options as option (option.value)}
        <option value={option.value} disabled={option.disabled}>
          {$_(option.label, { default: option.label })}
        </option>
      {/each}
    {/if}
  </select>
{:else if options}
  <!-- custom dropdown select -->
  {@const label = options.filter((o) => o.value === value)[0]?.label}
  <Dropdown class="dropdown-end [&:open_.select]:border-primary/80! {_class}" contentClass="bg-base-200 my-1">
    {#snippet trigger()}
      <div class={selectClass}>{$_(label, { default: label })}</div>
    {/snippet}
    <ul class="menu">
      {#each options as option (option.value)}
        <li class={option.disabled ? 'menu-disabled' : ''}>
          <button
            class={option.value === value ? '[&_svg]:block' : ''}
            onclick={(event) => {
              value = option.value;
              onchange?.(event);
            }}
          >
            <span class="size-3">
              <svg
                xmlns="http://www.w3.org/2000/svg"
                viewBox="0 0 24 24"
                fill="currentColor"
                class="pointer-events-none hidden size-3 shrink-0"
              >
                <path d="M20.285 2l-11.285 11.567-5.286-5.011-3.714 3.716 9 8.728 15-15.285z"></path>
              </svg>
            </span>
            {$_(option.label, { default: option.label })}
          </button>
        </li>
      {/each}
    </ul>
  </Dropdown>
{/if}
