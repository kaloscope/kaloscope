<script lang="ts">
  import { api } from '$lib/api';
  import { Container, Label, Select, Setting } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { user } from '$lib/stores';
  import { onMount } from 'svelte';

  // the loading state
  const loading = createLoading();

  // the shared history retention options
  const retentionDays = [1, 3, 7, 14, 30, 90, 180, 365];

  // the homepage options
  const homepageOptions = $derived.by(() => {
    const options = [
      { value: '/dashboard', label: 'nav.dashboard.title' },
      { value: '/websearch', label: 'nav.websearch.title' },
      { value: '/medialibs', label: 'nav.medialibs.title' },
      { value: '/settings', label: 'nav.settings.title' }
    ];
    if ($user?.role === 'admin') {
      options.splice(3, 0, { value: '/downloads', label: 'nav.downloads.title' });
    }
    return options;
  });

  /**
   * Update the preference value.
   *
   * @param key - The preference key.
   */
  function update(key: string) {
    if ($user?.preferences) {
      api
        .post('user/update_pref', {
          json: { key: key, value: $user.preferences[key] }
        })
        .catch(() => {
          user.set(null);
        });
    }
  }

  onMount(() => {
    // refresh user info when mounted
    loading.start();
    user.set(null);
    $effect(() => {
      $user && loading.end();
    });
  });
</script>

<Container type="settings" loading={$loading}>
  {#if $user?.preferences}
    <Setting title={$_('preference.navigation.title')}>
      <fieldset class="fieldset">
        <Label>{$_('preference.navigation.homepage')}</Label>
        <Select
          translate
          options={homepageOptions}
          bind:value={$user.preferences.homepage}
          onchange={() => update('homepage')}
          class="w-full"
        />
      </fieldset>
      <fieldset class="fieldset">
        <Label tip={$_('preference.navigation.vibration.tip')}>{$_('preference.navigation.vibration.title')}</Label>
        <Select
          translate
          options={[
            { value: false, label: 'action.toggle_off' },
            { value: true, label: 'action.toggle_on' }
          ]}
          bind:value={$user.preferences.vibration}
          onchange={() => update('vibration')}
          class="w-full"
        />
      </fieldset>
    </Setting>
    <Setting title={$_('preference.dashboard.title')} tip={$_('preference.dashboard.tip')}>
      <div>
        <fieldset class="fieldset grid-cols-2">
          <Label class="my-2">{$_('preference.dashboard.search')}</Label>
          <input
            type="checkbox"
            class="toggle self-center justify-self-end"
            bind:checked={$user.preferences.recent_searches}
            onchange={() => update('recent_searches')}
          />
        </fieldset>
        <fieldset class="fieldset grid-cols-2">
          <Label class="my-2">{$_('preference.dashboard.watch')}</Label>
          <input
            type="checkbox"
            class="toggle self-center justify-self-end"
            bind:checked={$user.preferences.recent_watches}
            onchange={() => update('recent_watches')}
          />
        </fieldset>
      </div>
    </Setting>
    <Setting title={$_('preference.privacy.title')} tip={$_('preference.privacy.tip')}>
      {#each [['search_records', 'search'], ['watch_records', 'watch'], ['read_records', 'read']] as [key, label] (key)}
        {@const current = $user.preferences[key]}
        <fieldset class="fieldset">
          <Label>{$_(`preference.privacy.${label}`)}</Label>
          <Select name={key} bind:value={$user.preferences[key]} onchange={() => update(key)} class="w-full">
            <option value={0}>{$_('preference.privacy.untrack')}</option>
            {#each retentionDays as days (days)}
              <option value={days}>{days} {$_(days === 1 ? 'duration.day' : 'duration.days').toLowerCase()}</option>
            {/each}
            {#if typeof current === 'number' && current > 0 && !retentionDays.includes(current)}
              <option value={current}>{current} {$_('duration.days').toLowerCase()}</option>
            {/if}
            <option value={-1}>{$_('preference.privacy.permanent')}</option>
          </Select>
        </fieldset>
      {/each}
    </Setting>
  {/if}
</Container>
