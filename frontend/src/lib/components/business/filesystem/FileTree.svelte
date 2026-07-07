<script lang="ts" module>
  import type { Path, PathStats, Resp } from '$lib/types';

  type FileTreeProps = Partial<{
    rootPath: string;
    onlyDirs: boolean;
    onconfirm: (stats: PathStats) => void;
  }>;
</script>

<script lang="ts">
  import { enhance } from '$app/forms';
  import { api } from '$lib/api';
  import { Modal, alert, prompt } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { token } from '$lib/stores';
  import { onMount } from 'svelte';

  let { rootPath = '', onlyDirs = false, onconfirm }: FileTreeProps = $props();
  let paths: Path[] = $state([]);
  let current: string = $state('');
  let showHidden: boolean = $state(false);

  // the modal dialog instance
  let modal: Modal;
  export const showModal = (expandTo?: string) => {
    if (!paths || paths.length === 0 || expandTo) {
      if (expandTo) {
        current = expandTo;
      }
      list(rootPath, expandTo).then((result) => {
        paths = result;
        modal.show();
      });
    } else {
      modal.show();
    }
  };

  // the loading state
  const loading = createLoading();

  /**
   * Prompt for a directory name and create it under the selected directory.
   */
  function promptCreateDir() {
    const parent = current || rootPath;
    if (!parent) {
      alert({ level: 'error', message: 'bad_request' });
      return;
    }
    prompt({
      icon: icons.addCircle,
      title: $_('filesystem.new_folder'),
      message: $_('filesystem.new_folder_message'),
      placeholder: $_('filesystem.new_folder_placeholder'),
      onconfirm: (name) => createDir(parent, name)
    });
  }

  /**
   * Create a child directory and select it in the tree.
   *
   * @param parent - The path to create the directory under.
   * @param name - The new directory name.
   */
  async function createDir(parent: string, name?: string) {
    const folderName = name?.trim();
    if (!folderName) {
      alert({ level: 'error', message: 'bad_request' });
      return;
    }
    loading.start();
    try {
      const resp = await api.post('filesystem/mkdir', { json: { parent, name: folderName } }).json<Resp<string>>();
      current = resp.data;
      paths = await list(rootPath, current);
    } catch (error) {
      console.error(error);
    } finally {
      loading.end();
    }
  }

  /**
   * List the files and directories in the given path.
   *
   * @param path - The path to list.
   * @param expandTo - The path to expand to.
   */
  async function list(path: string, expandTo?: string): Promise<Path[]> {
    try {
      const resp = await api
        .get('filesystem/list', {
          searchParams: {
            path: path,
            only_dirs: onlyDirs,
            expand_to: expandTo
          }
        })
        .json<Resp<Path[]>>();
      return resp.data;
    } catch (error) {
      console.error(error);
      return [];
    }
  }

  /**
   * Unfold the given path and load its children.
   *
   * @param path - The path to unfold.
   */
  async function unfold(path: Path) {
    current = path.path;
    if (path.children) {
      return;
    }
    path.loading = false;
    setTimeout(() => {
      if (path.loading === false) path.loading = true;
    }, 500);
    path.children = await list(path.path);
    path.loading = undefined;
  }

  /**
   * Confirm the selected path and close the modal.
   *
   * @param path - The path to confirm.
   */
  export function confirm(path?: string) {
    loading.start();
    api
      .get('filesystem/stats', { searchParams: { path: path ?? current } })
      .json<Resp<PathStats>>()
      .then(({ data }) => {
        if (onlyDirs && !data.writable) {
          alert({ level: 'error', message: 'permission_denied' });
          return;
        }
        modal.close();
        onconfirm?.(data);
      })
      .finally(() => {
        loading.end();
      });
  }

  onMount(async () => {
    if ($token) {
      paths = await list(rootPath);
    }
  });
</script>

<Modal
  icon={icons.rowChild}
  title={$_('action.select', $_(onlyDirs ? 'field.dir' : 'field.file'))}
  maxWidth="40rem"
  bind:this={modal}
>
  <form
    method="post"
    use:enhance={({ cancel }) => {
      cancel();
      confirm();
    }}
  >
    <fieldset class="fieldset">
      <ul class="menu max-h-[50vh] w-full flex-nowrap gap-1 overflow-auto rounded-box border">
        {@render tree(paths)}
      </ul>
      <label class="mt-2 fieldset-label w-fit">
        <input type="checkbox" class="checkbox" bind:checked={showHidden} />
        <span class="text-base text-base-content opacity-90">{$_('filesystem.show_hidden')}</span>
      </label>
    </fieldset>
    <div class="modal-action">
      {#if onlyDirs}
        <button
          type="button"
          class="btn mr-auto"
          onclick={promptCreateDir}
          disabled={$loading !== null || !(current || rootPath)}
        >
          <iconify-icon icon={icons.addCircle} width="1.25rem" class="size-5"></iconify-icon>
          {$_('filesystem.new_folder')}
        </button>
      {/if}
      <button type="button" class="btn" onclick={() => modal.close()}>
        {$_('message.cancel')}
      </button>
      <button type="submit" class="btn btn-submit" disabled={$loading !== null}>
        {$_('message.confirm')}
        {#if $loading}
          <span class="loading loading-xs loading-dots"></span>
        {/if}
      </button>
    </div>
  </form>
</Modal>

{#snippet tree(paths?: Path[] | null)}
  {#if paths}
    {#each paths.filter((p) => showHidden || !p.is_hidden) as path (path.path)}
      {@const activeClass = current === path.path ? 'item-emphasis' : ''}
      <li>
        {#if path.is_dir && !path.is_empty}
          <details bind:open={path.open}>
            <summary class={activeClass} onclick={() => unfold(path)}>
              {@render item(path)}
            </summary>
            <ul>{@render tree(path.children)}</ul>
          </details>
        {:else}
          <button type="button" class={activeClass} onclick={() => (current = path.path)}>
            {@render item(path)}
          </button>
        {/if}
      </li>
    {/each}
  {/if}
{/snippet}

{#snippet item(path: Path)}
  <span class="flex-center size-5 opacity-80">
    {#if path.loading}
      <span class="loading loading-xs loading-spinner"></span>
    {:else}
      <iconify-icon icon={path.is_dir ? icons.folder : icons.document} width="1.25rem"></iconify-icon>
    {/if}
  </span>
  {path.name}
{/snippet}
