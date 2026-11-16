<script lang="ts" module>
  import type { DropdownProps } from '$lib/components';

  export type LanguagesProps = Pick<DropdownProps, 'class' | 'triggerClass' | 'contentClass'>;
</script>

<script lang="ts">
  import { Badge, Dropdown, Modal } from '$lib/components';
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
        <Badge uppercase>{code.split('-')[0]}</Badge>
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
