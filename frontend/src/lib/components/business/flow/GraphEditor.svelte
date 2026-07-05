<script lang="ts" module>
  import type { FlowGraph, Resp } from '$lib/types';

  type GraphEditorProps = Partial<{
    id: number;
    name: string;
    icon: string | null;
    description: string | null;
    category: keyof typeof GraphCategory;
    onsave: (result: FlowGraph) => void;
  }>;
</script>

<script lang="ts">
  import { enhance } from '$app/forms';
  import { cropper } from '$lib/actions';
  import { api } from '$lib/api';
  import { Image, Label, Modal, Select } from '$lib/components';
  import { enumToOptions, GraphCategory } from '$lib/enums';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { loadFile } from '$lib/utils';

  let { id, name, icon, description, category, onsave }: GraphEditorProps = $props();

  // the modal dialog instance
  let modal: Modal;
  export const showModal = () => modal.show();

  // the icon input element
  let iconInput: HTMLInputElement;
  let cropSrc: string | null = $state(null);
  let cropImg: Promise<Blob | null> | null = null;

  // the loading state and form schema
  const loading = createLoading();
  const schema = createFormSchema(({ text, textarea }) => ({
    name: text().maxlength(64),
    description: textarea().maxlength(255).required(false)
  }));

  /**
   * Save or update the flow graph basics.
   *
   * @param form - The form element.
   * @param data - The form data.
   */
  async function upsert(form: HTMLFormElement, data: FormData) {
    loading.start();
    if (id) {
      data.append('id', id.toString());
    }
    if (icon) {
      data.append('icon', icon);
    }
    const image = cropSrc && (await cropImg);
    if (image) {
      data.append('image', image);
    }
    api
      .post('flow/graph/upsert', { body: data })
      .json<Resp<FlowGraph>>()
      .then(({ data }) => {
        cropSrc = null;
        onsave?.(data);
        id ? modal.close() : setTimeout(() => form.reset(), 200);
      })
      .finally(() => {
        loading.end();
      });
  }
</script>

<Modal icon={icons.flowchart} title={$_(id ? 'action.edit' : 'action.add', $_('entity.graph'))} bind:this={modal}>
  <form
    method="post"
    use:enhance={({ formElement, formData, cancel }) => {
      cancel();
      upsert(formElement, formData);
    }}
  >
    <div class="flex-col-center">
      {#if cropSrc}
        <div
          tabindex="0"
          role="button"
          class="flex-center size-24 overflow-hidden rounded-sm border"
          ondblclick={() => iconInput.click()}
        >
          {#key cropSrc}
            <img src={cropSrc} alt="" class="size-full" use:cropper={(img) => (cropImg = img)} />
          {/key}
        </div>
      {:else}
        <div class="group relative size-24">
          <Image
            border
            transparent
            src={icon}
            icon={icons.documentFlowchart}
            width="96px"
            onclick={() => iconInput.click()}
          />
          {#if icon}
            {@const opacityClass = 'opacity-0 group-hover:opacity-100 transition-opacity'}
            <button
              class="btn absolute top-0.5 right-0.5 btn-square btn-xs {opacityClass}"
              aria-label="Delete"
              onclick={(event) => {
                event.preventDefault();
                cropSrc = '';
                icon = '';
                iconInput.value = '';
              }}
            >
              <iconify-icon icon={icons.delete} width="1rem"></iconify-icon>
            </button>
          {/if}
        </div>
      {/if}
      <input
        type="file"
        class="hidden"
        accept="image/*"
        bind:this={iconInput}
        onchange={async (event) => (cropSrc = await loadFile(event.currentTarget))}
      />
    </div>
    <fieldset class="fieldset">
      <Label required>{$_('field.category')}</Label>
      <Select
        translate
        options={enumToOptions(GraphCategory, false)}
        bind:value={category}
        name="category"
        class="w-full"
        disabled={!!id}
      />
      <Label required>{$_('field.name')}</Label>
      <input placeholder={$_('field.name')} class="input w-full truncate" bind:value={name} {...schema.name} />
      <Label>{$_('field.description')}</Label>
      <textarea
        rows={5}
        placeholder={$_('field.description')}
        class="textarea w-full"
        bind:value={description}
        {...schema.description}></textarea>
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
