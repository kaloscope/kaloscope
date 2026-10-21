<script lang="ts" module>
  import type { Navigation } from '$lib/types';

  export type NavbarProps = {
    /** Navigation items. */
    navs?: Navigation[];
    /** Whether to show the back button. */
    back?: boolean;
    /** Whether to show the shadow effect. */
    shadow?: boolean;
    /** Whether to hide the navbar component. */
    hidden?: boolean;
    /** Whether to use the app mode. */
    appMode?: boolean;
  };
</script>

<script lang="ts">
  import { afterNavigate, goto } from '$app/navigation';
  import { page } from '$app/state';
  import { tooltip } from '$lib/actions';
  import { assistant, toggleAssistant } from '$lib/assistant/session';
  import { Languages, Logo, Notifications, PageHeader, Signposts, Themes, UserCenter } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { historyBack, subroutes } from '$lib/stores';

  let { navs = [], back = false, shadow = false, hidden = false, appMode = false }: NavbarProps = $props();
  let themeSwitcher: Themes;
  let langSwitcher: Languages;
  let notifications: Notifications;
  let logo: Logo | null = $state(null);

  // unread notifications count
  let unreadNotifications = $state(0);

  // the dynamic class names
  let navbarClass = $derived(appMode ? 'sticky bg-blur-90' : 'bg-base-200');
  let shadowClass = $derived(shadow ? 'nav-shadow' : '');

  // get the drawer style based on the current page
  let drawerStyle: string | null | undefined = $state(null);
  afterNavigate(() => {
    if (document.querySelector('.drawer') === null) {
      drawerStyle = null;
    } else {
      const pathname = page.url.pathname;
      drawerStyle = navs.find((nav) => pathname.startsWith(nav.path))?.drawerStyle;
    }
  });
</script>

<div class="navbar top-0 layer-2 h-(--ks-navbar-h) min-h-(--ks-navbar-h) {navbarClass} {shadowClass}" class:hidden>
  <!-- left part -->
  <div class="mr-auto flex-center shrink-0 overflow-hidden">
    {#if appMode}
      {@const btnClass = 'btn h-10 min-h-10 btn-subtle'}
      {#if back}
        <!-- back button -->
        <button class="btn-square w-10 opacity-90 {btnClass}" aria-label="Back" onclick={historyBack}>
          <iconify-icon icon={icons.backSolid} width="1.25rem"></iconify-icon>
        </button>
      {:else}
        <!-- drawer button -->
        {#if drawerStyle}
          <label for="drawer" class="btn-square w-10 text-surface opacity-90 sm:hidden {btnClass}">
            {#if drawerStyle === 'menu'}
              <iconify-icon icon={icons.menuFoldSolid} width="1.75rem"></iconify-icon>
            {:else if drawerStyle === 'app'}
              <iconify-icon icon={icons.moreApp} width="1.5rem"></iconify-icon>
            {/if}
          </label>
        {/if}
        <!-- logo button -->
        <button class="border-0 bg-transparent px-3 max-sm:hidden {btnClass}" onclick={() => logo && logo.spin()}>
          <Logo size="2rem" bind:this={logo} />
          <span class="app-name text-2xl text-shadow-sm max-lg:hidden">
            {$_('app.name', { locale: 'en-US' })}
          </span>
        </button>
        <!-- reload button -->
        {#if !drawerStyle}
          <button
            class="border-0 bg-transparent px-2 app-name text-xl sm:hidden {btnClass}"
            onclick={() => location.reload()}
          >
            {$_('app.name', { locale: 'en-US' })}
          </button>
        {/if}
      {/if}
      <Signposts class="sm:hidden" />
    {/if}
  </div>
  <!-- center part -->
  <div class="mx-auto flex-center overflow-hidden">
    <PageHeader>
      {#snippet fallback()}
        <ul class="menu menu-horizontal gap-2 p-0 max-sm:hidden">
          {#each navs as nav (nav.path)}
            {@const active = page.url.pathname.startsWith(nav.path)}
            <li>
              <a
                href={nav.path}
                aria-label={$_(nav.title)}
                class="flex-center size-10 duration-300 {active ? 'pointer-events-none item-emphasis' : ''}"
                onclick={(event) => {
                  if (active) {
                    event.preventDefault();
                    return;
                  }
                  // navigate to subroute if exists
                  const subroute = $subroutes?.[nav.path];
                  if (subroute) {
                    event.preventDefault();
                    goto(subroute, { replaceState: true });
                  }
                }}
                use:tooltip={{
                  zIndex: 9999,
                  content: $_(nav.title),
                  followCursor: true,
                  delay: [800, 0]
                }}
              >
                <iconify-icon icon={active ? nav.iconFilled : nav.icon} width="1.5rem"></iconify-icon>
              </a>
            </li>
          {/each}
        </ul>
      {/snippet}
    </PageHeader>
  </div>
  <!-- right part -->
  <div class="ml-auto flex-center shrink-0 gap-1">
    <Languages
      class="dropdown-end {appMode ? 'max-sm:hidden' : ''}"
      triggerClass="h-(--ks-navbar-h)"
      contentClass="mt-1"
      bind:this={langSwitcher}
    />
    <Themes
      class="dropdown-end {appMode ? 'max-sm:hidden' : ''}"
      triggerClass="h-(--ks-navbar-h)"
      contentClass="mt-1"
      pwaThemeColor={appMode ? '--color-base-125' : '--color-base-200'}
      bind:this={themeSwitcher}
    />
    <Notifications
      class="mr-1.5 {appMode ? 'max-sm:hidden' : 'hidden'}"
      onrefresh={(unread) => (unreadNotifications = unread)}
      bind:this={notifications}
    />
    {#if appMode}
      <button
        id="assistant-trigger"
        type="button"
        class="btn h-10 min-h-10 btn-subtle px-3"
        aria-label={$_($assistant.open ? 'assistant.close_shortcut' : 'assistant.open')}
        use:tooltip={{ content: $_($assistant.open ? 'assistant.close_shortcut' : 'assistant.open'), zIndex: 10000 }}
        aria-haspopup="dialog"
        aria-expanded={$assistant.open}
        aria-controls="assistant-panel"
        aria-keyshortcuts="Alt+A"
        onclick={toggleAssistant}
      >
        {#if $assistant.busy}
          <span class="loading size-6 loading-spinner opacity-90" aria-hidden="true"></span>
        {:else}
          <iconify-icon icon={icons.botSquare} width="1.5rem" class="size-6 opacity-90" aria-hidden="true"
          ></iconify-icon>
        {/if}
      </button>
    {/if}
    <UserCenter
      {navs}
      switchTheme={() => themeSwitcher.showModal()}
      switchLanguage={() => langSwitcher.showModal()}
      notifications={{ show: () => notifications.showModal(), unread: unreadNotifications }}
      class="dropdown-end {appMode ? '' : 'hidden'}"
      triggerClass="h-(--ks-navbar-h)"
      contentClass="mt-1"
    />
  </div>
</div>
