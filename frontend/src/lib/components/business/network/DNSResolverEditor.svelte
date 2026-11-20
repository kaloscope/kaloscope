<script lang="ts" module>
  import type { DNSResolver, Resp } from '$lib/types';

  type DNSResolverEditorProps = Partial<{
    id: number;
    name: string;
    protocol: 'tls' | 'https';
    nameserver: string;
    dnssec: boolean;
    onsave: (result: DNSResolver) => void;
  }>;
</script>

<script lang="ts">
  import { enhance } from '$app/forms';
  import { api } from '$lib/api';
  import { Label, Modal } from '$lib/components';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';

  let { id, name, protocol = 'tls', nameserver, dnssec = false, onsave }: DNSResolverEditorProps = $props();

  // the modal dialog instance
  let modal: Modal;
  export const showModal = () => modal.show();

  // the loading state and form schema
  const loading = createLoading();
  const schema = createFormSchema(({ text }) => ({
    name: text().maxlength(64),
    nameserver: text().maxlength(255)
  }));

  /**
   * Save or update the DNS resolver.
   *
   * @param data - Form data.
   */
  function upsert(data: FormData) {
    loading.start();
    const json: Record<string, unknown> = Object.fromEntries(data);
    json.id = id;
    api
      .post('network/dns/upsert', { json })
      .json<Resp<DNSResolver>>()
      .then(({ data }) => {
        modal.close();
        onsave?.(data);
        setTimeout(() => {
          // reset the form
          name = '';
          protocol = 'tls';
          nameserver = '';
          dnssec = false;
        }, 200);
      })
      .finally(() => {
        loading.end();
      });
  }
</script>

<Modal
  icon={icons.bookGlobe}
  title={$_(id ? 'action.edit' : 'action.add', $_('entity.dns_resolver'))}
  bind:this={modal}
>
  <form
    method="post"
    use:enhance={({ formData, cancel }) => {
      cancel();
      upsert(formData);
    }}
  >
    <fieldset class="fieldset">
      <Label required>{$_('field.name')}</Label>
      <input class="input w-full" bind:value={name} {...schema.name} />
      <Label required>{$_('field.protocol')}</Label>
      <div class="flex flex-wrap gap-4">
        <label class="flex cursor-pointer items-center gap-1.5">
          <input type="radio" class="radio radio-sm" name="protocol" value="tls" bind:group={protocol} />
          <span class="text-sm">DNS over TLS</span>
        </label>
        <label class="flex cursor-pointer items-center gap-1.5">
          <input type="radio" class="radio radio-sm" name="protocol" value="https" bind:group={protocol} />
          <span class="text-sm">DNS over HTTPS</span>
        </label>
      </div>
      <Label required>{$_('field.nameserver')}</Label>
      <input placeholder="1.1.1.1" class="input w-full" bind:value={nameserver} {...schema.nameserver} />
      <label class="mt-2 fieldset-label w-fit">
        <input type="checkbox" class="checkbox checkbox-sm" name="dnssec" bind:checked={dnssec} />
        <span class="text-sm text-base-content/80">DNSSEC</span>
      </label>
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
