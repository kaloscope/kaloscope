<script lang="ts" module>
  import type { DropdownProps } from '$lib/components';
  import type { Navigation, Resp } from '$lib/types';
  import type { IconifyIcon } from 'iconify-icon';
  import type { MouseEventHandler } from 'svelte/elements';
  import { MediaQuery } from 'svelte/reactivity';

  type UserCenterProps = {
    /** Navigation items. */
    navs?: Navigation[];
    /** Function to switch the theme. */
    switchTheme?: () => void;
    /** Function to switch the language. */
    switchLanguage?: () => void;
    /** Notifications configuration. */
    notifications?: {
      show: () => void;
      unread: number;
    };
  } & Pick<DropdownProps, 'class' | 'triggerClass' | 'contentClass'>;

  // the standalone display mode media query
  const standaloneMode = new MediaQuery('(display-mode: standalone)');

  // the deferred prompt event for PWA installation
  // https://developer.mozilla.org/en-US/docs/Web/API/BeforeInstallPromptEvent
  type BeforeInstallPromptEvent = Event & { prompt: () => void };
  let deferredPrompt: BeforeInstallPromptEvent | null = $state(null);

  // whether the app is in fullscreen mode
  let isFullscreen: boolean = $state(fullscreen.isFullscreen());
</script>

<script lang="ts">
  import { goto } from '$app/navigation';
  import { api } from '$lib/api';
  import { Dropdown, Image, Modal, confirm } from '$lib/components';
  import { _, duration } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { token, user } from '$lib/stores';
  import { fullscreen } from '$lib/utils/browser';
  import { onMount, tick } from 'svelte';

  let {
    navs = [],
    switchTheme,
    switchLanguage,
    notifications,
    class: _class,
    triggerClass,
    contentClass
  }: UserCenterProps = $props();

  // the duration of the user's login time
  let loginDuration: string = $state($duration($user?.login_at));

  // the current application version
  let version: string = $state('');

  let updateModal: Modal;
  let checkingUpdates = $state(false);
  let updateFailed = $state(false);
  let latestVersion = $state('');

  /**
   * Check for a newer stable version and update the dialog.
   *
   * @returns A promise that resolves when the check finishes.
   */
  async function checkForUpdates() {
    if (checkingUpdates) {
      return;
    }
    checkingUpdates = true;
    updateFailed = false;
    latestVersion = '';
    await tick();
    try {
      const { data } = await api
        .get('system/version', {
          searchParams: { check_update: true },
          timeout: 15000,
          retry: 0,
          context: { silentErrors: true }
        })
        .json<Resp<{ version: string; latest_version: string; has_update: boolean }>>();
      version = `v${data.version}`;
      latestVersion = data.has_update ? `v${data.latest_version}` : '';
    } catch {
      updateFailed = true;
    } finally {
      checkingUpdates = false;
    }
  }

  /**
   * Logout the user.
   */
  function logout() {
    api.post('auth/logout').then(() => {
      token.set(null);
      goto('/login');
    });
  }

  onMount(() => {
    api
      .get('system/version')
      .json<Resp<{ version: string }>>()
      .then(({ data }) => {
        if (data.version) {
          version = `v${data.version}`;
        }
      });
  });
</script>

<svelte:window
  onbeforeinstallprompt={(event: Event) => {
    event.preventDefault();
    deferredPrompt = event as BeforeInstallPromptEvent;
  }}
/>

<svelte:document onfullscreenchange={() => (isFullscreen = fullscreen.isFullscreen())} />

{#snippet option(
  icon: string | IconifyIcon,
  text: string,
  onclick: MouseEventHandler<HTMLElement>,
  _class?: string,
  badge?: number | string
)}
  <li class={_class}>
    <button onclick={(event) => onclick?.(event)}>
      <iconify-icon {icon} width="1.25rem" class="size-5"></iconify-icon>
      {text}
      {#if typeof badge === 'number' && badge > 0}
        <span class="badge badge-sm badge-primary">
          {badge > 99 ? '99+' : badge}
        </span>
      {:else if typeof badge === 'string' && badge}
        <span class="badge badge-sm opacity-50">{badge}</span>
      {/if}
    </button>
  </li>
{/snippet}

<Dropdown
  class={_class}
  {triggerClass}
  {contentClass}
  onclick={() => {
    loginDuration = $duration($user?.login_at);
  }}
>
  {#snippet trigger()}
    <Image
      circle
      shadow
      sluggish
      src={$user?.avatar}
      text={$user?.username}
      icon={icons.user}
      class="pointer-events-none mx-2"
    />
  {/snippet}

  <ul class="menu gap-1">
    {#if $user}
      <div class="text-center text-lg font-bold text-surface">
        {$user.username}
      </div>
    {/if}
    {#if loginDuration}
      <div class="text-center text-xs opacity-60">
        {`${$_('session.duration')}: ${loginDuration}`}
      </div>
    {/if}

    <div class="divider my-0"></div>
    {@render option(icons.language, $_('app.switch_language'), () => switchLanguage?.(), 'sm:hidden')}
    {@render option(icons.colorSwatch, $_('app.switch_theme'), () => switchTheme?.(), 'sm:hidden')}
    {#if isFullscreen}
      {@render option(icons.fullScreenMinimizeFilled, $_('app.exit_fullscreen'), () => fullscreen.exit())}
    {:else}
      {@render option(icons.fullScreenMaximizeFilled, $_('app.enter_fullscreen'), () => fullscreen.enter())}
    {/if}

    <div class="divider my-0"></div>
    {@render option(
      icons.alertUrgent,
      $_('app.notifications'),
      () => notifications?.show(),
      'sm:hidden',
      notifications?.unread
    )}
    {#each navs.filter((nav) => !nav.mobile) as nav (nav.path)}
      {@render option(nav.icon, $_(nav.title), () => goto(nav.path), 'sm:hidden')}
    {/each}
    {@render option(icons.lockClosedKey, $_('nav.settings.personal.account'), () => {
      goto('/settings/personal/account');
    })}
    {@render option(icons.textGrammarSettings, $_('nav.settings.personal.preferences'), () => {
      goto('/settings/personal/preferences');
    })}

    <div class="divider my-0"></div>
    {#if !standaloneMode.current && deferredPrompt}
      {@render option(icons.desktopArrowDown, $_('app.pwa_install'), () => deferredPrompt?.prompt())}
    {/if}
    {@render option(
      icons.stars,
      $_('app.check_updates'),
      () => {
        updateModal.show();
        checkForUpdates();
      },
      '',
      version
    )}

    <div class="divider my-0"></div>
    {@render option(
      icons.signOut,
      $_('app.logout'),
      () => confirm({ icon: icons.signOut, title: $_('app.logout'), onconfirm: logout }),
      'text-base-content/60 font-semibold'
    )}
  </ul>
</Dropdown>

<Modal bind:this={updateModal} title={$_('app.check_updates')} icon={icons.stars} maxWidth="24rem">
  <div class="flex min-h-24 flex-col items-center justify-center gap-3 text-center" role="status" aria-live="polite">
    {#if checkingUpdates}
      <span class="loading loading-md loading-spinner" aria-hidden="true"></span>
      <p>{$_('app.checking_updates')}</p>
    {:else if updateFailed}
      <p class="text-error">{$_('app.update_check_failed')}</p>
    {:else if latestVersion}
      <p>
        {$_('app.latest_version')}
        <a
          class="link font-semibold link-primary"
          href="https://hub.docker.com/r/kaloscope/kaloscope/tags"
          target="_blank"
          rel="noreferrer"
        >
          {latestVersion}
        </a>
      </p>
    {:else}
      <p>{$_('app.up_to_date')}</p>
    {/if}
  </div>
  {#if updateFailed}
    <div class="modal-action">
      <button type="button" class="btn btn-submit" onclick={checkForUpdates}>
        {$_('action.retry')}
      </button>
    </div>
  {/if}
</Modal>
