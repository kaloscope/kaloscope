<script lang="ts" module>
  import type { FlowRepo, Resp } from '$lib/types';

  /**
   * The flow repositories.
   */
  let repos: FlowRepo[] = $state([]);
</script>

<script lang="ts">
  import { api } from '$lib/api';
  import { Button, Image, Modal, alert, confirm, prompt } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { onMount } from 'svelte';

  let {
    onchange,
    onclose
  }: {
    onchange?: (repos: FlowRepo[]) => void;
    onclose?: (changed: boolean) => void;
  } = $props();

  // track if the repositories have changed
  let changed = false;

  // the modal dialog instance
  let modal: Modal;
  export const showModal = () => {
    changed = false;
    modal.show();
  };

  // the loading state
  const loading = createLoading();

  /**
   * Add a GitHub repository.
   *
   * @param url - The input URL.
   */
  function addRepo(url: string | undefined) {
    if (!url) {
      return;
    }
    url = url.trim();
    // validate the URL format
    const regex = /^(?:https:\/\/github\.com\/)?([\w.-]+)\/([\w.-]+)(?:\.git)?$/i;
    if (!regex.test(url)) {
      alert({ level: 'error', message: 'invalid_github_repo' });
      return;
    }
    loading.start();
    api
      .post('flow/repo/add', { json: { repo: url } })
      .json<Resp<FlowRepo>>()
      .then(({ data }) => {
        if (repos.every((r) => r.id !== data.id)) {
          repos.push(data);
        }
        onchange?.(repos);
        changed = true;
      })
      .finally(() => {
        loading.end();
      });
  }

  /**
   * Delete a GitHub repository.
   *
   * @param repo - The repository to delete.
   */
  function deleteRepo(repo: FlowRepo) {
    repo.loading = false;
    setTimeout(() => {
      if (repo.loading === false) repo.loading = true;
    }, 500);
    api
      .post('flow/repo/delete', { json: { ids: [repo.id] } })
      .then(() => {
        repos = repos.filter((r) => r.id !== repo.id);
        onchange?.(repos);
        changed = true;
      })
      .finally(() => {
        repo.loading = undefined;
      });
  }

  onMount(() => {
    api
      .get('flow/repo/list')
      .json<Resp<FlowRepo[]>>()
      .then(({ data }) => {
        repos = data;
        onchange?.(repos);
      });
  });
</script>

<Modal
  icon={icons.cloudCube}
  title={$_('action.edit', $_('entity.sources'))}
  onclose={() => onclose?.(changed)}
  bind:this={modal}
>
  {#if repos.length > 0}
    <ul class="list overflow-hidden rounded-box border shadow-sm">
      <li
        class="bg-gradient px-3 py-1 text-xs text-base-content/50"
        style="border-bottom: 1px inset var(--color-border)"
      >
        {$_('flow.tmpl.last_sync', $dateTime(repos[0].updated_at))}
      </li>
      {#each repos as repo (repo.id)}
        <li class="list-row items-center">
          <Image transparent src={repo.owner_avatar} icon={icons.box3d} width="2rem" />
          <span>
            <a href={repo.repo_url} target="_blank" class="link link-hover">{repo.repo_name}</a>
            <div class="text-xs font-semibold opacity-60">{repo.repo_description}</div>
          </span>
          <Button
            icon={icons.subtractCircle}
            text={$_('action.delete', $_('entity.source'))}
            disabled={repo.loading !== undefined}
            loading={repo.loading}
            onclick={() => {
              confirm({
                icon: icons.subtractCircle,
                title: `${$_('action.delete', $_('entity.source'))} [${repo.repo_name}]`,
                onconfirm: () => deleteRepo(repo)
              });
            }}
          />
        </li>
      {/each}
    </ul>
  {/if}
  <Button
    ghost={false}
    square={false}
    icon={icons.addCircle}
    text={$_('action.add', $_('entity.source'))}
    disabled={$loading !== null}
    loading={$loading}
    onclick={() => {
      prompt({
        icon: icons.box3d,
        title: $_('action.add', $_('entity.source')),
        message: $_('flow.tmpl.confirm_repo'),
        hint: $_('flow.tmpl.risk_warning'),
        placeholder: 'kaloscope/workflows',
        options: ['kaloscope/workflows'],
        onconfirm: (url) => addRepo(url)
      });
    }}
  />
</Modal>
