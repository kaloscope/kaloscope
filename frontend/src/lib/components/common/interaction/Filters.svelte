<script lang="ts" module>
  import type { Filter } from '$lib/types';

  export type FiltersProps = {
    /** The search filters schema. */
    schema: Record<string, Filter>;
    /** The search filters JSON value. */
    value?: string | null;
    /** The callback function when the dialog is closed. */
    onclose?: (value: string) => void;
  };
</script>

<script lang="ts">
  import { Label, Modal } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';

  let { schema, value, onclose }: FiltersProps = $props();

  // svelte-ignore state_referenced_locally
  let values: Record<string, string | string[]> = $state(JSON.parse(value || '{}'));

  // calendar filter start date storage
  const starts: Record<string, string> = {};

  let modal: Modal;
  export const showModal = () => modal.show();

  /**
   * Check if the element is covered by the bottom of the screen.
   *
   * @param element - The element to check.
   * @returns True if the element is covered, false otherwise.
   */
  function isBottomOverflow(element: Element | null): boolean {
    if (!element) {
      return false;
    }
    return element.getBoundingClientRect().bottom > window.innerHeight;
  }

  /**
   * Get the range string from the stored array value.
   *
   * @param key - The filter key.
   * @returns The range string for Cally.
   */
  function rangeValue(key: string): string {
    const value = values[key];
    if (!Array.isArray(value) || !value[0] || !value[1]) {
      return '';
    }
    return `${value[0]}/${value[1]}`;
  }

  /**
   * Get the range label from the stored array value.
   *
   * @param key - The filter key.
   * @returns The range label.
   */
  function rangeLabel(key: string): string {
    const value = values[key];
    if (!Array.isArray(value) || !value[0] || !value[1]) {
      return '';
    }
    return `${value[0]} ~ ${value[1]}`;
  }

  /**
   * Format a Cally date event detail as an ISO date string.
   *
   * @param date - The Cally event date.
   * @returns The ISO date string.
   */
  function formatDate(date: Date): string {
    const year = date.getUTCFullYear();
    const month = String(date.getUTCMonth() + 1).padStart(2, '0');
    const day = String(date.getUTCDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }
</script>

<Modal
  icon={icons.adjustmentsHorizontal}
  title={$_('entity.filters')}
  bind:this={modal}
  onclose={() => onclose?.(JSON.stringify(values))}
>
  <fieldset class="mb-2 fieldset">
    {#each Object.entries(schema) as [key, filter] (key)}
      <Label>{filter.label ?? key}</Label>
      {#if filter.type === 'text'}
        {@render scalar(key, 'search')}
      {:else if filter.type === 'datetime'}
        {@render scalar(key, 'datetime-local')}
      {:else if filter.type === 'date' || filter.type === 'time' || filter.type === 'week' || filter.type === 'month'}
        {@render scalar(key, filter.type)}
      {:else if filter.type === 'calendar' || filter.type === 'calendar-range'}
        {@render calendar(key, filter.type === 'calendar-range')}
      {:else if filter.type === 'radio'}
        {@render radio(key, filter)}
      {:else if filter.type === 'checkbox'}
        {@render checkbox(key, filter)}
      {:else if filter.type === 'select'}
        {@render select(key, filter)}
      {/if}
    {/each}
  </fieldset>
</Modal>

<!-- scalar filter -->
{#snippet scalar(key: string, type: 'search' | 'datetime-local' | 'date' | 'time' | 'week' | 'month')}
  <input
    {type}
    class="input w-full truncate input-sm"
    value={values[key]}
    oninput={(event) => {
      values[key] = type === 'search' ? event.currentTarget.value.trim() : event.currentTarget.value;
      if (values[key] === '') {
        delete values[key];
      }
    }}
  />
{/snippet}

<!-- calendar filter -->
{#snippet calendar(key: string, range: boolean)}
  {@const callyId = `cally-${key}`}
  {@const callyPopoverId = `cally-popover-${key}`}
  <button
    id={callyId}
    popovertarget={callyPopoverId}
    type="button"
    class="input w-full input-sm"
    style="anchor-name:--{callyId}"
    onclick={() => {
      setTimeout(() => {
        const element = document.querySelector(`#${callyPopoverId}`);
        if (isBottomOverflow(element)) {
          element?.classList.add('dropdown-top');
        }
        element?.classList.remove('invisible');
      });
    }}
  >
    <iconify-icon icon={icons.calendar} width="1.25rem" class="opacity-70"></iconify-icon>
    {range ? rangeLabel(key) : values[key]}
    {#if values[key]}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <div
        tabindex="0"
        role="button"
        class="absolute right-2 flex-center cursor-pointer"
        onclick={(event) => {
          event.preventDefault();
          delete values[key];
          delete starts[key];
        }}
      >
        <iconify-icon
          icon={icons.dismissCircleFilled}
          width="1.25rem"
          class="text-base-content/50 transition-colors duration-200 hover:text-base-content"
        ></iconify-icon>
      </div>
    {/if}
  </button>
  <div
    popover
    id={callyPopoverId}
    class="dropdown invisible m-1 rounded-box border bg-base-100 shadow-xl transition-none"
    style="position-anchor:--{callyId}"
  >
    {#if range}
      <calendar-range
        class="cally [&_::part(day_selected):hover]:bg-base-content/80 [&_::part(day_today):hover]:bg-primary/80"
        value={rangeValue(key)}
        onrangestart={(event: CustomEvent<Date>) => {
          starts[key] = formatDate(event.detail);
        }}
        onrangeend={(event: CustomEvent<Date>) => {
          const start = starts[key];
          const end = formatDate(event.detail);
          if (start && end) {
            values[key] = start <= end ? [start, end] : [end, start];
            delete starts[key];
            document.getElementById(callyPopoverId)?.hidePopover();
          } else {
            delete values[key];
          }
        }}
      >
        {@render calendarMonth()}
      </calendar-range>
    {:else}
      <calendar-date
        class="cally [&_::part(day_selected):hover]:bg-base-content/80 [&_::part(day_today):hover]:bg-primary/80"
        value={values[key]}
        onfocusday={(event: CustomEvent<Date>) => {
          const date = formatDate(event.detail);
          if (values[key] !== date) {
            values[key] = date;
            document.getElementById(callyPopoverId)?.hidePopover();
          }
        }}
      >
        {@render calendarMonth()}
      </calendar-date>
    {/if}
  </div>
{/snippet}

{#snippet calendarMonth()}
  {/* @ts-expect-error property does not exist */ null}
  <svg
    aria-label="Previous"
    class="size-4 fill-current"
    slot="previous"
    xmlns="http://www.w3.org/2000/svg"
    viewBox="0 0 24 24"
  >
    <path d="M15.75 19.5 8.25 12l7.5-7.5"></path>
  </svg>
  {/* @ts-expect-error property does not exist */ null}
  <svg aria-label="Next" class="size-4 fill-current" slot="next" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
    <path d="m8.25 4.5 7.5 7.5-7.5 7.5"></path>
  </svg>
  <calendar-month></calendar-month>
{/snippet}

<!-- radio filter -->
{#snippet radio(key: string, filter: Filter)}
  {#if filter.options}
    <div class="flex flex-wrap gap-4">
      {#each Object.entries(filter.options) as [value, label] (value)}
        <label class="label">
          <input
            type="radio"
            class="radio radio-sm"
            name={key}
            checked={values[key] === value}
            onclick={() => {
              if (values[key] === value) {
                delete values[key];
              } else {
                values[key] = value;
              }
            }}
          />
          <span class="text-sm text-base-content/80">{label}</span>
        </label>
      {/each}
    </div>
  {/if}
{/snippet}

<!-- checkbox filter -->
{#snippet checkbox(key: string, filter: Filter)}
  {#if filter.options}
    <div class="flex flex-wrap gap-4">
      {#each Object.entries(filter.options) as [value, label] (value)}
        <label class="label">
          <input
            type="checkbox"
            class="checkbox checkbox-sm"
            name={key}
            checked={values[key]?.includes(value)}
            onclick={() => {
              const selected = (values[key] ?? []) as string[];
              if (selected.includes(value)) {
                values[key] = selected.filter((v) => v !== value);
                if (values[key].length === 0) {
                  delete values[key];
                }
              } else {
                values[key] = [...selected, value];
              }
            }}
          />
          <span class="text-sm text-base-content/80">{label}</span>
        </label>
      {/each}
    </div>
  {/if}
{/snippet}

<!-- select filter -->
{#snippet select(key: string, filter: Filter)}
  {#if filter.options}
    <select
      class="select w-full appearance-none select-sm"
      value={values[key] ?? ''}
      onchange={(event) => {
        values[key] = event.currentTarget.value;
        if (values[key] === '') {
          delete values[key];
        }
      }}
    >
      <option value="">{$_('enum.all')}</option>
      {#each Object.entries(filter.options) as [value, label] (value)}
        <option {value}>{label}</option>
      {/each}
    </select>
  {/if}
{/snippet}
