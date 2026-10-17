<script lang="ts">
  import { Label } from '$lib/components';
  import { _ } from '$lib/i18n';

  let {
    value = $bindable(''),
    available,
    loading,
    fetchDisabled,
    onfetch
  }: {
    value: string;
    available: string[] | null;
    loading: boolean | null;
    fetchDisabled: boolean;
    onfetch: () => void;
  } = $props();

  const uid = $props.id();
  let search = $state('');
  let input: HTMLTextAreaElement;
  let selected = $derived(
    value
      .split(/\r?\n/)
      .map((id) => id.trim())
      .filter(Boolean)
  );
  let filtered = $derived(available?.filter((id) => id.toLowerCase().includes(search.trim().toLowerCase())) ?? []);

  $effect(() => {
    if (available === null) search = '';
  });

  /**
   * Update one model selection while retaining manually entered IDs.
   *
   * @param id - Model identifier to select or remove.
   * @param checked - Whether the model should be selected.
   */
  function select(id: string, checked: boolean) {
    value = (checked ? [...new Set([...selected, id])] : selected.filter((model) => model !== id)).join('\n');
    input.setCustomValidity('');
  }
</script>

<div class="space-y-2">
  <div class="flex flex-wrap items-center justify-between gap-2">
    <Label preventDefault={false}>
      <label for="{uid}-selected">{$_('ai.models')}</label>
    </Label>
    <button type="button" class="btn mt-2 btn-sm" disabled={fetchDisabled || loading !== null} onclick={onfetch}>
      {$_(available === null ? 'ai.models_fetch' : 'action.reload')}
      {#if loading}<span class="loading loading-xs loading-dots"></span>{/if}
    </button>
  </div>
  {#if available !== null}
    <div class="space-y-2" aria-busy={loading !== null}>
      {#if available.length > 0}
        <input
          type="search"
          class="input w-full input-sm"
          bind:value={search}
          placeholder={$_('action.search', $_('ai.models'))}
          aria-label={$_('action.search', $_('ai.models'))}
          onkeydown={(event) => {
            if (event.key === 'Enter') event.preventDefault();
          }}
        />
        <div class="max-h-48 overflow-y-auto rounded-field border" role="group" aria-label={$_('ai.models')}>
          {#each filtered as id (id)}
            <label class="flex cursor-pointer items-center gap-3 px-3 py-2 hover:bg-base-200">
              <input
                type="checkbox"
                class="checkbox shrink-0 checkbox-sm"
                checked={selected.includes(id)}
                disabled={!selected.includes(id) && selected.length >= 100}
                onchange={(event) => select(id, event.currentTarget.checked)}
              />
              <span class="min-w-0 font-mono text-sm break-all">{id}</span>
            </label>
          {:else}
            <p class="p-3 text-sm text-base-content/70">{$_('ai.models_no_match')}</p>
          {/each}
        </div>
      {:else}
        <p class="text-sm text-base-content/70" role="status">{$_('ai.models_empty')}</p>
      {/if}
    </div>
  {/if}
  <div class="space-y-1.5">
    <textarea
      bind:this={input}
      id="{uid}-selected"
      name="model_ids"
      class="textarea w-full font-mono"
      rows="3"
      bind:value
      spellcheck="false"
      aria-describedby="{uid}-hint {uid}-count"></textarea>
    <div class="flex flex-wrap justify-between gap-x-4 gap-y-1 px-1 text-xs leading-relaxed opacity-60">
      <p id="{uid}-hint">{$_('ai.models_hint')}</p>
      <p id="{uid}-count" class="ml-auto shrink-0 text-right">{$_('ai.models_selected', selected.length)}</p>
    </div>
  </div>
</div>
