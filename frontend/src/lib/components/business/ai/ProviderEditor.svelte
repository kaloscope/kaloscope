<script lang="ts">
  import { enhance } from '$app/forms';
  import { api } from '$lib/api';
  import { alert, Label, Modal } from '$lib/components';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type {
    AIProvider,
    AIProviderConnection,
    AIProviderPreset,
    AIProviderUpsert,
    BaseResp,
    Resp
  } from '$lib/types';
  import { isHTTPError } from 'ky';
  import { onDestroy } from 'svelte';
  import ProviderIcon from './ProviderIcon.svelte';
  import ModelPicker from './ModelPicker.svelte';

  let { onsave }: { onsave: (provider: AIProvider) => void } = $props();

  const uid = $props.id();
  const loading = createLoading();
  const testLoading = createLoading();
  const modelsLoading = createLoading();
  const schema = createFormSchema(({ text, url, password }) => ({
    name: text().maxlength(64),
    base_url: url().maxlength(2048),
    api_key: password().maxlength(4096).required(false).autocomplete('new-password')
  }));

  let modal: Modal;
  let form: HTMLFormElement;
  let connectionRequests: Partial<Record<'discover' | 'test', AbortController>> = {};
  let original: AIProvider | null = $state(null);
  let preset: AIProviderPreset | null = $state(null);
  let name = $state('');
  let baseUrl = $state('');
  let apiKey = $state('');
  let modelText = $state('');
  let enabled = $state(true);
  let availableModels: string[] | null = $state(null);
  let showApiKey = $derived.by(() => preset?.api_key_required !== false);
  let endpointChanged = $derived.by(() => {
    if (!original?.has_api_key) return false;
    try {
      return new URL(baseUrl.trim()).href.replace(/\/+$/, '') !== new URL(original.base_url).href.replace(/\/+$/, '');
    } catch {
      return false;
    }
  });
  let hasRequiredApiKey = $derived.by(
    () => !preset?.api_key_required || !!apiKey.trim() || (!!original?.has_api_key && !endpointChanged)
  );
  let keyHint = $derived.by(() =>
    endpointChanged
      ? preset?.api_key_required
        ? 'ai.endpoint_key_required_hint'
        : 'ai.endpoint_key_hint'
      : !original?.has_api_key && !preset?.api_key_required
        ? 'ai.key_optional_hint'
        : null
  );

  onDestroy(resetDiscovery);

  /**
   * Open a fresh form for a saved connection or an editable preset.
   *
   * @param provider - Saved connection to edit, or `null` to create one.
   * @param defaults - Preset defaults, or `null` for a custom connection.
   */
  export function showModal(provider: AIProvider | null = null, defaults: AIProviderPreset | null = null) {
    if ($loading !== null) return;
    original = provider;
    preset = defaults;
    name = provider?.name ?? defaults?.name ?? '';
    baseUrl = provider?.base_url ?? defaults?.base_url ?? '';
    modelText = provider?.model_ids.join('\n') ?? '';
    enabled = provider?.enabled ?? true;
    clearSecret();
    modal.show();
  }

  /** Clear the transient credential and cancel connection requests when the dialog closes. */
  function clearSecret() {
    apiKey = '';
    resetDiscovery();
  }

  /** Discard a stale catalog and cancel connection requests while retaining selected models. */
  function resetDiscovery() {
    for (const controller of Object.values(connectionRequests)) controller.abort();
    connectionRequests = {};
    modelsLoading.end();
    testLoading.end();
    availableModels = null;
  }

  /**
   * Clear browser validation when a field is edited.
   *
   * @param event - Input or change event from the form.
   */
  function clearValidation(event: Event) {
    const target = event.target;
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
      target.setCustomValidity('');
      if (['base_url', 'api_key'].includes(target.name)) resetDiscovery();
    }
  }

  /**
   * Validate connection fields independently of the name and selected models.
   *
   * Use an empty key for unauthenticated presets or a changed endpoint without a replacement.
   *
   * @returns Draft connection settings, or `null` when validation fails.
   */
  function connection(): AIProviderConnection | null {
    const urlInput = form.elements.namedItem('base_url') as HTMLInputElement;
    const keyInput = form.elements.namedItem('api_key') as HTMLInputElement | null;
    const endpoint = baseUrl.trim().replace(/\/+$/, '');
    const key = showApiKey ? apiKey.trim() : '';
    let validUrl = false;
    try {
      const url = new URL(endpoint);
      validUrl =
        ['http:', 'https:'].includes(url.protocol) &&
        !!url.hostname &&
        !url.username &&
        !url.password &&
        !/[?#\s]/.test(endpoint) &&
        ![...endpoint].some((char) => char.charCodeAt(0) < 32);
    } catch {
      // let the field explain the invalid URL
    }
    urlInput.setCustomValidity(validUrl ? '' : $_('ai.invalid_url'));
    keyInput?.setCustomValidity(/[^\x21-\x7e]/.test(key) ? $_('ai.invalid_key') : '');
    if (!urlInput.reportValidity() || (keyInput && !keyInput.reportValidity())) return null;
    return {
      ...(original ? { id: original.id } : {}),
      preset: original?.preset ?? preset?.id ?? null,
      base_url: endpoint,
      ...(key || endpointChanged || !showApiKey ? { api_key: key } : {})
    };
  }

  /**
   * Fetch model candidates or test connectivity independently of other actions.
   *
   * @param action - Whether to populate model candidates or report the connection test result.
   * @returns A promise that resolves when the request finishes or is cancelled.
   */
  async function fetchModels(action: 'discover' | 'test'): Promise<void> {
    if (!hasRequiredApiKey || (action === 'test' ? $testLoading : $modelsLoading) !== null) return;
    const payload = connection();
    if (!payload) return;
    const controller = new AbortController();
    const requestLoading = action === 'test' ? testLoading : modelsLoading;
    connectionRequests[action] = controller;
    requestLoading.start();
    try {
      const { data } = await api
        .post('ai/provider/models', {
          json: payload,
          signal: controller.signal,
          retry: 0,
          context: { silentErrors: true }
        })
        .json<Resp<string[]>>();
      if (connectionRequests[action] !== controller || !modal.isOpen()) return;
      if (action === 'test') {
        alert({ level: 'success', message: $_('ai.test_success') });
      } else {
        availableModels = data;
      }
    } catch (cause) {
      if (connectionRequests[action] !== controller || !modal.isOpen()) return;
      const response = isHTTPError(cause) ? (cause.data as BaseResp | undefined) : undefined;
      if (response?.status === 401) return;
      alert({
        level: 'error',
        message: response?.message
          ? $_(`alert.${response.message}`, { default: $_('alert.ai_models_fetch_failed') })
          : $_('alert.ai_models_fetch_failed')
      });
    } finally {
      if (connectionRequests[action] === controller) {
        delete connectionRequests[action];
        requestLoading.end();
      }
    }
  }

  /**
   * Validate and save a connection independently of discovery and testing.
   *
   * @param form - Submitted form containing the editable connection fields.
   * @returns A promise that resolves after the save attempt finishes.
   */
  async function save(form: HTMLFormElement): Promise<void> {
    if (!hasRequiredApiKey || $loading !== null) return;
    const nameInput = form.elements.namedItem('name') as HTMLInputElement;
    const modelsInput = form.elements.namedItem('model_ids') as HTMLTextAreaElement;
    name = name.trim();
    const draft = connection();
    if (!draft) return;
    const models = modelText
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);

    nameInput.setCustomValidity(name ? '' : $_('ai.name_required'));
    modelsInput.setCustomValidity(
      models.length > 100 ||
        models.some(
          (value) =>
            value.length > 255 || [...value].some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
        )
        ? $_('ai.invalid_models')
        : new Set(models).size !== models.length
          ? $_('ai.duplicate_models')
          : ''
    );
    if (!form.reportValidity()) return;

    const payload: AIProviderUpsert = {
      ...draft,
      name,
      protocol: 'chat_completions',
      model_ids: models,
      enabled
    };
    loading.start();
    try {
      const { data } = await api.post('ai/provider/upsert', { json: payload }).json<Resp<AIProvider>>();
      apiKey = '';
      onsave(data);
      modal.close();
    } catch (cause) {
      if (!isHTTPError(cause)) alert({ level: 'error', message: $_('ai.save_failed') });
    } finally {
      loading.end();
    }
  }
</script>

<Modal
  bind:this={modal}
  icon={icons.layerDiagonalSparkle}
  title={$_(original ? 'action.edit' : 'action.add', $_('entity.provider'))}
  maxWidth="38rem"
  onclose={clearSecret}
>
  <form
    method="post"
    bind:this={form}
    use:enhance={({ formElement, cancel }) => {
      cancel();
      save(formElement);
    }}
    oninput={clearValidation}
    onchange={clearValidation}
  >
    <fieldset class="fieldset">
      <Label required preventDefault={false}>
        <label for="{uid}-name">{$_('field.name')}</label>
      </Label>
      <div class="flex items-center gap-3">
        <div class="flex max-w-1/2 shrink-0 items-center gap-2">
          <ProviderIcon preset={original?.preset ?? preset?.id ?? null} class="size-6 shrink-0" />
          <span class="truncate text-sm" title={preset?.name ?? $_('ai.custom')}>{preset?.name ?? $_('ai.custom')}</span
          >
        </div>
        <input
          id="{uid}-name"
          class="input min-w-0 flex-1"
          {...schema.name}
          bind:value={name}
          autocomplete="off"
          placeholder={$_('field.name')}
        />
      </div>
      <Label required preventDefault={false}>
        <label for="{uid}-url">{$_('ai.base_url')}</label>
      </Label>
      <input
        id="{uid}-url"
        class="input w-full"
        {...schema.base_url}
        bind:value={baseUrl}
        autocomplete="off"
        spellcheck="false"
        placeholder="https://api.example.com/v1"
        aria-describedby="{uid}-url-hint"
      />
      <p id="{uid}-url-hint" class="px-1 text-xs leading-relaxed opacity-60">{$_('ai.base_url_hint')}</p>
      {#if showApiKey}
        <Label preventDefault={false}>
          <label for="{uid}-key">{$_('ai.api_key')}</label>
        </Label>
        <input
          id="{uid}-key"
          class="input w-full"
          {...schema.api_key}
          bind:value={apiKey}
          spellcheck="false"
          placeholder={original?.has_api_key && !endpointChanged ? $_('ai.key_keep') : undefined}
          aria-describedby={keyHint ? `${uid}-key-hint` : undefined}
        />
        {#if keyHint}
          <p id="{uid}-key-hint" class="px-1 text-xs leading-relaxed opacity-60">{$_(keyHint)}</p>
        {/if}
      {/if}
      <ModelPicker
        bind:value={modelText}
        available={availableModels}
        loading={$modelsLoading}
        fetchDisabled={!hasRequiredApiKey}
        onfetch={() => fetchModels('discover')}
      />
      <div class="flex items-center gap-3">
        <Label class="grow" preventDefault={false}>
          <label for="{uid}-enabled">{$_('ai.enabled')}</label>
        </Label>
        <input id="{uid}-enabled" type="checkbox" class="toggle mt-2 toggle-sm" bind:checked={enabled} />
      </div>
    </fieldset>
    <div class="modal-action flex-wrap">
      <button
        type="button"
        class="btn mr-auto btn-soft"
        disabled={!hasRequiredApiKey || $testLoading !== null}
        aria-busy={$testLoading !== null}
        onclick={() => fetchModels('test')}
      >
        {$_('ai.test')}
        {#if $testLoading}<span class="loading loading-xs loading-dots"></span>{/if}
      </button>
      <div class="flex gap-2">
        <button type="button" class="btn min-w-20" onclick={() => modal.close()}>{$_('message.cancel')}</button>
        <button type="submit" class="btn min-w-20 btn-submit" disabled={!hasRequiredApiKey || $loading !== null}>
          {$_('message.confirm')}
          {#if $loading}<span class="loading loading-xs loading-dots"></span>{/if}
        </button>
      </div>
    </div>
  </form>
</Modal>
