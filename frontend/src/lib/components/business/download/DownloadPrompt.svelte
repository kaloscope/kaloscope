<script lang="ts" module>
  import { api } from '$lib/api';
  import { DEFAULT_SUB_PATTERN, DEFAULT_SUB_REPL } from '$lib/constants';
  import { TransferMethod } from '$lib/enums';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import type { DownloadDir, Downloader, MediaLib, Resp } from '$lib/types';

  // the modal dialog and file tree instance
  let modal: Modal;
  let fileTree: FileTree;

  // the download directory and downloaders
  let directory: DownloadDir | null = $state(null);
  let downloaders: Downloader[] = $state([]);
  let downloaderId: number = $state(0);

  // the media libraries and transfer options
  let mediaLibs: MediaLib[] = $state([]);
  let transferLibId: number = $state(0);
  let transferMethod: keyof typeof TransferMethod = $state('hardlink');
  let supportsHardlink: boolean = $state(true);
  let subPattern: string = $state(DEFAULT_SUB_PATTERN);
  let subRepl: string = $state(DEFAULT_SUB_REPL);

  // the torrent input and magnet link
  let files: FileList | null = $state(null);
  let link: string = $state('');

  // the submittable state
  let submittable = $derived.by(() => {
    if (downloaderId === 0) {
      return false;
    }
    if (files && files.length > 0) {
      return true;
    }
    return link.trim() !== '';
  });

  // the loading state and form schema
  const loading = createLoading();
  const schema = createFormSchema(({ text }) => ({
    sub_pattern: text().maxlength(512).required(false),
    sub_repl: text().maxlength(512).required(false)
  }));

  // the oncreate callback
  let oncreate: (() => void) | null;

  /**
   * Show the download prompt modal.
   *
   * @param uri - The URI to download, if any.
   * @param callback - Optional callback to execute after the download is created.
   */
  export function downloadPrompt(uri?: string | null, callback?: () => void) {
    init().then(() => {
      oncreate = callback ?? null;
      // if a URI is provided, use it as the link and clear the files
      if (uri) {
        link = uri;
        files = null;
        modal.show();
        return;
      }
      // if no URI is provided, try to read a link from the clipboard
      if (navigator.clipboard) {
        navigator.clipboard
          .readText()
          .then((text) => {
            text = text.trim();
            if (!text || text === link) {
              return null;
            }
            // check if the text is a valid download link
            return api
              .post('download/validate', { body: text })
              .json<Resp<boolean>>()
              .then(({ data }) => (data ? text : null));
          })
          .then((text) => {
            if (text) {
              link = text;
              files = null;
            }
          })
          .finally(() => modal.show());
      } else {
        modal.show();
      }
    });
  }

  /**
   * Initialize the component.
   */
  async function init() {
    const [_directories, _downloaders, _mediaLibs] = await Promise.all([
      getDirectories(),
      getDownloaders(),
      getMediaLibs()
    ]);
    downloaders = _downloaders;
    mediaLibs = _mediaLibs;
    // set the initial values
    if (!directory) {
      directory = _directories[0] || null;
    }
    if (downloaders.length === 0) {
      downloaderId = 0;
    } else if (!downloaderId) {
      downloaderId = downloaders.find((d) => d.status !== 'down')?.id ?? 0;
    }
  }

  /**
   * Get the download directories.
   */
  async function getDirectories(): Promise<DownloadDir[]> {
    try {
      const resp = await api.get('download/dir/list').json<Resp<DownloadDir[]>>();
      return resp.data;
    } catch (error) {
      console.error(error);
      return [];
    }
  }

  /**
   * Get the downloaders.
   */
  async function getDownloaders(): Promise<Downloader[]> {
    try {
      const resp = await api.get('download/manager/list').json<Resp<Downloader[]>>();
      return resp.data;
    } catch (error) {
      console.error(error);
      return [];
    }
  }

  /**
   * Get the media libraries.
   */
  async function getMediaLibs(): Promise<MediaLib[]> {
    try {
      const resp = await api.get('media/lib/list').json<Resp<MediaLib[]>>();
      return resp.data;
    } catch (error) {
      console.error(error);
      return [];
    }
  }

  /**
   * Get the platform.
   */
  async function getPlatform(): Promise<string> {
    try {
      const resp = await api.get('system/platform').json<Resp<{ platform: string }>>();
      return resp.data.platform;
    } catch (error) {
      console.error(error);
      return '';
    }
  }

  /**
   * Add a download task.
   *
   * @param data - The form data.
   */
  function addTask(data: FormData) {
    loading.start();
    // append the pause field based on the start checkbox
    data.append('pause', (!data.get('start')).toString());
    data.delete('start');
    // delete the transfer fields if no media library is selected
    if (!transferLibId) {
      data.delete('transfer_lib_id');
      data.delete('transfer_method');
      data.delete('sub_pattern');
      data.delete('sub_repl');
    }
    api
      .post('download/add', { body: data })
      .then(() => {
        modal.close();
        oncreate?.();
        setTimeout(() => {
          // reset the form
          transferLibId = 0;
          transferMethod = supportsHardlink ? 'hardlink' : 'symlink';
          subPattern = DEFAULT_SUB_PATTERN;
          subRepl = DEFAULT_SUB_REPL;
        }, 200);
      })
      .finally(() => {
        loading.end();
      });
  }
</script>

<script lang="ts">
  import { enhance } from '$app/forms';
  import { FileTree, Label, Modal, Select } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { onMount } from 'svelte';

  let _modal: Modal;
  let _fileTree: FileTree;

  onMount(() => {
    // assign the modal dialog and file tree instances to the module variables
    modal = _modal;
    fileTree = _fileTree;
    // initialize platform-dependent variables once at mount time
    getPlatform().then((platform) => {
      supportsHardlink = platform !== 'win32';
      transferMethod = supportsHardlink ? 'hardlink' : 'symlink';
    });
  });
</script>

<Modal icon={icons.download} title={$_('action.add', $_('entity.download'))} maxWidth="36rem" bind:this={_modal}>
  <form
    method="post"
    enctype="multipart/form-data"
    use:enhance={({ formData, cancel }) => {
      cancel();
      addTask(formData);
    }}
  >
    <fieldset class="fieldset">
      <Label required>{$_('download.downloader.title')}</Label>
      <Select
        required
        options={downloaders.map((d) => ({
          value: d.id,
          label: d.name,
          disabled: d.status === 'down'
        }))}
        bind:value={downloaderId}
        name="downloader_id"
        class="w-full"
      />
      <Label required>{$_('download.dir')}</Label>
      {#if directory}
        <button type="button" class="input w-full cursor-pointer" onclick={() => fileTree.showModal(directory?.path)}>
          <iconify-icon icon={icons.folder} width="1.5rem" class="opacity-50"></iconify-icon>
          <input
            type="text"
            autocomplete="off"
            class="grow cursor-pointer truncate text-left direction-rtl"
            value={directory.path.split('').reverse().join('')}
            readonly
          />
          <input type="text" class="hidden" name="dir" value={directory.path} />
          <span class="italic-text">{directory.free}</span>
        </button>
      {:else}
        <label class="input w-full">
          <iconify-icon icon={icons.folder} width="1.5rem" class="opacity-50"></iconify-icon>
          <input type="text" class="grow" name="dir" />
        </label>
      {/if}
      <Label required class="mt-6">{$_('download.prompt')}</Label>
      <textarea
        rows={5}
        placeholder={$_('download.supported')}
        class="textarea w-full"
        name="link"
        bind:value={link}
        disabled={files && files.length > 0}
      ></textarea>
      <input type="file" accept=".torrent" class="file-input w-full file-input-sm" name="torrent" bind:files />
      <label class="mt-2 fieldset-label w-fit">
        <input type="checkbox" class="checkbox checkbox-sm" checked={true} name="start" />
        <span class="text-base text-base-content opacity-90">{$_('download.start')}</span>
      </label>
      <Label class="mt-6">{$_('download.transfer.title')}</Label>
      <Select
        options={[
          { value: 0, label: $_('download.transfer.none') },
          ...mediaLibs.map((lib) => ({ value: lib.id, label: lib.name }))
        ]}
        bind:value={transferLibId}
        name="transfer_lib_id"
        class="w-full"
      />
      {#if transferLibId}
        <Label required>{$_('download.transfer.method')}</Label>
        <div class="flex flex-wrap gap-4">
          {#each Object.entries(TransferMethod) as [value, method] (value)}
            {#if value !== 'hardlink' || supportsHardlink}
              <label class="flex cursor-pointer items-center gap-1.5">
                <input type="radio" class="radio radio-sm" name="transfer_method" {value} bind:group={transferMethod} />
                <span class="text-sm">{$_(method.label)}</span>
              </label>
            {/if}
          {/each}
        </div>
        <Label>{$_('download.transfer.substitution')}</Label>
        <div class="flex gap-2">
          <input
            placeholder={$_('download.transfer.sub_pattern')}
            class="input w-full truncate"
            bind:value={subPattern}
            {...schema.sub_pattern}
          />
          <input
            placeholder={$_('download.transfer.sub_repl')}
            class="input w-full truncate"
            bind:value={subRepl}
            {...schema.sub_repl}
            disabled={!subPattern}
          />
        </div>
      {/if}
    </fieldset>
    <div class="modal-action">
      <button type="button" class="btn" onclick={() => modal.close()}>
        {$_('message.cancel')}
      </button>
      <button type="submit" class="btn btn-submit" disabled={$loading !== null || !submittable}>
        {$_('message.confirm')}
        {#if $loading}
          <span class="loading loading-xs loading-dots"></span>
        {/if}
      </button>
    </div>
  </form>
</Modal>

<FileTree
  bind:this={_fileTree}
  onlyDirs={true}
  onconfirm={(stats) => {
    directory = { path: stats.path, free: stats.free };
  }}
/>
