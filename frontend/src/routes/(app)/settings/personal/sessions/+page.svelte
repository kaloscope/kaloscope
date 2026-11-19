<script lang="ts">
  import { api } from '$lib/api';
  import { Button, EntityCard, Grid, Image, confirm } from '$lib/components';
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

<Grid
  data={sessions}
  loading={$loading}
  uniqueKey="login_id"
  class="pull-to-refresh"
  gridClass="grid-cols-cards"
  itemClass="card-surface"
  skeleton={{ ratio: '3/1', lines: 3 }}
>
  {#snippet item(session, index)}
    {@const { browser, device, os } = UAParser(session.user_agent)}
    <EntityCard
      title={browser.name ? `${browser.name} ${browser.major ?? ''}`.trim() : session.user_agent}
      subtitle={[os.name ? `${os.name} ${os.version ?? ''}`.trim() : null, session.client_ip]
        .filter(Boolean)
        .join(' · ')}
      tip={session.user_agent}
    >
      {#snippet media()}
        <Image
          transparent
          preset={browser.name?.toLowerCase()}
          icon={device.type === 'mobile' ? icons.phone : device.type === 'tablet' ? icons.tablet : icons.desktop}
          width="1.5rem"
          class="[&_iconify-icon]:opacity-100!"
        />
      {/snippet}
      {#snippet actions()}
        {#if index === 0}
          <span class="rounded-field bg-primary/10 px-2 py-1 text-xs font-medium text-primary">
            {$_('session.current')}
          </span>
        {/if}
      {/snippet}
      <dl class="grid grid-cols-2 gap-3 text-sm">
        <div class="min-w-0">
          <dt class="text-xs text-base-content/60">{$_('session.duration')}</dt>
          <dd class="mt-0.5 truncate">{$duration(session.login_at)}</dd>
        </div>
        <div class="min-w-0">
          <dt class="text-xs text-base-content/60">{$_('session.activity')}</dt>
          <dd class="mt-0.5 truncate" title={$dateTime(session.last_activity)}>{$dateTime(session.last_activity)}</dd>
        </div>
      </dl>
      {#snippet footer()}
        <span class="truncate text-xs text-base-content/50">
          {$_('session.expires', $dateTime(session.expire_at))}
        </span>
        {#if index === 0}
          <div class="flex shrink-0 items-center gap-1">
            <Button icon={icons.redo} text={$_('action.reload')} onclick={getSessions} />
            <Button
              square={false}
              icon={icons.clear}
              text={$_('session.sign_out_others')}
              disabled={sessions.length <= 1}
              onclick={() => {
                confirm({
                  icon: icons.clear,
                  message: $_('session.kill.others'),
                  onconfirm: () => killSessions(sessions.slice(1).map((s) => s.login_id))
                });
              }}
            />
          </div>
        {:else}
          <Button
            square={false}
            icon={icons.logout}
            text={$_('session.sign_out')}
            onclick={() => {
              confirm({
                icon: icons.logout,
                message: $_('session.kill.one'),
                onconfirm: () => killSessions([session.login_id])
              });
            }}
          />
        {/if}
      {/snippet}
    </EntityCard>
  {/snippet}
</Grid>
