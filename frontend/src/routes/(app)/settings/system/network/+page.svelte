<script lang="ts">
  import { api } from '$lib/api';
  import {
    Button,
    Cell,
    confirm,
    DataView,
    DNSResolverEditor,
    HCell,
    Modal,
    ProxyServerEditor,
    Search,
    URLRuleEditor
  } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { DNSResolver, HTTPProxy, Resp, URLRule } from '$lib/types';
  import { debounce } from '$lib/utils';
  import { onMount, tick } from 'svelte';

  // URL rule states
  let rules: URLRule[] = $state([]);
  let pattern: string = $state('');
  let creator: URLRuleEditor | null = $state(null);
  let updater: URLRuleEditor | null = $state(null);
  let selected: URLRule | null = $state(null);
  const loading = createLoading();

  /**
   * Search for URL rules.
   */
  function search() {
    loading.start();
    api
      .get('network/rule/list', { searchParams: { pattern } })
      .json<Resp<URLRule[]>>()
      .then(({ data }) => (rules = data))
      .finally(() => loading.end());
  }

  /**
   * Delete a URL rule by ID.
   *
   * @param id - The URL rule ID.
   */
  function del(id: number) {
    loading.start();
    api
      .post('network/rule/delete', { json: { ids: [id] } })
      .then(() => search())
      .catch(() => loading.end());
  }

  /**
   * Toggle a boolean field of a URL rule.
   *
   * @param rule - The URL rule.
   * @param field - The field to toggle.
   */
  function toggle(rule: URLRule, field: 'secure_dns' | 'http_proxy') {
    const value = !rule[field];
    rule[field] = value;
    api.post('network/rule/toggle', {
      json: { id: rule.id, [field]: value }
    });
  }

  /**
   * Sort the URL rules.
   */
  const sort = debounce(() => {
    const ids = rules.map((rule) => rule.id);
    api.post('network/rule/sort', { json: { ids } });
  });

  /**
   * Move a rule up in the list.
   *
   * @param index - The index of the rule to move up.
   */
  function moveUp(index: number) {
    if (index <= 0) {
      return;
    }
    const temp = rules[index];
    rules[index] = rules[index - 1];
    rules[index - 1] = temp;
    sort();
  }

  /**
   * Move a rule down in the list.
   *
   * @param index - The index of the rule to move down.
   */
  function moveDown(index: number) {
    if (index >= rules.length - 1) {
      return;
    }
    const temp = rules[index];
    rules[index] = rules[index + 1];
    rules[index + 1] = temp;
    sort();
  }

  // DNS resolver states
  let resolvers: DNSResolver[] = $state([]);
  let resolverModal: Modal;
  let resolverCreator: DNSResolverEditor | null = $state(null);
  let resolverUpdater: DNSResolverEditor | null = $state(null);
  let selectedResolver: DNSResolver | null = $state(null);

  /**
   * Get the DNS resolvers.
   */
  function getDNSResolvers() {
    api
      .get('network/dns/list')
      .json<Resp<DNSResolver[]>>()
      .then(({ data }) => (resolvers = data));
  }

  /**
   * Delete a DNS resolver by ID.
   *
   * @param id - The DNS resolver ID.
   */
  function delDNSResolver(id: number) {
    api
      .post('network/dns/delete', {
        json: { ids: [id] }
      })
      .then(() => getDNSResolvers());
  }

  // HTTP proxy states
  let proxies: HTTPProxy[] = $state([]);
  let proxyModal: Modal;
  let proxyCreator: ProxyServerEditor | null = $state(null);
  let proxyUpdater: ProxyServerEditor | null = $state(null);
  let selectedProxy: HTTPProxy | null = $state(null);

  /**
   * Get the HTTP proxy servers.
   */
  function getProxyServers() {
    api
      .get('network/proxy/list')
      .json<Resp<HTTPProxy[]>>()
      .then(({ data }) => (proxies = data));
  }

  /**
   * Delete an HTTP proxy server by ID.
   *
   * @param id - The HTTP proxy server ID.
   */
  function delProxyServer(id: number) {
    api
      .post('network/proxy/delete', {
        json: { ids: [id] }
      })
      .then(() => getProxyServers());
  }

  onMount(() => {
    search();
    getDNSResolvers();
    getProxyServers();
  });
</script>

<DataView dvh loading={$loading} data={rules}>
  {#snippet filters()}
    <Search label={$_('field.pattern')} bind:value={pattern} onsearch={() => search()} />
  {/snippet}
  {#snippet actions()}
    <Button size="md" icon={icons.bookGlobe} text={$_('entity.dns_resolvers')} onclick={() => resolverModal.show()} />
    <Button size="md" icon={icons.serverLink} text={$_('entity.proxy_servers')} onclick={() => proxyModal.show()} />
    <Button
      size="md"
      icon={icons.addCircle}
      text={$_('action.add', $_('entity.rule'))}
      onclick={() => creator?.showModal()}
    />
  {/snippet}
  {#snippet header()}
    <HCell width={['30%', '48%']} text={$_('field.pattern')} />
    <HCell width={['15%', '26%']} text={$_('field.secure_dns')} />
    <HCell width={['15%', '26%']} text={$_('field.http_proxy')} />
    <HCell width={['20%', null]} text={$_('field.created')} />
    <HCell width={['20%', null]} text={$_('field.updated')} />
    <HCell actions />
  {/snippet}
  {#snippet row(rule, index)}
    <Cell text={rule.pattern} />
    <Cell>
      <input
        type="checkbox"
        class="toggle toggle-sm"
        checked={rule.secure_dns && rule.resolver_ids?.length > 0}
        onchange={() => toggle(rule, 'secure_dns')}
        disabled={rule.resolver_ids?.length === 0}
      />
    </Cell>
    <Cell>
      <input
        type="checkbox"
        class="toggle toggle-sm"
        checked={rule.http_proxy && rule.proxy_id !== null}
        onchange={() => toggle(rule, 'http_proxy')}
        disabled={rule.proxy_id === null}
      />
    </Cell>
    <Cell text={$dateTime(rule.created_at)} />
    <Cell text={$dateTime(rule.updated_at)} />
    <Cell
      actions={[
        {
          icon: icons.edit,
          text: $_('action.edit', $_('entity.rule')),
          onclick: () => {
            selected = rule;
            tick().then(() => updater?.showModal());
          }
        },
        {
          icon: icons.arrowUp,
          text: $_('action.move_up'),
          disabled: !!pattern || index === 0,
          onclick: () => moveUp(index)
        },
        {
          icon: icons.arrowDown,
          text: $_('action.move_down'),
          disabled: !!pattern || index === rules.length - 1,
          onclick: () => moveDown(index)
        },
        {
          icon: icons.deleteDismiss,
          text: $_('action.delete', $_('entity.rule')),
          onclick: () => {
            confirm({
              icon: icons.deleteDismiss,
              title: `${$_('action.delete', $_('entity.rule'))} [${rule.pattern}]`,
              onconfirm: () => del(rule.id)
            });
          }
        }
      ]}
    />
  {/snippet}
</DataView>

<URLRuleEditor bind:this={creator} {resolvers} {proxies} onsave={() => search()} />

{#if selected}
  <URLRuleEditor bind:this={updater} {...selected} {resolvers} {proxies} onsave={() => search()} />
{/if}

<Modal icon={icons.bookGlobe} title={$_('entity.dns_resolvers')} maxWidth="34rem" bind:this={resolverModal}>
  <div class="flex max-h-96 min-h-18 flex-col gap-2 overflow-y-auto p-1">
    {#each resolvers as resolver (resolver.id)}
      <div class="my-auto flex items-center gap-2 rounded-selector bg-base-200 p-2">
        <span class="grow truncate text-sm">{resolver.name}</span>
        <span class="divider mx-0 divider-horizontal h-6 w-0 self-center"></span>
        <Button
          size="xs"
          icon={icons.edit}
          onclick={() => {
            selectedResolver = resolver;
            tick().then(() => resolverUpdater?.showModal());
          }}
        />
        <Button
          size="xs"
          icon={icons.deleteDismiss}
          onclick={() => {
            confirm({
              icon: icons.deleteDismiss,
              title: `${$_('action.delete', $_('entity.dns_resolver'))} [${resolver.name}]`,
              onconfirm: () => delDNSResolver(resolver.id)
            });
          }}
        />
      </div>
    {/each}
    {#if resolvers.length === 0}
      <div class="m-auto text-sm opacity-50">{$_('data.nodata')}</div>
    {/if}
  </div>
  <div class="flex justify-end border-t pt-4">
    <Button
      icon={icons.addCircle}
      text={$_('action.add', $_('entity.dns_resolver'))}
      square={false}
      class="btn-soft font-normal"
      onclick={() => resolverCreator?.showModal()}
    />
  </div>
</Modal>

<DNSResolverEditor bind:this={resolverCreator} onsave={() => getDNSResolvers()} />

{#if selectedResolver}
  <DNSResolverEditor bind:this={resolverUpdater} {...selectedResolver} onsave={() => getDNSResolvers()} />
{/if}

<Modal icon={icons.serverLink} title={$_('entity.proxy_servers')} maxWidth="34rem" bind:this={proxyModal}>
  <div class="flex max-h-96 min-h-18 flex-col gap-2 overflow-y-auto p-1">
    {#each proxies as proxy (proxy.id)}
      <div class="my-auto flex items-center gap-2 rounded-selector bg-base-200 p-2">
        <span class="grow truncate text-sm">{proxy.name}</span>
        <span class="divider mx-0 divider-horizontal h-6 w-0 self-center"></span>
        <Button
          size="xs"
          icon={icons.edit}
          onclick={() => {
            selectedProxy = proxy;
            tick().then(() => proxyUpdater?.showModal());
          }}
        />
        <Button
          size="xs"
          icon={icons.deleteDismiss}
          onclick={() => {
            confirm({
              icon: icons.deleteDismiss,
              title: `${$_('action.delete', $_('entity.proxy_server'))} [${proxy.name}]`,
              onconfirm: () => delProxyServer(proxy.id)
            });
          }}
        />
      </div>
    {/each}
    {#if proxies.length === 0}
      <div class="m-auto text-sm opacity-50">{$_('data.nodata')}</div>
    {/if}
  </div>
  <div class="flex justify-end border-t pt-4">
    <Button
      icon={icons.addCircle}
      text={$_('action.add', $_('entity.proxy_server'))}
      square={false}
      class="btn-soft font-normal"
      onclick={() => proxyCreator?.showModal()}
    />
  </div>
</Modal>

<ProxyServerEditor bind:this={proxyCreator} onsave={() => getProxyServers()} />

{#if selectedProxy}
  <ProxyServerEditor bind:this={proxyUpdater} {...selectedProxy} onsave={() => getProxyServers()} />
{/if}
