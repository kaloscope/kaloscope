<script lang="ts">
  import { enhance } from '$app/forms';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import { Label, Logo, Navbar } from '$lib/components';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import { _, headTitle } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { token, user } from '$lib/stores';
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
   * @param data - The login form data.
   */
  function login(data: FormData) {
    loading.start();
    api
      .post('auth/login', { body: data })
      .json<Resp<Token>>()
      .then(({ data }) => {
        user.set(data.user);
        token.set(data.token);
        goto('/');
      })
      .finally(() => {
        loading.end();
      });
  }

  onMount(() => {
    // redirect to home page if user is already logged in
    if ($token) {
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
      const url = new URL(page.url);
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

<Navbar />
<main class="hero min-h-(--ks-lvh) bg-base-200">
  <div class="hero-content mb-20 w-full flex-col gap-6">
    <div class="flex-center gap-2">
      <Logo size="2.5rem" />
      <span class="app-name text-4xl text-shadow-md">{$_('app.name', { locale: 'en-US' })}</span>
    </div>
    <div class="card w-full max-w-96 border bg-base-100 shadow-2xl">
      <form
        method="post"
        class="card-body"
        use:enhance={({ formData, cancel }) => {
          cancel();
          login(formData);
        }}
      >
        <fieldset class="fieldset">
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
          <button type="submit" class="btn mt-6 btn-primary" disabled={$loading !== null}>
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
