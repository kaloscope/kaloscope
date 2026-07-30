<script lang="ts" module>
  import type { Downloader, Resp } from '$lib/types';
  import { isMap, isPair, isScalar, parseDocument, type Document, type ParsedNode } from 'yaml';

  type DownloaderEditorProps = Partial<{
    id: number;
    preset: string;
    config: string;
    onsave: (result: Downloader) => void;
  }>;

  type DownloaderSimpleField = 'name' | 'host' | 'port' | 'username' | 'password' | 'secret';

  type DownloaderSimpleValues = {
    name: string;
    secure: boolean;
    host: string;
    port: number;
    username: string;
    password: string;
    secret: string;
  };

  type DownloaderSimpleConfig = {
    fields: DownloaderSimpleField[];
    values: DownloaderSimpleValues;
  };

  /**
   * Parse a downloader YAML document and require a top-level mapping.
   *
   * @param source - The downloader YAML source.
   * @returns The parsed YAML document.
   */
  function parseConfig(source: string): Document.Parsed<ParsedNode> {
    const document = parseDocument(source);
    if (document.errors.length > 0) {
      throw new Error(`Invalid downloader YAML: ${document.errors.map((error) => error.message).join('; ')}`);
    }
    if (!isMap(document.contents)) {
      throw new Error('Invalid downloader YAML: the root must be a mapping');
    }
    return document as Document.Parsed<ParsedNode>;
  }

  /**
   * Convert an optional YAML scalar value to an editable string.
   *
   * @param value - The YAML value.
   * @returns The string value or an empty string for `null` and `undefined`.
   */
  function stringValue(value: unknown): string {
    return value === null || value === undefined ? '' : String(value);
  }

  /**
   * Convert an optional YAML scalar value to a number.
   *
   * @param value - The YAML value.
   * @returns The finite number or `0`.
   */
  function numberValue(value: unknown): number {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  /**
   * Keep an empty authentication value as YAML `null`.
   *
   * @param value - The editable authentication value.
   * @returns The original value or `null` when empty.
   */
  function authenticationValue(value: string): string | null {
    return value === '' ? null : value;
  }

  /**
   * Move a section comment misparsed onto the last nested scalar to the following top-level key.
   *
   * The `yaml` parser may attach a comment after an empty scalar to that scalar, and `setIn` preserves it.
   *
   * @param document - The parsed YAML document.
   * @param key - The top-level mapping key whose final scalar may own the comment.
   */
  function restoreFollowingSectionComment(document: Document.Parsed<ParsedNode>, key: string) {
    if (!isMap(document.contents)) {
      return;
    }

    const entries = document.contents.items;
    const sectionIndex = entries.findIndex((entry) => isPair(entry) && isScalar(entry.key) && entry.key.value === key);
    const section = entries[sectionIndex];
    const following = entries[sectionIndex + 1];

    if (!isPair(section) || !isMap(section.value) || !isPair(following) || !isScalar(following.key)) {
      return;
    }

    const trailing = section.value.items[section.value.items.length - 1];
    if (!isPair(trailing) || !isScalar(trailing.value) || !trailing.value.spaceBefore || !trailing.value.comment) {
      return;
    }

    const comment = trailing.value.comment;
    trailing.value.comment = null;
    trailing.value.spaceBefore = false;
    following.key.commentBefore = following.key.commentBefore ? `${comment}\n${following.key.commentBefore}` : comment;
    following.key.spaceBefore = true;
  }

  /**
   * Read the simple fields supported by a downloader YAML document.
   *
   * @param source - The downloader YAML source.
   * @returns The inferred fields and their editable values.
   */
  function readDownloaderConfig(source: string): DownloaderSimpleConfig {
    const document = parseConfig(source);
    const fields: DownloaderSimpleField[] = [];

    document.has('name') && fields.push('name');
    document.has('host') && fields.push('host');
    document.has('port') && fields.push('port');
    document.hasIn(['auth', 'username']) && fields.push('username');
    document.hasIn(['auth', 'password']) && fields.push('password');
    document.hasIn(['auth', 'secret']) && fields.push('secret');

    return {
      fields,
      values: {
        name: stringValue(document.get('name')),
        secure: stringValue(document.get('protocol')).toLowerCase() === 'https',
        host: stringValue(document.get('host')),
        port: numberValue(document.get('port')),
        username: stringValue(document.getIn(['auth', 'username'])),
        password: stringValue(document.getIn(['auth', 'password'])),
        secret: stringValue(document.getIn(['auth', 'secret']))
      }
    };
  }

  /**
   * Update only the simple fields already declared by a downloader YAML document.
   *
   * @param source - The downloader YAML source.
   * @param values - The edited simple field values.
   * @returns The updated YAML source with advanced fields preserved.
   */
  function writeDownloaderConfig(source: string, values: DownloaderSimpleValues): string {
    const document = parseConfig(source);
    restoreFollowingSectionComment(document, 'auth');

    document.has('name') && document.set('name', values.name.trim());
    document.has('protocol') && document.set('protocol', values.secure ? 'https' : 'http');
    document.has('host') && document.set('host', values.host.trim());
    document.has('port') && document.set('port', values.port);
    document.hasIn(['auth', 'username']) && document.setIn(['auth', 'username'], authenticationValue(values.username));
    document.hasIn(['auth', 'password']) && document.setIn(['auth', 'password'], authenticationValue(values.password));
    document.hasIn(['auth', 'secret']) && document.setIn(['auth', 'secret'], authenticationValue(values.secret));

    return document.toString({ lineWidth: 0 });
  }

  /**
   * Read a simple configuration without propagating YAML parsing errors.
   *
   * @param source - The downloader YAML source.
   * @returns The simple configuration, or `null` when it cannot be read.
   */
  function tryReadDownloaderConfig(source: string): DownloaderSimpleConfig | null {
    try {
      return readDownloaderConfig(source);
    } catch {
      return null;
    }
  }

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
  import { alert, CodeMirror, confirm, Image, Label, Modal, URLWrapper } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { yaml } from '@codemirror/lang-yaml';
  import { onMount } from 'svelte';

  let { id, preset = '', config = CONFIG_TEMPLATE, onsave }: DownloaderEditorProps = $props();

  // the simple form parsed from the initial `config`
  // svelte-ignore state_referenced_locally
  const initialSimple = tryReadDownloaderConfig(config);
  // the inferred simple-form fields and their editable values
  let simple = $state(initialSimple ?? readDownloaderConfig(CONFIG_TEMPLATE));

  // whether the advanced YAML editor is active
  // svelte-ignore state_referenced_locally
  let advanced = $state(!preset || initialSimple === null);

  // the current downloader `id` used to detect prop changes
  // svelte-ignore state_referenced_locally
  let editorId = $state(id);
  // the `CodeMirror` instance used to replace the YAML document
  let codeMirror: CodeMirror | null = $state(null);

  // the `URLWrapper` instance used to normalize the host and protocol
  let urlWrapper = $state<URLWrapper>();

  // the sorted preset keys followed by the custom downloader option
  let presetOptions = $derived(presets ? [...Object.keys(presets).sort((a, b) => a.localeCompare(b)), ''] : ['']);

  // the modal dialog instance
  let modal: Modal;
  export const showModal = () => modal.show();

  // the loading state
  const loading = createLoading();

  /**
   * Synchronize the selected preset, YAML document, simple fields, and editing mode.
   *
   * @param nextPreset - The next preset key, or an empty string for a custom downloader.
   * @param nextConfig - The YAML document to edit.
   */
  function applyConfig(nextPreset: string, nextConfig: string) {
    const nextSimple = tryReadDownloaderConfig(nextConfig);
    preset = nextPreset;
    config = nextConfig;
    codeMirror?.setDocument(nextConfig, true);
    nextSimple && (simple = nextSimple);
    advanced = !nextPreset || nextSimple === null;
  }

  /**
   * Apply a preset and select its supported editing mode.
   *
   * @param nextPreset - The next preset key, or an empty string for a custom downloader.
   */
  function applyPreset(nextPreset: string) {
    applyConfig(nextPreset, presets?.[nextPreset] || CONFIG_TEMPLATE);
  }

  /**
   * Select the first sorted preset for a new downloader.
   *
   * @param availablePresets - The available downloader presets.
   */
  function applyDefaultPreset(availablePresets: Record<string, string>) {
    if (!id && !preset && config === CONFIG_TEMPLATE) {
      const [firstPreset] = Object.keys(availablePresets).sort((a, b) => a.localeCompare(b));
      firstPreset && applyPreset(firstPreset);
    }
  }

  /**
   * Select a downloader preset after confirming that edited YAML can be replaced.
   *
   * @param nextPreset - The preset selected by the user.
   */
  function selectPreset(nextPreset: string) {
    if (nextPreset === preset) {
      return;
    }

    // check whether the selected preset configuration has unsaved changes
    if ((preset === '' && config === CONFIG_TEMPLATE) || (!!preset && config === presets?.[preset])) {
      applyPreset(nextPreset);
    } else {
      confirm({
        message: $_('message.leave.content'),
        onconfirm: () => applyPreset(nextPreset)
      });
    }
  }

  /**
   * Switch between the simple form and complete YAML editor.
   *
   * @param event - The mode switch change event.
   */
  function switchMode(event: Event) {
    const target = event.currentTarget as HTMLInputElement;
    if (target.checked) {
      advanced = true;
      return;
    }
    const nextSimple = tryReadDownloaderConfig(config);
    if (nextSimple) {
      simple = nextSimple;
      advanced = false;
      return;
    }
    target.checked = true;
    alert({ level: 'error', message: 'invalid_yaml_config' });
  }

  /**
   * Check whether the source YAML declares an editable field.
   *
   * @param field - The simple field name.
   * @returns Whether the field should be displayed.
   */
  function hasSimpleField(field: DownloaderSimpleField): boolean {
    return simple.fields.includes(field);
  }

  /**
   * Update an editable value and synchronize it to the YAML document.
   *
   * @param field - The simple value name.
   * @param value - The new field value.
   */
  function updateSimpleValue<Key extends keyof DownloaderSimpleValues>(field: Key, value: DownloaderSimpleValues[Key]) {
    simple.values[field] = value;
    config = writeDownloaderConfig(config, simple.values);
  }

  /**
   * Remove an optional HTTP prefix pasted into the host field.
   */
  function standardizeHost() {
    if (!urlWrapper) {
      return;
    }
    simple.values.host = urlWrapper.standardize(simple.values.host);
    config = writeDownloaderConfig(config, simple.values);
  }

  /**
   * Save or update the downloader.
   *
   * @param form - The form element.
   */
  function upsert(form: HTMLFormElement) {
    loading.start();
    api
      .post('download/manager/upsert', { json: { id, preset, config } })
      .json<Resp<Downloader>>()
      .then(({ data }) => {
        modal.close();
        onsave?.(data);
        setTimeout(() => form.reset(), 200);
      })
      .finally(() => {
        loading.end();
      });
  }

  $effect.pre(() => {
    if (id !== editorId) {
      editorId = id;
      applyConfig(preset, config);
    } else if (!preset) {
      advanced = true;
    }
  });

  onMount(() => {
    if (presets === null) {
      api
        .get('download/manager/presets')
        .json<Resp<Record<string, string>>>()
        .then(({ data }) => {
          presets = data;
          applyDefaultPreset(data);
        });
    } else {
      applyDefaultPreset(presets);
    }
  });
</script>

<Modal
  icon={icons.box3dDownload}
  title={$_(id ? 'action.edit' : 'action.add', $_('download.downloader.config'))}
  maxWidth="42rem"
  bind:this={modal}
>
  <form
    method="post"
    use:enhance={({ formElement, cancel }) => {
      cancel();
      upsert(formElement);
    }}
  >
    <fieldset class="fieldset">
      {#if !id}
        <Label>{$_('download.downloader.preset')}</Label>
        <div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {#each presetOptions as key (key)}
            <button
              type="button"
              class="btn h-auto min-h-20 flex-col gap-2 bg-base-100 py-3 {preset === key
                ? 'border-primary bg-primary/10'
                : ''}"
              aria-pressed={preset === key}
              onclick={() => selectPreset(key)}
            >
              <Image
                transparent
                preset={key ? key.toLowerCase() : null}
                icon={icons.box3dScanFill}
                width="2.5rem"
                height="2.5rem"
                class={key ? undefined : '[&_iconify-icon]:opacity-70!'}
              />
              <span class="font-normal">{key || $_('download.downloader.custom')}</span>
            </button>
          {/each}
        </div>
      {/if}

      {#if advanced}
        {#if !preset}
          <div class="mt-2 flex items-center gap-1 text-xs opacity-50">
            <iconify-icon icon={icons.info} width="1.125rem" class="size-4.5"></iconify-icon>
            {$_('download.downloader.custom_yaml_only')}
          </div>
        {/if}
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
      {:else}
        {#if hasSimpleField('name')}
          <Label required>{$_('field.name')}</Label>
          <input
            required
            class="input w-full"
            autocomplete="off"
            value={simple.values.name}
            oninput={(event) => updateSimpleValue('name', event.currentTarget.value)}
          />
        {/if}

        <div class="grid grid-cols-1 gap-2 sm:grid-cols-[minmax(0,2fr)_minmax(7rem,1fr)]">
          {#if hasSimpleField('host')}
            <div class="space-y-1.5">
              <Label required>{$_('download.downloader.url')}</Label>
              <URLWrapper
                bind:this={urlWrapper}
                bind:secure={simple.values.secure}
                onclick={() => updateSimpleValue('secure', simple.values.secure)}
              >
                <input
                  required
                  class="grow truncate"
                  autocomplete="url"
                  inputmode="url"
                  value={simple.values.host}
                  oninput={(event) => updateSimpleValue('host', event.currentTarget.value)}
                  onchange={standardizeHost}
                />
              </URLWrapper>
            </div>
          {/if}

          {#if hasSimpleField('port')}
            <div class="space-y-1.5">
              <Label required>{$_('field.port')}</Label>
              <input
                required
                class="input w-full"
                type="number"
                min="1"
                max="65535"
                value={simple.values.port}
                oninput={(event) => updateSimpleValue('port', event.currentTarget.valueAsNumber || 0)}
              />
            </div>
          {/if}
        </div>

        {#if hasSimpleField('username') || hasSimpleField('password')}
          <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {#if hasSimpleField('username')}
              <div class="space-y-1.5">
                <Label>{$_('field.username')}</Label>
                <input
                  class="input w-full"
                  autocomplete="username"
                  value={simple.values.username}
                  oninput={(event) => updateSimpleValue('username', event.currentTarget.value)}
                />
              </div>
            {/if}

            {#if hasSimpleField('password')}
              <div class="space-y-1.5">
                <Label>{$_('field.password')}</Label>
                <input
                  class="input w-full"
                  type="password"
                  autocomplete="current-password"
                  value={simple.values.password}
                  oninput={(event) => updateSimpleValue('password', event.currentTarget.value)}
                />
              </div>
            {/if}
          </div>
        {/if}

        {#if hasSimpleField('secret')}
          <Label>{$_('download.downloader.rpc_secret')}</Label>
          <input
            class="input w-full"
            type="password"
            autocomplete="off"
            value={simple.values.secret}
            oninput={(event) => updateSimpleValue('secret', event.currentTarget.value)}
          />
        {/if}
      {/if}
    </fieldset>
    <div class="modal-action items-center">
      <label class="label mr-auto gap-2" class:cursor-pointer={!!preset} class:opacity-60={!preset}>
        <input type="checkbox" class="toggle toggle-sm" checked={advanced} disabled={!preset} onchange={switchMode} />
        <span>{$_('download.downloader.advanced_mode')}</span>
      </label>
      <div class="flex gap-2">
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
    </div>
  </form>
</Modal>
