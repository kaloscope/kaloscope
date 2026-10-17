<script lang="ts">
  import { api } from '$lib/api';
  import { Button, Container, ProviderEditor, ProviderIcon, Setting, alert, confirm } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { AIProvider, AIProviderPreset, AIProviderUpsert, Resp } from '$lib/types';
  import { isHTTPError } from 'ky';
  import { onMount } from 'svelte';

  let providers: AIProvider[] = $state([]);
  let presets: AIProviderPreset[] = $state([]);
  let editor: ProviderEditor;
  let loadError = $state(false);
  let pendingId: number | null = $state(null);
  const loading = createLoading();

  /**
   * Load saved providers and their available presets together.
   *
   * @returns A promise that resolves after loading or showing a retry state.
   */
  async function load(): Promise<void> {
    loading.start();
    loadError = false;
    try {
      const [saved, defaults] = await Promise.all([
        api.get('ai/provider/list').json<Resp<AIProvider[]>>(),
        api.get('ai/provider/presets').json<Resp<AIProviderPreset[]>>()
      ]);
      providers = saved.data;
      presets = defaults.data;
    } catch (cause) {
      loadError = true;
      if (!isHTTPError(cause)) alert({ level: 'error', message: $_('ai.load_failed') });
    } finally {
      loading.end();
    }
  }

  /**
   * Replace a saved provider in the list or append a new one.
   *
   * @param provider - Persisted provider returned by the API.
   */
  function saved(provider: AIProvider) {
    const index = providers.findIndex((item) => item.id === provider.id);
    if (index < 0) providers.push(provider);
    else providers[index] = provider;
  }

  /**
   * Persist an enabled state while retaining the stored credential.
   *
   * @param provider - Connection being toggled.
   * @param input - Checkbox whose state is restored if saving fails.
   * @returns A promise that resolves after the update attempt.
   */
  async function toggle(provider: AIProvider, input: HTMLInputElement): Promise<void> {
    const payload: AIProviderUpsert = {
      id: provider.id,
      name: provider.name,
      preset: provider.preset,
      protocol: 'chat_completions',
      base_url: provider.base_url,
      model_ids: provider.model_ids,
      enabled: input.checked
    };
    pendingId = provider.id;
    try {
      const { data } = await api.post('ai/provider/upsert', { json: payload }).json<Resp<AIProvider>>();
      saved(data);
    } catch (cause) {
      input.checked = provider.enabled;
      if (!isHTTPError(cause)) alert({ level: 'error', message: $_('ai.update_failed') });
    } finally {
      pendingId = null;
    }
  }

  /**
   * Delete a confirmed provider without removing its row on failure.
   *
   * @param id - Provider identifier to delete.
   * @returns A promise that resolves after the deletion attempt.
   */
  async function remove(id: number): Promise<void> {
    pendingId = id;
    try {
      await api.post(`ai/provider/${id}/delete`);
      providers = providers.filter((provider) => provider.id !== id);
    } catch (cause) {
      if (!isHTTPError(cause)) alert({ level: 'error', message: $_('ai.delete_failed') });
    } finally {
      pendingId = null;
    }
  }

  onMount(() => {
    load();
  });
</script>

<Container type="settings" minWidth="0" maxWidth="56rem" rowGap="2rem" loading={$loading}>
  <Setting title={$_('nav.settings.system.ai')}>
    {#if loadError}
      <div class="flex flex-wrap items-center justify-between gap-3">
        <p class="text-sm opacity-50" role="status">{$_('ai.load_failed')}</p>
        <button class="btn btn-sm" onclick={load}>{$_('action.retry')}</button>
      </div>
    {:else}
      <div class="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-4">
        {#each presets as preset (preset.id)}
          <button
            class="btn h-12 justify-start gap-3 bg-base-100 font-normal"
            onclick={() => editor.showModal(null, preset)}
          >
            <ProviderIcon preset={preset.id} class="size-6 shrink-0" />
            <span>{preset.name}</span>
          </button>
        {/each}
        <button class="btn h-12 justify-start gap-3 font-normal" onclick={() => editor.showModal()}>
          <iconify-icon icon={icons.addCircle} width="1.5rem" aria-hidden="true"></iconify-icon>
          <span>{$_('ai.custom')}</span>
        </button>
      </div>
    {/if}
  </Setting>

  {#if !loadError}
    <Setting title={$_('ai.configured')}>
      {#if providers.length === 0}
        <p class="py-8 text-center text-sm opacity-50">{$_('ai.empty')}</p>
      {:else}
        <ul class="divide-y divide-border rounded-field border bg-base-100">
          {#each providers as provider (provider.id)}
            <li class="flex min-w-0 items-center gap-3 p-4">
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-3">
                  <ProviderIcon preset={provider.preset} class="size-7 shrink-0" />
                  <div class="min-w-0 flex-1">
                    <h3 class="truncate font-medium" title={provider.name}>{provider.name}</h3>
                    <p class="mt-1 truncate text-xs text-base-content/70" title={provider.base_url}>
                      {provider.base_url}
                    </p>
                  </div>
                </div>
                <div class="mt-3 flex flex-wrap items-center gap-2 text-xs text-base-content/70">
                  <span class="inline-flex items-center gap-1.5 rounded-field bg-base-200 px-2 py-1">
                    <span
                      class="size-1.5 shrink-0 rounded-full {provider.enabled ? 'bg-success' : 'bg-base-content/40'}"
                      aria-hidden="true"
                    ></span>
                    {$_(provider.enabled ? 'ai.enabled' : 'ai.disabled')}
                  </span>
                  <span class="inline-flex items-center gap-1.5 rounded-field bg-base-200 px-2 py-1">
                    <iconify-icon icon={icons.key} width="0.875rem" aria-hidden="true"></iconify-icon>
                    {$_(provider.has_api_key ? 'ai.key_saved' : 'ai.key_missing')}
                  </span>
                  <span class="inline-flex items-center gap-1.5 rounded-field bg-base-200 px-2 py-1">
                    <iconify-icon icon={icons.layerDiagonalSparkle} width="0.875rem" aria-hidden="true"></iconify-icon>
                    {$_('ai.model_count', provider.model_ids.length)}
                  </span>
                </div>
                {#if provider.model_ids.length}
                  <div class="mt-3 flex flex-wrap gap-2">
                    {#each provider.model_ids.slice(0, 6) as model (model)}
                      <span class="max-w-full truncate rounded bg-base-200 px-2 py-1 text-xs" title={model}
                        >{model}</span
                      >
                    {/each}
                    {#if provider.model_ids.length > 6}<span class="px-2 py-1 text-xs text-base-content/70"
                        >+{provider.model_ids.length - 6}</span
                      >{/if}
                  </div>
                {/if}
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <input
                  type="checkbox"
                  class="toggle shrink-0 toggle-sm"
                  aria-label={$_('ai.toggle', provider.name)}
                  checked={provider.enabled}
                  disabled={pendingId !== null}
                  onchange={(event) => toggle(provider, event.currentTarget)}
                />
                <Button
                  icon={icons.edit}
                  text={$_('action.edit', $_('entity.provider'))}
                  disabled={pendingId !== null}
                  onclick={() =>
                    editor.showModal(provider, presets.find((preset) => preset.id === provider.preset) ?? null)}
                />
                <Button
                  icon={icons.delete}
                  text={$_('action.delete', $_('entity.provider'))}
                  disabled={pendingId !== null}
                  onclick={() =>
                    confirm({
                      icon: icons.delete,
                      title: `${$_('action.delete', $_('entity.provider'))} [${provider.name}]`,
                      onconfirm: () => {
                        remove(provider.id);
                      }
                    })}
                />
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </Setting>
  {/if}
</Container>

<ProviderEditor bind:this={editor} onsave={saved} />
