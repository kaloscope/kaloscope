<script lang="ts" module>
  import type { Downloader, DownloaderDriver, Resp } from '$lib/types';
  import { isMap, isPair, isScalar, parseDocument, type Document, type ParsedNode } from 'yaml';

  /** The editable component properties. */
  type DownloaderEditorProps = Partial<{
    id: number;
    preset: string;
    config: string;
    onsave: (result: Downloader) => void;
  }>;

  /** A YAML field supported by the simple downloader form. */
  type DownloaderSimpleField =
    | 'name'
    | 'host'
    | 'port'
    | 'username'
    | 'password'
    | 'secret'
    | 'token'
    | 'tool'
    | 'remote_root'
    | 'remote_cleanup'
    | 'poll_interval'
    | 'poll_max_interval'
    | 'pull_concurrency';

  /** The editable values shared by RPC and OpenList forms. */
  type DownloaderSimpleValues = {
    driver: DownloaderDriver;
    name: string;
    token: string;
    tool: string;
    remote_root: string;
    remote_cleanup: string;
    poll_interval: number;
    poll_max_interval: number;
    pull_concurrency: number;
    secure: boolean;
    host: string;
    port: number;
    path: string;
    username: string;
    password: string;
    secret: string;
  };

  /** The inferred simple fields and their current values. */
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
    const endpoint = {
      secure: stringValue(document.get('protocol')).toLowerCase() === 'https',
      host: stringValue(document.get('host')),
      port: numberValue(document.get('port')),
      path: stringValue(document.get('path'))
    };

    if (document.get('driver') === 'openlist') {
      document.hasIn(['auth', 'token']) && fields.push('token');
      document.has('tool') && fields.push('tool');
      document.has('remote_root') && fields.push('remote_root');
      document.has('remote_cleanup') && fields.push('remote_cleanup');
      document.has('poll_interval') && fields.push('poll_interval');
      document.has('poll_max_interval') && fields.push('poll_max_interval');
      document.has('pull_concurrency') && fields.push('pull_concurrency');

      return {
        fields,
        values: {
          driver: 'openlist',
          name: stringValue(document.get('name')),
          token: stringValue(document.getIn(['auth', 'token'])),
          tool: stringValue(document.get('tool')),
          remote_root: stringValue(document.get('remote_root')),
          remote_cleanup: stringValue(document.get('remote_cleanup')),
          poll_interval: numberValue(document.get('poll_interval')),
          poll_max_interval: numberValue(document.get('poll_max_interval')),
          pull_concurrency: numberValue(document.get('pull_concurrency')),
          ...endpoint,
          username: '',
          password: '',
          secret: ''
        }
      };
    }

    document.hasIn(['auth', 'username']) && fields.push('username');
    document.hasIn(['auth', 'password']) && fields.push('password');
    document.hasIn(['auth', 'secret']) && fields.push('secret');

    return {
      fields,
      values: {
        driver: 'rpc',
        name: stringValue(document.get('name')),
        token: '',
        tool: '',
        remote_root: '',
        remote_cleanup: 'keep',
        poll_interval: 0,
        poll_max_interval: 0,
        pull_concurrency: 0,
        ...endpoint,
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
    document.has('path') && document.set('path', values.path.trim());

    if (values.driver === 'openlist') {
      document.hasIn(['auth', 'token']) && document.setIn(['auth', 'token'], authenticationValue(values.token));
      document.has('tool') && document.set('tool', values.tool.trim());
      document.has('remote_root') && document.set('remote_root', values.remote_root.trim());
      document.has('remote_cleanup') && document.set('remote_cleanup', values.remote_cleanup);
      document.has('poll_interval') && document.set('poll_interval', values.poll_interval);
      document.has('poll_max_interval') && document.set('poll_max_interval', values.poll_max_interval);
      document.has('pull_concurrency') && document.set('pull_concurrency', values.pull_concurrency);
      return document.toString({ lineWidth: 0 });
    }

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
  import { alert, CodeMirror, confirm, Image, Label, Modal, Select, URLWrapper } from '$lib/components';
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
  let presetOptions = $derived.by(() => {
    if (!presets) return [''];
    const keys = Object.keys(presets).sort((left, right) => {
      if (left === right) return 0;
      if (left === 'OpenList') return -1;
      if (right === 'OpenList') return 1;
      return left.localeCompare(right);
    });
    return [...keys, ''];
  });

  // the tools exposed by the current `OpenList` endpoint; `null` means not loaded
  let openlistTools: string[] | null = $state(null);
  // the selectable tools, preserving an existing value until the list is loaded
  let openlistToolOptions = $derived.by(() => {
    const tools = openlistTools ?? (simple.values.tool ? [simple.values.tool] : []);
    return [
      {
        value: '',
        label: $_('action.select', $_('download.downloader.openlist.tool')),
        disabled: true
      },
      ...tools.map((tool) => ({ value: tool, label: tool }))
    ];
  });

  // the modal dialog instance
  let modal: Modal;
  export const showModal = () => modal.show();

  // the loading state
  const loading = createLoading();
  const toolsLoading = createLoading();

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
    openlistTools = null;
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
   * Update an endpoint value and discard tools loaded from the previous endpoint.
   *
   * @param field - The endpoint value name.
   * @param value - The new endpoint value.
   */
  function updateEndpointValue<Key extends 'secure' | 'host' | 'port'>(field: Key, value: DownloaderSimpleValues[Key]) {
    openlistTools = null;
    updateSimpleValue(field, value);
  }

  /**
   * Remove an optional HTTP prefix pasted into the host field.
   */
  function standardizeHost() {
    if (!urlWrapper) {
      return;
    }
    openlistTools = null;
    simple.values.host = urlWrapper.standardize(simple.values.host);
    config = writeDownloaderConfig(config, simple.values);
  }

  /**
   * Load the offline download tools exposed by the configured OpenList endpoint.
   *
   * @returns A promise that resolves after the tool list request finishes.
   */
  async function loadOpenListTools() {
    const endpoint = {
      protocol: simple.values.secure ? 'https' : 'http',
      host: simple.values.host.trim(),
      port: simple.values.port,
      path: simple.values.path.trim()
    };
    if (!endpoint.host || endpoint.port < 1) return;
    toolsLoading.start();
    try {
      const { data } = await api
        .post('download/manager/openlist/tools', {
          json: endpoint,
          searchParams: { path: simple.values.remote_root.trim() }
        })
        .json<Resp<string[]>>();
      openlistTools = data;
      if (simple.values.tool && !data.includes(simple.values.tool)) {
        updateSimpleValue('tool', '');
      }
    } catch {
      openlistTools = null;
    } finally {
      toolsLoading.end();
    }
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
                onclick={() => updateEndpointValue('secure', simple.values.secure)}
              >
                <input
                  required
                  class="grow truncate"
                  autocomplete="url"
                  inputmode="url"
                  value={simple.values.host}
                  oninput={(event) => updateEndpointValue('host', event.currentTarget.value)}
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
                oninput={(event) => updateEndpointValue('port', event.currentTarget.valueAsNumber || 0)}
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

        {#if hasSimpleField('token')}
          <Label required>{$_('download.downloader.openlist.token')}</Label>
          <input
            required
            class="input w-full"
            type="password"
            autocomplete="off"
            value={simple.values.token}
            oninput={(event) => updateSimpleValue('token', event.currentTarget.value)}
          />
        {/if}

        {#if hasSimpleField('tool')}
          <Label required>{$_('download.downloader.openlist.tool')}</Label>
          <div class="flex gap-2">
            <Select
              required
              options={openlistToolOptions}
              bind:value={simple.values.tool}
              onchange={() => updateSimpleValue('tool', simple.values.tool)}
              class="min-w-0 grow"
            />
            <button
              type="button"
              class="btn shrink-0"
              disabled={$toolsLoading !== null || !simple.values.host.trim() || simple.values.port < 1}
              onclick={loadOpenListTools}
            >
              {$_('download.downloader.openlist.load_tools')}
              {#if $toolsLoading}<span class="loading loading-xs loading-dots"></span>{/if}
            </button>
          </div>
        {/if}

        {#if hasSimpleField('remote_root') || hasSimpleField('remote_cleanup')}
          <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {#if hasSimpleField('remote_root')}
              <div class="space-y-1.5">
                <Label required>{$_('download.downloader.openlist.remote_root')}</Label>
                <input
                  required
                  class="input w-full"
                  value={simple.values.remote_root}
                  oninput={(event) => updateSimpleValue('remote_root', event.currentTarget.value)}
                />
              </div>
            {/if}
            {#if hasSimpleField('remote_cleanup')}
              <div class="space-y-1.5">
                <Label>{$_('download.downloader.openlist.remote_cleanup')}</Label>
                <Select
                  options={[
                    { value: 'keep', label: $_('download.downloader.openlist.keep') },
                    { value: 'delete_on_success', label: $_('download.downloader.openlist.delete_on_success') }
                  ]}
                  bind:value={simple.values.remote_cleanup}
                  onchange={() => updateSimpleValue('remote_cleanup', simple.values.remote_cleanup)}
                  class="w-full"
                />
              </div>
            {/if}
          </div>
        {/if}

        {#if hasSimpleField('poll_interval') || hasSimpleField('poll_max_interval') || hasSimpleField('pull_concurrency')}
          <div class="flex flex-wrap items-end gap-2">
            {#if hasSimpleField('poll_interval')}
              <div class="min-w-[min(100%,12.5rem)] flex-[1_1_12.5rem] space-y-1.5">
                <Label required>{$_('download.downloader.openlist.poll_interval')}</Label>
                <input
                  required
                  class="input w-full"
                  type="number"
                  min="5"
                  value={simple.values.poll_interval}
                  oninput={(event) => updateSimpleValue('poll_interval', event.currentTarget.valueAsNumber || 0)}
                />
              </div>
            {/if}
            {#if hasSimpleField('poll_max_interval')}
              <div class="min-w-[min(100%,12.5rem)] flex-[1_1_12.5rem] space-y-1.5">
                <Label required>{$_('download.downloader.openlist.poll_max_interval')}</Label>
                <input
                  required
                  class="input w-full"
                  type="number"
                  min={Math.max(5, simple.values.poll_interval)}
                  value={simple.values.poll_max_interval}
                  oninput={(event) => updateSimpleValue('poll_max_interval', event.currentTarget.valueAsNumber || 0)}
                />
              </div>
            {/if}
            {#if hasSimpleField('pull_concurrency')}
              <div class="min-w-[min(100%,12.5rem)] flex-[1_1_12.5rem] space-y-1.5">
                <Label required>{$_('download.downloader.openlist.pull_concurrency')}</Label>
                <input
                  required
                  class="input w-full"
                  type="number"
                  min="1"
                  value={simple.values.pull_concurrency}
                  oninput={(event) => updateSimpleValue('pull_concurrency', event.currentTarget.valueAsNumber || 0)}
                />
              </div>
            {/if}
          </div>
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
