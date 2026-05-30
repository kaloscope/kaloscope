<script lang="ts" module>
  import type { Downloader, Resp } from '$lib/types';

  type DownloaderEditorProps = Partial<{
    id: number;
    preset: string;
    exists: string[];
    config: string;
    onsave: (result: Downloader) => void;
  }>;

  /**
   * The default configuration template.
   */
  const CONFIG_TEMPLATE = `
# Name
name: downloader

# URL
protocol: http
host: 127.0.0.1
port: 8080
path: /

# Authentication
auth:
  username:
  password:

# API methods
methods:
  version:
  login:
  add_link:
  add_torrent:
  list:
  details:
  pause:
  start:
  delete:
`.trimStart();

  /**
   * The downloader presets.
   */
  let presets: Record<string, string> | null = $state(null);
</script>

<script lang="ts">
  import { enhance } from '$app/forms';
  import { api } from '$lib/api';
  import { CodeMirror, Label, Modal, confirm } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { yaml } from '@codemirror/lang-yaml';
  import { onMount } from 'svelte';

  let { id, preset = '', exists = [], config = CONFIG_TEMPLATE, onsave }: DownloaderEditorProps = $props();
  let codeMirror: CodeMirror;

  // the modal dialog instance
  let modal: Modal;
  export const showModal = () => modal.show();

  // the loading state
  const loading = createLoading();

  /**
   * Save or update the downloader.
   *
   * @param form - The form element.
   * @param data - The form data.
   */
  function upsert(form: HTMLFormElement, data: FormData) {
    loading.start();
    const json: Record<string, unknown> = Object.fromEntries(data);
    json.id = id;
    json.config = config;
    api
      .post('download/manager/upsert', { json })
      .json<Resp<Downloader>>()
      .then((resp) => {
        modal.close();
        onsave?.(resp.data);
        setTimeout(() => form.reset(), 200);
      })
      .finally(() => {
        loading.end();
      });
  }

  onMount(() => {
    if (presets === null) {
      api
        .get('download/manager/presets')
        .json<Resp<Record<string, string>>>()
        .then((resp) => (presets = resp.data));
    }
  });
</script>

<Modal
  icon={icons.box3dDownload}
  title={$_(id ? 'action.edit' : 'action.add', $_('download.downloader.config'))}
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
      <Label>{$_('download.downloader.preset')}</Label>
      <select
        class="select w-full appearance-none {preset ? '' : 'text-base-content/50'}"
        value={preset}
        name="preset"
        disabled={!!id}
        onchange={(event) => {
          const target = event.currentTarget;
          const onconfirm = () => {
            preset = target.value;
            codeMirror.setDocument(presets?.[preset] || CONFIG_TEMPLATE, true);
          };
          if ((preset === '' && config === CONFIG_TEMPLATE) || (preset && config === presets?.[preset])) {
            // change the preset directly
            onconfirm();
          } else {
            // confirm to change the preset
            confirm({
              message: $_('message.leave.content'),
              oncancel: () => (target.value = preset),
              onconfirm: onconfirm
            });
          }
        }}
      >
        <option value="">{$_('enum.none')}</option>
        {#if presets}
          {#each Object.keys(presets).sort((a, b) => a.localeCompare(b)) as key (key)}
            <option value={key} disabled={exists.includes(key)}>{key}</option>
          {/each}
        {/if}
      </select>
      <Label required>{$_('download.downloader.config')}</Label>
      <CodeMirror
        darkMode
        minWidth="100%"
        maxWidth="100%"
        maxHeight="18rem"
        language={yaml()}
        title={$_('download.downloader.config')}
        bind:this={codeMirror}
        bind:document={config}
      />
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
