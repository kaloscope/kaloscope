<script lang="ts">
  import { api } from '$lib/api';
  import { Button, Label } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _, nodeFormatter } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { user } from '$lib/stores';
  import type { AIModelSelection, AIProvider, Field, FlowGraphContext, Resp } from '$lib/types';
  import { useSvelteFlow } from '@xyflow/svelte';
  import { getContext, hasContext, untrack } from 'svelte';

  let { data = null, ...field }: Field & { nodeId: string; data?: AIModelSelection | null } = $props();
  const { updateNodeData } = useSvelteFlow();
  const { label } = nodeFormatter;
  let providers = $state<AIProvider[] | null>(null);
  const loading = createLoading();
  let failed = $state(false);
  let controller: AbortController | undefined;
  let providerSelect: HTMLSelectElement;
  let modelSelect: HTMLSelectElement;
  const editable = $derived($user?.role === 'admin');
  const provider = $derived(providers?.find((item) => item.id === data?.provider_id));
  const issue = $derived.by(() => {
    if (!providers || !data?.provider_id) return null;
    if (!provider) return 'ai_provider_missing';
    if (!provider.enabled) return 'ai_provider_disabled';
    if (data.model_id && !provider.model_ids.includes(data.model_id)) return 'ai_model_missing';
    if (provider.preset && !['ollama', 'lmstudio'].includes(provider.preset) && !provider.has_api_key) {
      return 'ai_key_unavailable';
    }
    return null;
  });

  /**
   * Refresh provider availability without changing an existing node selection.
   *
   * @returns A promise resolved when the catalog request settles.
   */
  async function refresh() {
    if (!editable || $loading !== null) return;
    controller = new AbortController();
    loading.start();
    failed = false;
    try {
      const response = await api
        .get('ai/provider/list', { signal: controller.signal, context: { silentErrors: true } })
        .json<Resp<AIProvider[]>>();
      providers = response.data;
    } catch {
      if (!controller.signal.aborted) failed = true;
    } finally {
      loading.end();
    }
  }

  /**
   * Select a provider and require an explicit model selection.
   *
   * @param event - Provider select change event.
   */
  function selectProvider(event: Event) {
    const providerId = Number((event.currentTarget as HTMLSelectElement).value);
    updateNodeData(field.nodeId, { [field.id]: { provider_id: providerId, model_id: '' } });
  }

  /**
   * Save the selected model without copying provider credentials.
   *
   * @param event - Model select change event.
   */
  function selectModel(event: Event) {
    updateNodeData(field.nodeId, {
      [field.id]: { provider_id: data?.provider_id, model_id: (event.currentTarget as HTMLSelectElement).value }
    });
  }

  /**
   * Report missing selections on the provider and model controls.
   *
   * @returns Whether the controls and any required stored selection are valid.
   */
  function reportValidity(): boolean {
    return (
      providerSelect.reportValidity() &&
      modelSelect.reportValidity() &&
      (!field.required || (!!data?.provider_id && !!data?.model_id))
    );
  }

  if (hasContext('flow/graph')) {
    const context = getContext<FlowGraphContext>('flow/graph');
    context.addValidator(reportValidity);
  }

  $effect(() => {
    if (editable) void untrack(refresh);
    return () => controller?.abort();
  });
</script>

<svelte:window onfocus={() => void refresh()} />

<fieldset class="fieldset">
  <div class="flex items-center justify-between gap-2">
    <Label required={field.required}>{$label(field.label)}</Label>
    {#if editable}
      <Button
        class="nodrag"
        size="xs"
        icon={icons.redo}
        text={$_('action.reload', $_('entity.provider'))}
        loading={$loading}
        onclick={() => void refresh()}
      />
    {/if}
  </div>
  <div class="grid grid-cols-2 gap-2">
    <select
      class="nodrag select w-full appearance-none select-sm"
      aria-label={$_('entity.provider')}
      required={field.required}
      value={data?.provider_id ?? ''}
      bind:this={providerSelect}
      disabled={!editable || $loading !== null}
      onchange={selectProvider}
    >
      <option disabled value="">
        {providers?.length === 0 ? $_('ai.empty') : $_('action.select', $_('entity.provider'))}
      </option>
      {#if data?.provider_id && !provider}
        <option value={data.provider_id}>#{data.provider_id}</option>
      {/if}
      {#each providers ?? [] as item (item.id)}
        <option value={item.id} disabled={!item.enabled}>
          {item.name}{item.enabled ? '' : ` (${$_('ai.disabled')})`}
        </option>
      {/each}
    </select>
    <select
      class="nodrag select w-full appearance-none select-sm"
      aria-label={$_('ai.models')}
      required={field.required}
      value={data?.model_id ?? ''}
      bind:this={modelSelect}
      disabled={!editable || $loading !== null || !provider?.enabled}
      onchange={selectModel}
    >
      <option disabled value="">{$_('action.select', $_('ai.models'))}</option>
      {#if data?.model_id && !provider?.model_ids.includes(data.model_id)}
        <option value={data.model_id}>{data.model_id}</option>
      {/if}
      {#each provider?.model_ids ?? [] as model (model)}
        <option value={model}>{model}</option>
      {/each}
    </select>
  </div>
  {#if failed || issue}
    <p class="max-w-lg text-xs text-error" role="status">
      {failed ? $_('ai.load_failed') : $_(`alert.${issue}`)}
    </p>
  {/if}
</fieldset>
