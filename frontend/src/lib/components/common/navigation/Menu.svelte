<script lang="ts" module>
  import type { Menu, MenuRoute, Signpost } from '$lib/types';

  export type MenuProps = {
    /** List of menu items. */
    menus: Menu[];
    /** Map of properties that should be interpolated in the i18n message. */
    interpolation?: Record<string, string | number | (string | number)[]>;
    /** Whether route titles should be translated as i18n keys. */
    translate?: boolean;
  };
</script>

<script lang="ts">
  import { afterNavigate, goto } from '$app/navigation';
  import { page } from '$app/state';
  import { Image, Logo } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { freeze, signposts } from '$lib/stores';
  import { onMount } from 'svelte';

  let { menus, interpolation = {}, translate = true }: MenuProps = $props();

  /**
   * Hide the drawer when a menu item is clicked.
   *
   * @param callback - Callback function to be executed after the drawer is closed.
   */
  function hideDrawer(callback: () => void) {
    const drawer = document.getElementById('drawer') as HTMLInputElement | null;
    if (drawer && drawer.checked) {
      drawer.checked = false;
      // disable the freeze state to allow scrolling
      freeze.set(false);
      // wait for the drawer to close
      setTimeout(callback, 300);
    } else {
      callback();
    }
  }

  onMount(() => {
    const updateSignposts = () => {
      // set the signposts based on the current page
      const pathname = page.url.pathname;
      let titles: Signpost[] = [];
      loop: for (const menu of menus) {
        for (const route of menu.routes) {
          if (route.path && pathname.startsWith(route.path)) {
            const routeTranslate = route.translate ?? translate;
            titles = [menu.title, routeTranslate ? route.title : { title: route.title, translate: false }];
            break loop;
          }
        }
      }
      signposts.set(titles);
    };
    updateSignposts();
    afterNavigate(updateSignposts);
    // clear the signposts when the component is destroyed
    return () => {
      signposts.set([]);
    };
  });
</script>

<ul class="menu min-h-dvh w-60 gap-1 max-sm:bg-base-125 sm:min-h-(--ks-lvh)">
  <div class="flex-center gap-2 p-2 sm:hidden">
    <Logo size="1.5rem" />
    <span class="app-name text-xl">{$_('app.name', { locale: 'en-US' })}</span>
  </div>
  {#each menus as menu (menu.title)}
    <li class="menu-title">{$_(menu.title)}</li>
    {#each menu.routes as route, index (index)}
      <li>
        {#if route.path}
          {@const blank = route.path.toLowerCase().startsWith('http')}
          {@const active = page.url.pathname === route.path}
          <a
            href={route.path}
            target={blank ? '_blank' : ''}
            class="{blank ? 'group' : ''} {active ? 'menu-emphasis' : ''}"
            onclick={(event) => {
              if (!blank) {
                event.preventDefault();
                !active && hideDrawer(() => route.path && goto(route.path));
              }
            }}
          >
            {@render routeContent(route)}
          </a>
        {:else}
          <span class="pointer-events-none">
            {@render routeContent(route)}
          </span>
        {/if}
      </li>
    {/each}
  {/each}
</ul>

{#snippet routeContent(route: MenuRoute)}
  {@const routeTranslate = route.translate ?? translate}
  {#if typeof route.icon === 'string'}
    <Image src={route.icon} width="1.25rem" />
  {:else}
    <iconify-icon icon={route.icon} width="1.25rem" class="size-5" style:color={route.iconColor}></iconify-icon>
  {/if}
  {routeTranslate ? $_(route.title, interpolation[route.title]) : route.title}
  <iconify-icon icon={icons.externalLink} width="1rem" class="invisible group-hover:visible"></iconify-icon>
{/snippet}
