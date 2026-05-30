<script lang="ts" module>
  import type { DNSResolver, HTTPProxy, Resp, URLRule } from '$lib/types';

  type URLRuleEditorProps = Partial<{
    id: number;
    pattern: string;
    resolver_ids: number[];
    resolvers: DNSResolver[];
    proxy_id: number | null;
    proxies: HTTPProxy[];
    onsave: (result: URLRule) => void;
  }>;
</script>

<script lang="ts">
  import { enhance } from '$app/forms';
  import { api } from '$lib/api';
  import { Label, Modal, URLWrapper } from '$lib/components';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';

  let {
    id,
    pattern,
    resolver_ids = [],
    resolvers = [],
    proxy_id = null,
    proxies = [],
    onsave
  }: URLRuleEditorProps = $props();

  // the modal dialog instance
  let modal: Modal;
  export const showModal = () => modal.show();

  // the URL wrapper instance
  let urlWrapper: URLWrapper | null = $state(null);
  let secure: boolean = $state(true);

  // the loading state and form schema
  const loading = createLoading();
  const schema = createFormSchema(({ text }) => ({
    pattern: text().maxlength(245)
  }));

  /**
   * Save or update the URL rule.
   *
   * @param form - The form element.
   * @param data - The form data.
   */
  function upsert(form: HTMLFormElement, data: FormData) {
    loading.start();
    const json: Record<string, unknown> = Object.fromEntries(data);
    json.id = id;
    json.pattern = urlWrapper?.full(pattern);
    json.proxy_id = proxy_id;
    json.resolver_ids = resolver_ids;
    api
      .post('network/rule/upsert', { json })
      .json<Resp<URLRule>>()
      .then((resp) => {
        modal.close();
        onsave?.(resp.data);
        setTimeout(() => {
          form.reset();
          proxy_id = null;
          resolver_ids = [];
        }, 200);
      })
      .finally(() => {
        loading.end();
      });
  }

  $effect(() => {
    if (urlWrapper) {
      pattern = urlWrapper.standardize(pattern);
    }
  });
</script>

<Modal icon={icons.globeDesktop} title={$_(id ? 'action.edit' : 'action.add', $_('entity.rule'))} bind:this={modal}>
  <form
    method="post"
    use:enhance={({ formElement, formData, cancel }) => {
      cancel();
      upsert(formElement, formData);
    }}
  >
    <fieldset class="fieldset">
      <Label required>{$_('field.pattern')}</Label>
      <URLWrapper bind:secure bind:this={urlWrapper}>
        <input placeholder="*" class="grow truncate" bind:value={pattern} {...schema.pattern} />
      </URLWrapper>
      {#if resolvers.length > 0}
        <Label class="mt-4">{$_('entity.dns_resolvers')}</Label>
        <div class="flex flex-wrap gap-4">
          {#each resolvers as resolver (resolver.id)}
            <label class="label">
              <input
                type="checkbox"
                class="checkbox"
                checked={resolver_ids.includes(resolver.id)}
                onchange={() => {
                  if (resolver_ids.includes(resolver.id)) {
                    resolver_ids = resolver_ids.filter((r) => r !== resolver.id);
                  } else {
                    resolver_ids = [...resolver_ids, resolver.id];
                  }
                }}
              />
              <span class="text-base text-base-content/80">{resolver.name}</span>
            </label>
          {/each}
        </div>
      {/if}
      {#if proxies.length > 0}
        <Label class="mt-4">{$_('entity.proxy_server')}</Label>
        <div class="flex flex-wrap gap-4">
          {#each proxies as proxy (proxy.id)}
            <label class="label">
              <input
                type="radio"
                class="radio"
                checked={proxy_id === proxy.id}
                onclick={() => {
                  proxy_id = proxy_id === proxy.id ? null : proxy.id;
                }}
              />
              <span class="text-base text-base-content/80">{proxy.name}</span>
            </label>
          {/each}
        </div>
      {/if}
    </fieldset>
    <div class="modal-action">
      <button type="button" class="btn" onclick={() => modal.close()}>
        {$_('message.cancel')}
      </button>
      <button type="submit" class="btn btn-submit" disabled={$loading !== null}>
        {$_('message.confirm')}
        {#if $loading}
          <span class="loading loading-xs loading-dots"></span>
        {/if}
      </button>
    </div>
  </form>
</Modal>
