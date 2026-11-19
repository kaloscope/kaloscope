<script lang="ts">
  import { enhance } from '$app/forms';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import { Label, Logo, Navbar } from '$lib/components';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import { _, headTitle } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { token, user } from '$lib/stores.svelte';
  import type { Resp, Token } from '$lib/types';
  import { onMount } from 'svelte';

  // the loading state and form schema
  const loading = createLoading();
  const schema = createFormSchema(({ text, password }) => ({
    username: text().maxlength(64).autocomplete('username'),
    password: password().maxlength(64).autocomplete('current-password')
  }));

  // local state for auto-fill from query params
  let usernameValue = $state('');
  let passwordValue = $state('');

  /**
   * Login user with given data and redirect to home page.
   *
   * @param data - Login form data.
   */
  function login(data: FormData) {
    loading.start();
    api
      .post('auth/login', { body: data })
      .json<Resp<Token>>()
      .then(({ data }) => {
        token.current = data.token;
        user.current = data.user;
        goto('/');
      })
      .finally(() => {
        loading.end();
      });
  }

  onMount(() => {
    // redirect to home page if user is already logged in
    if (token.current) {
      goto('/');
      return;
    }

    // auto-fill form from query params
    const params = page.url.searchParams;
    const username = params.get('username');
    const password = params.get('password');

    if (username || password) {
      if (username) usernameValue = username;
      if (password) passwordValue = password;

      // remove query params from URL after filling
      const url = new URL(page.url.href);
      url.searchParams.delete('username');
      url.searchParams.delete('password');
      goto(url, { replaceState: true });
    }

    // check if there are any users, if not redirect to setup page
    api
      .get('user/count')
      .json<Resp<number>>()
      .then(({ data }) => {
        if (!data) {
          goto('/setup');
        }
      });
  });
</script>

<svelte:head>
  <title>{$headTitle('app.login')}</title>
</svelte:head>

<!-- one background behind the bar and the form, so there is no seam between them -->
<div class="min-h-dvh bg-aurora">
  <Navbar />
  <main class="hero min-h-(--ks-lvh)">
    <div class="hero-content mb-20 w-full flex-col gap-8">
      <div class="flex-center gap-2">
        <Logo size="2.5rem" />
        <span class="app-name text-4xl text-shadow-md">{$_('app.name', { locale: 'en-US' })}</span>
      </div>
      <div class="w-full max-w-96 card-auth">
        <form
          method="post"
          class="p-6 sm:p-7"
          use:enhance={({ formData, cancel }) => {
            cancel();
            login(formData);
          }}
        >
          <fieldset class="fieldset gap-y-2">
            <Label small>{$_('field.username')}</Label>
            <label class="input w-full">
              <iconify-icon icon={icons.user}></iconify-icon>
              <input class="grow" {...schema.username} bind:value={usernameValue} />
            </label>
            <Label small>{$_('field.password')}</Label>
            <label class="input w-full">
              <iconify-icon icon={icons.key}></iconify-icon>
              <input class="grow" {...schema.password} bind:value={passwordValue} />
            </label>
            <button type="submit" class="btn mt-6 h-11 rounded-full shadow-sm btn-primary" disabled={$loading !== null}>
              {$_('app.login')}
              {#if $loading}
                <span class="loading loading-xs loading-dots"></span>
              {/if}
            </button>
          </fieldset>
        </form>
      </div>
    </div>
  </main>
</div>
