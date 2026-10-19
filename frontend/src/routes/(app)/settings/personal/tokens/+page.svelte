<script lang="ts">
  import { enhance } from '$app/forms';
  import { api } from '$lib/api';
  import { Button, Container, Label, Modal, Select, Setting, alert, confirm } from '$lib/components';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { MCPAccessToken, MCPAccessTokenCreate, MCPAccessTokenCreated, Resp } from '$lib/types';
  import { isHTTPError } from 'ky';
  import { onDestroy, onMount } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  const uid = $props.id();
  const loading = createLoading();
  const creating = createLoading();
  const deleting = new SvelteSet<number>();
  const schema = createFormSchema(({ text }) => ({ name: text().maxlength(64) }));
  const endpoint = `${window.location.origin}/_api/mcp`;
  const authHeader = 'Authorization: Bearer <token>';

  let tokens: MCPAccessToken[] = $state([]);
  let initialized = $state(false);
  let loadError = $state(false);
  let name = $state('');
  let expiresInDays: MCPAccessTokenCreate['expires_in_days'] = $state(90);
  let secret = $state('');
  let modal: Modal;
  let secretInput = $state<HTMLTextAreaElement>();
  let endpointInput: HTMLInputElement;
  let authHeaderInput: HTMLInputElement;
  let createRequest: AbortController | null = null;
  const requests = new AbortController();

  /**
   * Load the current user's token metadata without retrieving credentials.
   *
   * @returns A promise that resolves after loading or showing a retry state.
   */
  async function load(): Promise<void> {
    if ($loading !== null || deleting.size > 0) return;
    loading.start();
    loadError = false;
    try {
      const { data } = await api.get('mcp/token/list', { signal: requests.signal }).json<Resp<MCPAccessToken[]>>();
      tokens = data;
    } catch (cause) {
      if (requests.signal.aborted) return;
      loadError = true;
      if (!isHTTPError(cause)) alert({ level: 'error', message: $_('access_token.load_failed') });
    } finally {
      initialized = true;
      loading.end();
    }
  }

  /** Clear the one-time credential and cancel an unfinished creation request. */
  function reset() {
    secret = '';
    createRequest?.abort();
    createRequest = null;
    creating.end();
  }

  /** Open a new token form with the default lifetime. */
  function showCreator() {
    reset();
    name = '';
    expiresInDays = 90;
    modal.show();
  }

  /**
   * Create a token and keep its full credential only in the open dialog.
   *
   * @returns A promise that resolves after creation or an error alert.
   */
  async function create(): Promise<void> {
    if ($creating !== null || !name.trim()) return;
    const controller = new AbortController();
    createRequest = controller;
    creating.start();
    try {
      const payload: MCPAccessTokenCreate = { name: name.trim(), expires_in_days: expiresInDays };
      const { data } = await api
        .post('mcp/token/create', { json: payload, signal: controller.signal, retry: 0 })
        .json<Resp<MCPAccessTokenCreated>>();
      if (createRequest !== controller || !modal.isOpen()) return;
      const { token, ...metadata } = data;
      tokens = [metadata, ...tokens];
      secret = token;
    } catch (cause) {
      if (!controller.signal.aborted && !isHTTPError(cause)) {
        alert({ level: 'error', message: $_('access_token.create_failed') });
      }
    } finally {
      if (createRequest === controller) {
        createRequest = null;
        creating.end();
      }
    }
  }

  /**
   * Delete a confirmed token and remove its metadata from the list.
   *
   * @param token - Token whose credential should stop working.
   * @returns A promise that resolves after the deletion attempt.
   */
  async function remove(token: MCPAccessToken): Promise<void> {
    if ($loading !== null || deleting.has(token.id)) return;
    deleting.add(token.id);
    try {
      await api.post(`mcp/token/${token.id}/delete`, { signal: requests.signal });
      tokens = tokens.filter((item) => item.id !== token.id);
    } catch (cause) {
      if (!requests.signal.aborted && !isHTTPError(cause)) {
        alert({ level: 'error', message: $_('access_token.delete_failed') });
      }
    } finally {
      deleting.delete(token.id);
    }
  }

  /**
   * Copy a displayed value or select it for manual copying when clipboard access fails.
   *
   * @param input - Read-only field containing the value to copy.
   * @returns A promise that resolves after copying or showing a fallback hint.
   */
  async function copy(input: HTMLInputElement | HTMLTextAreaElement | undefined): Promise<void> {
    if (!input) return;
    try {
      await navigator.clipboard.writeText(input.value);
      alert({ level: 'success', message: $_('access_token.copied') });
    } catch {
      input.focus();
      input.select();
      alert({ level: 'warning', message: $_('access_token.copy_failed') });
    }
  }

  onMount(() => {
    load();
  });
  onDestroy(() => {
    requests.abort();
    reset();
  });
</script>

<Container type="settings" minWidth="0" maxWidth="56rem" rowGap="2rem" loading={initialized ? null : $loading}>
  <Setting title={$_('nav.settings.personal.tokens')}>
    <div class="flex flex-wrap items-center justify-between gap-3">
      <p class="text-sm text-base-content/70">{$_('access_token.description')}</p>
      <div class="flex shrink-0 items-center gap-2">
        <Button
          icon={icons.redo}
          text={$_('action.reload')}
          loading={$loading}
          disabled={deleting.size > 0}
          onclick={load}
        />
        <button class="btn btn-sm" onclick={showCreator}>
          <iconify-icon icon={icons.addCircle} width="1.25rem" aria-hidden="true"></iconify-icon>
          {$_('access_token.create')}
        </button>
      </div>
    </div>
    {#if loadError}
      <div class="flex items-center justify-between gap-3 py-4">
        <p class="text-sm text-base-content/70" role="status">{$_('access_token.load_failed')}</p>
        <button class="btn btn-sm" onclick={load}>{$_('action.retry')}</button>
      </div>
    {:else if tokens.length === 0}
      <p class="py-8 text-center text-sm text-base-content/70">{$_('access_token.empty')}</p>
    {:else}
      <ul class="divide-y divide-border rounded-field border bg-base-100">
        {#each tokens as token (token.id)}
          <li class="p-4">
            <div class="flex items-center gap-3">
              <iconify-icon icon={icons.key} width="1.5rem" class="shrink-0" aria-hidden="true"></iconify-icon>
              <div class="min-w-0 flex-1">
                <h3 class="truncate font-medium" title={token.name}>{token.name}</h3>
                <p class="mt-1 font-mono text-xs text-base-content/70">{token.prefix}…</p>
              </div>
              <Button
                icon={icons.delete}
                text={$_('access_token.delete')}
                disabled={$loading !== null || deleting.has(token.id)}
                loading={deleting.has(token.id)}
                onclick={() =>
                  confirm({
                    icon: icons.delete,
                    title: $_('access_token.delete'),
                    message: $_('access_token.delete_confirm', token.name),
                    onconfirm: () => {
                      remove(token);
                    }
                  })}
              />
            </div>
            <div class="mt-3 flex flex-wrap items-center gap-2 text-xs text-base-content/70">
              <span class="inline-flex items-center gap-1.5 rounded-field bg-base-200 px-2 py-1">
                <span
                  class="size-1.5 rounded-full {token.status === 'active' ? 'bg-success' : 'bg-base-content/40'}"
                  aria-hidden="true"
                ></span>
                {$_(`access_token.status.${token.status}`)}
              </span>
              <span class="rounded-field bg-base-200 px-2 py-1">{$_('access_token.read_only')}</span>
            </div>
            <dl class="mt-3 grid gap-3 text-xs sm:grid-cols-3">
              <div>
                <dt class="text-base-content/70">{$_('access_token.created_at')}</dt>
                <dd class="mt-1">{$dateTime(token.created_at)}</dd>
              </div>
              <div>
                <dt class="text-base-content/70">{$_('access_token.expires_at')}</dt>
                <dd class="mt-1">{$dateTime(token.expires_at)}</dd>
              </div>
              <div>
                <dt class="text-base-content/70">{$_('access_token.last_used_at')}</dt>
                <dd class="mt-1">{token.last_used_at ? $dateTime(token.last_used_at) : $_('access_token.unused')}</dd>
              </div>
            </dl>
          </li>
        {/each}
      </ul>
    {/if}
  </Setting>

  <Setting title={$_('access_token.connection')}>
    <p class="text-sm text-base-content/70">{$_('access_token.connection_hint')}</p>
    <dl class="flex flex-wrap gap-x-6 gap-y-2 text-sm">
      <div class="flex items-baseline gap-2">
        <dt class="text-base-content/70">{$_('access_token.transport')}</dt>
        <dd>Streamable HTTP</dd>
      </div>
      <div class="flex items-baseline gap-2">
        <dt class="text-base-content/70">{$_('access_token.authentication')}</dt>
        <dd>Bearer Token</dd>
      </div>
    </dl>
    <fieldset class="fieldset">
      <Label preventDefault={false}><label for="{uid}-endpoint">{$_('access_token.endpoint')}</label></Label>
      <div class="input w-full gap-2 bg-base-200/40 pr-1">
        <input
          id="{uid}-endpoint"
          class="min-w-0 flex-1 font-mono text-sm"
          readonly
          value={endpoint}
          bind:this={endpointInput}
        />
        <Button
          class="shrink-0"
          icon={icons.documentCopy}
          text={$_('action.copy', $_('access_token.endpoint'))}
          onclick={() => copy(endpointInput)}
        />
      </div>
      <Label preventDefault={false}><label for="{uid}-auth-header">{$_('access_token.auth_header')}</label></Label>
      <div class="input w-full gap-2 bg-base-200/40 pr-1">
        <input
          id="{uid}-auth-header"
          class="min-w-0 flex-1 font-mono text-sm"
          spellcheck="false"
          readonly
          value={authHeader}
          bind:this={authHeaderInput}
        />
        <Button
          class="shrink-0"
          icon={icons.documentCopy}
          text={$_('action.copy', $_('access_token.auth_header'))}
          onclick={() => copy(authHeaderInput)}
        />
      </div>
      <p class="px-1 text-xs leading-relaxed text-base-content/70">{$_('access_token.auth_hint')}</p>
    </fieldset>
  </Setting>
</Container>

<Modal
  bind:this={modal}
  icon={icons.key}
  title={$_(secret ? 'access_token.created' : 'access_token.create')}
  maxWidth="34rem"
  onclose={reset}
>
  {#if secret}
    <p class="text-sm text-base-content/70">{$_('access_token.once_hint')}</p>
    <fieldset class="fieldset">
      <Label preventDefault={false}><label for="{uid}-secret">{$_('nav.settings.personal.tokens')}</label></Label>
      <textarea
        id="{uid}-secret"
        class="textarea w-full font-mono text-sm break-all"
        rows="3"
        spellcheck="false"
        readonly
        value={secret}
        bind:this={secretInput}></textarea>
    </fieldset>
    <div class="modal-action">
      <button type="button" class="btn" onclick={() => modal.close()}>{$_('action.close')}</button>
      <button type="button" class="btn btn-submit" onclick={() => copy(secretInput)}>{$_('action.copy')}</button>
    </div>
  {:else}
    <form
      method="post"
      use:enhance={({ cancel }) => {
        cancel();
        create();
      }}
    >
      <fieldset class="fieldset">
        <Label required preventDefault={false}><label for="{uid}-name">{$_('field.name')}</label></Label>
        <input id="{uid}-name" class="input w-full" {...schema.name} bind:value={name} />
        <Label required preventDefault={false}><label for="{uid}-expiry">{$_('access_token.lifetime')}</label></Label>
        <Select
          id="{uid}-expiry"
          name="expires_in_days"
          required
          options={[30, 90, 365].map((days) => ({
            value: days,
            label: `${days} ${$_('duration.days').toLowerCase()}`
          }))}
          bind:value={expiresInDays}
          class="w-full"
        />
        <p class="mt-2 text-sm text-base-content/70">{$_('access_token.description')}</p>
      </fieldset>
      <div class="modal-action">
        <button type="button" class="btn" onclick={() => modal.close()}>{$_('message.cancel')}</button>
        <button type="submit" class="btn btn-submit" disabled={$creating !== null || !name.trim()}>
          {$_('message.confirm')}
          {#if $creating}<span class="loading loading-xs loading-dots"></span>{/if}
        </button>
      </div>
    </form>
  {/if}
</Modal>
