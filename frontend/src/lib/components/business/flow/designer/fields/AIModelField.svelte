<script lang="ts">
  import { api } from '$lib/api';
  import { decodeModelSelection, encodeModelKey, encodeModelSelection } from '$lib/assistant/panel';
  import { Button, Label } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _, nodeFormatter } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { user } from '$lib/stores.svelte';
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
  let modelSelect: HTMLSelectElement;

  const editable = $derived(user.current?.role === 'admin');
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
   * Save the selected provider and model without copying provider credentials.
   *
   * @param event - Model select change event.
   */
  function selectModel(event: Event) {
    const selection = decodeModelSelection((event.currentTarget as HTMLSelectElement).value);
    if (selection) updateNodeData(field.nodeId, { [field.id]: selection });
  }

  /**
   * Report a missing selection on the model control.
   *
   * @returns Whether the control and any required stored selection are valid.
   */
  function reportValidity(): boolean {
    return modelSelect.reportValidity() && (!field.required || (!!data?.provider_id && !!data?.model_id));
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
  <select
    class="nodrag select w-full appearance-none select-sm"
    aria-label={$_('ai.models')}
    required={field.required}
    value={(data?.provider_id && data.model_id && encodeModelSelection(data)) || ''}
    bind:this={modelSelect}
    disabled={!editable || $loading !== null}
    onchange={selectModel}
  >
    <option disabled value="">
      {providers?.length === 0 ? $_('ai.empty') : $_('action.select', $_('ai.models'))}
    </option>
    {#if data?.provider_id && data.model_id && !provider?.model_ids.includes(data.model_id)}
      <option value={encodeModelSelection(data)}>{provider?.name ?? `#${data.provider_id}`} · {data.model_id}</option>
    {/if}
    {#each providers ?? [] as item (item.id)}
      {#if item.model_ids.length}
        <optgroup label="{item.name}{item.enabled ? '' : ` (${$_('ai.disabled')})`}" disabled={!item.enabled}>
          {#each item.model_ids as model (model)}
            <option value={encodeModelKey(item.id, model)}>{model}</option>
          {/each}
        </optgroup>
      {/if}
    {/each}
  </select>
  {#if failed || issue}
    <p class="max-w-lg text-xs text-error" role="status">
      {failed ? $_('ai.load_failed') : $_(`alert.${issue}`)}
    </p>
  {/if}
</fieldset>
