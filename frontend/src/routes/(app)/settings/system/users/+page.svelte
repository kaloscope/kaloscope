<script lang="ts">
  import { api } from '$lib/api';
  import {
    Badge,
    Button,
    Cell,
    DataView,
    HCell,
    Image,
    Paginator,
    Search,
    UserCreator,
    UserPermissions,
    confirm,
    type PaginatorProps
  } from '$lib/components';
  import { UserRole } from '$lib/enums';
  import { createLoading, createSortField } from '$lib/helpers';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { Page, Resp, User } from '$lib/types';
  import { untrack } from 'svelte';

  let users: User[] = $state([]);
  let username: string = $state('');
  let userCreator: UserCreator;
  let userPermissions: UserPermissions;

  const pagination: PaginatorProps = $state({ current: 1, size: 15, onchange: search });
  const ordering = createSortField();
  const loading = createLoading();

  /**
   * Search for users.
   *
   * @param page - Page number.
   * @param size - Page size.
   */
  function search(page: number = 1, size: number = pagination.size) {
    loading.start();
    api
      .get('user/list', {
        searchParams: {
          page_num: page,
          page_size: size,
          ordering: $ordering,
          username: username
        }
      })
      .json<Resp<Page<User>>>()
      .then(({ data }) => {
        pagination.current = page;
        pagination.size = size;
        pagination.total = data.total;
        users = data.items;
      })
      .finally(() => {
        loading.end();
      });
  }

  /**
   * Delete a user by ID.
   *
   * @param id - User ID.
   */
  function del(id: number) {
    loading.start();
    api
      .post('user/delete', { json: { ids: [id] } })
      .then(() => search(pagination.current))
      .catch(() => loading.end());
  }

  $effect(() => {
    $ordering; // eslint-disable-line
    untrack(() => search());
  });
</script>

<DataView dvh loading={$loading} data={users}>
  {#snippet filters()}
    <Search label={$_('field.username')} bind:value={username} onsearch={() => search()} />
  {/snippet}
  {#snippet actions()}
    <Button
      size="md"
      icon={icons.personAdd}
      text={$_('action.add', $_('entity.user'))}
      onclick={() => userCreator.showModal()}
    />
  {/snippet}
  {#snippet header()}
    <HCell width={['30%', '60%']} text={$_('field.username')} sort={ordering.bind('username')} />
    <HCell width={['20%', '40%']} text={$_('field.role')} />
    <HCell width={['25%', null]} text={$_('field.created')} sort={ordering.bind('created_at')} />
    <HCell width={['25%', null]} text={$_('session.activity')} />
    <HCell actions />
  {/snippet}
  {#snippet row(user)}
    <Cell>
      <Image circle src={user.avatar} icon={icons.user} text={user.username} width="2.75rem" />
      <div class="truncate" title={user.username}>{user.username}</div>
    </Cell>
    <Cell>
      {@const role = UserRole[user.role]}
      <Badge icon={role.icon} iconColor={role.iconColor}>{$_(role.label)}</Badge>
    </Cell>
    <Cell text={$dateTime(user.created_at)} />
    <Cell text={$dateTime(user.last_activity)} />
    <Cell
      actions={[
        {
          disabled: user.role === 'admin',
          icon: icons.personEdit,
          text: $_('action.assign_permissions'),
          onclick: () => userPermissions.showModal(user.id)
        },
        {
          disabled: user.role === 'admin',
          icon: icons.delete,
          text: $_('action.delete', $_('entity.user')),
          onclick: () => {
            confirm({
              icon: icons.delete,
              title: `${$_('action.delete', $_('entity.user'))} [${user.username}]`,
              onconfirm: () => del(user.id)
            });
          }
        }
      ]}
    />
  {/snippet}
  {#snippet paginator(disabled)}
    <Paginator {disabled} {...pagination} />
  {/snippet}
</DataView>

<UserCreator bind:this={userCreator} oncreate={search} />

<UserPermissions bind:this={userPermissions} />
