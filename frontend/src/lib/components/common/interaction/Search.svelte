<script lang="ts" module>
  import type { Filter } from '$lib/types';

  export type SearchProps = {
    /** The search input value. */
    value: string;
    /** The label of the item to search. */
    label?: string;
    /** The placeholder text for the search input. */
    placeholder?: string | null;
    /** Whether the search input is required. */
    required?: boolean | null;
    /** Whether to trigger the search manually. */
    manual?: boolean;
    /** Whether to use a smaller input size. */
    small?: boolean;
    /** The maximum width of the container. */
    maxWidth?: string;
    /** The class names for the search input. */
    class?: string;
    /** The search event handler. */
    onsearch: (value: string) => void;

    /** The search filters schema. */
    schema?: Record<string, Filter> | null;
    /** The search filters JSON value. */
    filters?: string | null;
    /** The filter event handler. */
    onfilter?: (value: string) => void;
  };
</script>

<script lang="ts">
  import { Filters } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { debounce } from '$lib/utils';

  let {
    value = $bindable(),
    label,
    placeholder: _placeholder,
    required = false,
    manual = false,
    small = true,
    maxWidth = '20rem',
    class: _class,
    onsearch,
    schema,
    filters,
    onfilter
  }: SearchProps = $props();

  // the placeholder text
  let placeholder = $derived(_placeholder || (label ? $_('action.enter', label.toLocaleLowerCase()) : ''));

  // the input element
  let searchInput: HTMLInputElement;

  // the filters modal dialog
  let filtersModal: Filters | null = $state(null);
  let showFilters: boolean = $derived(!!schema && Object.entries(schema).some(([, filter]) => !!filter.type));

  // whether the input is in composition mode
  let compositing: boolean = false;

  // create a copy of the value to avoid direct binding issues
  // eslint-disable-next-line svelte/prefer-writable-derived
  let _value: string = $state(value);
  $effect(() => {
    _value = value;
  });

  // the debounced search event handler
  const _onsearch = debounce((newValue: string) => {
    value = newValue;
    onsearch(newValue);
  });

  // the button class names
  const btnClass = 'btn btn-square border-0 bg-transparent shadow-none btn-ghost btn-xs';
  const iconClass = 'text-base-content/50 transition-colors duration-200 hover:text-base-content';
</script>

<svelte:window
  onkeydown={(event) => {
    if (event.key === 'Enter' && document.activeElement === searchInput) {
      _onsearch(_value);
      if (!compositing) {
        searchInput.blur();
      }
    }
  }}
/>

{#snippet searchButton()}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <span
    tabindex="0"
    role="button"
    class={btnClass}
    aria-label="Search"
    onclick={(event) => {
      event.preventDefault();
      required && searchInput.reportValidity();
      _onsearch(_value);
    }}
  >
    <iconify-icon icon={icons.search} width="1.5rem" class={iconClass}></iconify-icon>
  </span>
{/snippet}

<!-- https://stackoverflow.com/questions/39884226/why-does-an-element-with-flex-1-width-0-still-have-width -->
<label class="input w-0 grow shadow-sm {small ? 'input-sm' : ''} {_class}" style:max-width={maxWidth}>
  {#if !manual}
    {@render searchButton()}
  {/if}

  <input
    type="search"
    class="grow truncate"
    maxlength="4096"
    {required}
    {placeholder}
    bind:value={_value}
    bind:this={searchInput}
    oninput={() => !manual && !compositing && _onsearch(_value)}
    oncompositionstart={() => (compositing = true)}
    oncompositionend={() => ((compositing = false), !manual && _onsearch(_value))}
  />

  {#if showFilters}
    {@const filtered = filters && filters !== '{}'}
    <button type="button" class={btnClass} aria-label="Show filters" onclick={() => filtersModal?.showModal()}>
      <iconify-icon
        icon={icons.adjustmentsHorizontal}
        width="1.5rem"
        class={filtered ? 'text-base-content' : iconClass}
      >
      </iconify-icon>
    </button>
  {/if}

  {#if manual}
    {@render searchButton()}
  {/if}
</label>

{#if schema && showFilters}
  <Filters bind:this={filtersModal} {schema} value={filters} onclose={(value) => onfilter?.(value)} />
{/if}
