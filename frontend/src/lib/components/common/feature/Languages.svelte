<script lang="ts" module>
  import type { DropdownProps } from '$lib/components';

  export type LanguagesProps = Pick<DropdownProps, 'class' | 'triggerClass' | 'contentClass'>;
</script>

<script lang="ts">
  import { Dropdown, Modal } from '$lib/components';
  import { _, locale, locales } from '$lib/i18n';
  import { icons } from '$lib/icons';

  let { class: _class, triggerClass, contentClass }: LanguagesProps = $props();

  // the modal dialog for the language switcher
  let modal: Modal;
  export const showModal = () => modal.show();

  /**
   * Switch the locale.
   *
   * @param code - Language code.
   */
  function switchLocale(code: string) {
    locale.set(code);
    // will be used when initializing the i18n library in the main layout
    localStorage.setItem('locale', code);
  }
</script>

{#snippet languages()}
  {#each $locales.filter((l) => l !== 'languages') as code (code)}
    <li>
      <button
        class={$locale?.startsWith(code) ? 'item-emphasis' : ''}
        onclick={() => {
          switchLocale(code);
        }}
      >
        <!-- the language menu keeps its bordered code badge -->
        <span
          class="badge h-fit rounded-field border-base-content/10 px-2 py-px text-xs font-medium uppercase opacity-70 shadow-inner"
        >
          {code.split('-')[0]}
        </span>
        {$_(code, { locale: 'languages' })}
      </button>
    </li>
  {/each}
{/snippet}

<Dropdown
  triggerIcon={icons.language}
  contentWidth="10rem"
  contentMaxHeight="50vh"
  class={_class}
  {triggerClass}
  {contentClass}
>
  <ul class="menu gap-1">
    {@render languages()}
  </ul>
</Dropdown>

<Modal icon={icons.language} title={$_('app.switch_language')} bind:this={modal}>
  <ul class="menu max-h-[50vh] w-full flex-nowrap gap-1 overflow-y-auto rounded-card border">
    {@render languages()}
  </ul>
</Modal>
