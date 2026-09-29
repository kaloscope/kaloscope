<script lang="ts">
  import { api } from '$lib/api';
  import { Drawer, Menu } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { user } from '$lib/stores';
  import type { MediaLib, Resp } from '$lib/types';
  import { onMount, type Snippet } from 'svelte';
  import type { LayoutData } from './$types';

  let { data, children }: { data: LayoutData; children: Snippet } = $props();

  let scanning: number[] = $state([]);
  let pending: number[] = $state([]);
  const abortController = new AbortController();

  /**
   * Scan a media library and track its completion.
   *
   * @param id - The media library ID.
   * @returns A promise that resolves when the scan request finishes.
   */
  async function scan(id: number) {
    if (scanning.includes(id)) {
      return;
    }
    scanning = [...scanning, id];
    pending = [...pending, id];
    try {
      await api.get(`media/lib/${id}/scan`, { signal: abortController.signal });
    } catch {
      scanning = scanning.filter((libId) => libId !== id);
    } finally {
      pending = pending.filter((libId) => libId !== id);
    }
  }

  $effect(() => {
    // wait for scan requests to finish before checking their background status
    const ids = scanning.filter((id) => !pending.includes(id));
    if (ids.length === 0) {
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const { data: libs } = await api.get('media/lib/list', { signal: controller.signal }).json<Resp<MediaLib[]>>();
        if (!controller.signal.aborted) {
          scanning = scanning.filter((id) => !ids.includes(id) || libs.some((lib) => lib.id === id && lib.scanning));
        }
      } catch {
        if (!controller.signal.aborted) {
          // keep the current state and retry after a temporary status request failure
          scanning = [...scanning];
        }
      }
    }, 2000);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  });

  onMount(() => {
    scanning = data.libs.filter((lib) => lib.scanning).map((lib) => lib.id);
    return () => abortController.abort();
  });
</script>

<Drawer>
  {@render children()}
  {#snippet side()}
    <Menu menus={data.menus}>
      {#snippet action(route)}
        {@const lib = data.libs.find((lib) => route.path === `/medialibs/${lib.id}`)}
        {#if lib && $user?.role === 'admin'}
          {@const busy = scanning.includes(lib.id)}
          <button
            type="button"
            class="flex cursor-pointer transition-opacity {busy
              ? ''
              : 'opacity-0 group-hover/menu:opacity-100 focus-visible:opacity-100 group-focus-within/menu:in-[:active-view-transition]:opacity-100'}"
            aria-label={$_('action.scan', lib.name)}
            aria-busy={busy}
            aria-disabled={busy}
            onclick={() => scan(lib.id)}
          >
            {#if busy}
              <span class="loading size-5 loading-spinner"></span>
            {:else}
              <iconify-icon icon={icons.folderSearch} width="1.25rem"></iconify-icon>
            {/if}
          </button>
        {/if}
      {/snippet}
    </Menu>
  {/snippet}
</Drawer>
