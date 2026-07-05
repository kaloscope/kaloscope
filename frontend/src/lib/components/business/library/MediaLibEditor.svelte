<script lang="ts" module>
  import { enumToOptions, LibType } from '$lib/enums';
  import type { FlowTrigger, MediaLib, Resp } from '$lib/types';

  type MediaLibEditorProps = Partial<{
    id: number;
    lib_type: keyof typeof LibType;
    dir: string;
    name: string;
    language: string | null;
    danmaku_server: string | null;
    danmaku_ttl: number;
    triggers: FlowTrigger[];
    onsave: (result: MediaLib) => void;
  }>;
</script>

<script lang="ts">
  import { enhance } from '$app/forms';
  import { api } from '$lib/api';
  import { FileTree, FlowTriggers, Label, Modal, Select, URLWrapper } from '$lib/components';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import { _, locales } from '$lib/i18n';
  import { icons } from '$lib/icons';

  let {
    id,
    lib_type,
    dir,
    name,
    language = '',
    danmaku_server,
    danmaku_ttl = 24,
    triggers,
    onsave
  }: MediaLibEditorProps = $props();

  // the modal dialog instance
  let modal: Modal;
  export const showModal = () => modal.show();

  // the file tree instance
  let fileTree: FileTree;

  // the URL wrapper instance
  let urlWrapper: URLWrapper | null = $state(null);
  let secure: boolean = $state(true);

  // the loading state and form schema
  const loading = createLoading();
  const schema = createFormSchema(({ text, number }) => ({
    dir: text().maxlength(4096),
    name: text().maxlength(64),
    danmaku_server: text().maxlength(245).required(false),
    danmaku_ttl: number().min(0).max(8760).required(false)
  }));

  /**
   * Save or update the media library.
   *
   * @param form - The form element.
   * @param data - The form data.
   */
  function upsert(form: HTMLFormElement, data: FormData) {
    loading.start();
    const json: Record<string, unknown> = Object.fromEntries(data);
    json.id = id;
    json.danmaku_server = urlWrapper?.full(danmaku_server);
    json.triggers = triggers;
    api
      .post('media/lib/upsert', { json })
      .json<Resp<MediaLib>>()
      .then(({ data }) => {
        modal.close();
        onsave?.(data);
        setTimeout(() => form.reset(), 200);
      })
      .finally(() => {
        loading.end();
      });
  }

  $effect(() => {
    if (urlWrapper) {
      danmaku_server = urlWrapper.standardize(danmaku_server);
    }
  });
</script>

<Modal
  icon={icons.videoClipMultiple}
  title={$_(id ? 'action.edit' : 'action.add', $_('entity.media_lib'))}
  bind:this={modal}
>
  <form
    method="post"
    use:enhance={({ formElement, formData, cancel }) => {
      cancel();
      upsert(formElement, formData);
    }}
  >
    <fieldset class="fieldset">
      <Label required>{$_('field.type')}</Label>
      <Select
        translate
        options={enumToOptions(LibType, false)}
        bind:value={lib_type}
        name="lib_type"
        class="w-full"
        disabled={!!id}
      />
      <div class="flex gap-2">
        <div class="w-3/5 space-y-1.5">
          <Label required>{$_('field.name')}</Label>
          <input placeholder={$_('field.name')} class="input w-full truncate" bind:value={name} {...schema.name} />
        </div>
        <div class="w-2/5 space-y-1.5">
          <Label>{$_('field.language')}</Label>
          <Select bind:value={language} name="language" class="w-full">
            <option value="">{$_('enum.none')}</option>
            {#each $locales.filter((l) => l !== 'languages') as code (code)}
              <option value={code}>{$_(code, { locale: 'languages' })}</option>
            {/each}
          </Select>
        </div>
      </div>
      <Label required>{$_('field.dir')}</Label>
      <!-- svelte-ignore a11y_consider_explicit_label -->
      <button
        type="button"
        class="input w-full {id ? 'cursor-not-allowed' : 'cursor-pointer'}"
        onclick={() => fileTree.showModal()}
        disabled={!!id}
      >
        <iconify-icon icon={icons.folder} width="1.5rem" class="opacity-50"></iconify-icon>
        <input
          type="text"
          autocomplete="off"
          placeholder={$_('action.select', $_('field.dir'))}
          class="grow truncate text-left direction-rtl {id ? 'cursor-not-allowed' : 'cursor-pointer'}"
          value={dir?.split('').reverse().join('')}
          disabled={!!id}
          readonly
        />
        <input type="text" class="hidden" name="dir" value={dir} />
      </button>
      <div class="flex flex-wrap gap-2">
        <div class="flex-3/5 space-y-1.5">
          <Label tip={$_('media.danmaku.server_tip')}>{$_('media.danmaku.server')}</Label>
          <URLWrapper bind:secure bind:this={urlWrapper}>
            <input
              placeholder="danmaku.kaloscope.org"
              class="grow truncate"
              bind:value={danmaku_server}
              {...schema.danmaku_server}
            />
          </URLWrapper>
        </div>
        <div class="grow space-y-1.5">
          <Label>{$_('media.danmaku.ttl')}</Label>
          <input
            placeholder={$_('media.danmaku.ttl')}
            class="input w-full"
            bind:value={danmaku_ttl}
            {...schema.danmaku_ttl}
          />
        </div>
      </div>
      <FlowTriggers class="mt-4" category="ingest" {triggers} onchange={(newTriggers) => (triggers = newTriggers)} />
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

<FileTree bind:this={fileTree} onlyDirs={true} onconfirm={(stats) => (dir = stats.path)} />
