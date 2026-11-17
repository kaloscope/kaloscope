<script lang="ts">
  import { api } from '$lib/api';
  import { Button, Grid, Image, confirm } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _, dateTime, duration } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { Resp, User } from '$lib/types';
  import { onMount } from 'svelte';
  import { UAParser } from 'ua-parser-js';

  // the user sessions
  let sessions: User[] = $state([]);
  // the loading state
  const loading = createLoading();

  /**
   * Get current user sessions.
   */
  function getSessions() {
    loading.start();
    api
      .get('auth/online')
      .json<Resp<User[]>>()
      .then(({ data }) => (sessions = data))
      .finally(() => loading.end());
  }

  /**
   * Kill the sessions.
   *
   * @param ids - Session IDs.
   */
  function killSessions(ids: string[]) {
    loading.start();
    api
      .post('auth/kickout', { json: { ids } })
      .then(() => getSessions())
      .catch(() => loading.end());
  }

  onMount(() => {
    getSessions();
  });
</script>

{#snippet term(name: string, description: string)}
  <div class="flex items-center justify-between gap-4 py-2" title={description}>
    <dt class="whitespace-nowrap">{name}:</dt>
    <dd class="truncate opacity-70">{description}</dd>
  </div>
{/snippet}

<Grid
  data={sessions}
  loading={$loading}
  uniqueKey="login_id"
  class="pull-to-refresh"
  gridClass="lg:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4"
  itemClass="card-surface"
  skeleton={{ ratio: '4/1', lines: 3 }}
>
  {#snippet item(session, index)}
    {@const { browser, device, os } = UAParser(session.user_agent)}
    <div class="flex justify-between gap-2 rounded-t-card bg-base-200/60 p-4">
      <div class="grid grid-flow-col items-center gap-2" title={session.user_agent}>
        <Image
          transparent
          preset={browser.name?.toLowerCase()}
          icon={device.type === 'mobile' ? icons.phone : device.type === 'tablet' ? icons.tablet : icons.desktop}
          width="2rem"
          class="[&_iconify-icon]:opacity-70!"
        />
        <div class="flex items-baseline gap-1 overflow-hidden">
          <span class="text-base {browser.version ? '' : 'truncate'}">
            {browser.name ?? session.user_agent}
          </span>
          {#if browser.version}
            <span class="truncate italic-text">{browser.version}</span>
          {/if}
        </div>
      </div>
      <div class="flex items-center gap-1">
        {#if index === 0}
          <Button icon={icons.redo} onclick={getSessions} />
          <Button
            icon={icons.clear}
            disabled={sessions.length <= 1}
            onclick={() => {
              confirm({
                icon: icons.clear,
                message: $_('session.kill.others'),
                onconfirm: () => killSessions(sessions.slice(1).map((s) => s.login_id))
              });
            }}
          />
        {:else}
          <Button
            icon={icons.logout}
            onclick={() => {
              confirm({
                icon: icons.logout,
                message: $_('session.kill.one'),
                onconfirm: () => killSessions([session.login_id])
              });
            }}
          />
        {/if}
      </div>
    </div>
    <dl class="rounded-b-card bg-base-100 p-4 text-sm">
      {@render term($_('session.os'), os.name ? `${os.name} ${os.version ?? ''}` : 'Unknown')}
      <div class="divider my-0"></div>
      {@render term($_('session.ip'), session.client_ip)}
      <div class="divider my-0"></div>
      {@render term($_('session.duration'), $duration(session.login_at))}
      <div class="divider my-0"></div>
      {@render term($_('session.activity'), $dateTime(session.last_activity))}
    </dl>
    {#if index === 0}
      <div class="absolute -top-2 -right-2 z-1">
        <iconify-icon icon={icons.pinFill} width="1.5rem"></iconify-icon>
      </div>
    {/if}
  {/snippet}
</Grid>
