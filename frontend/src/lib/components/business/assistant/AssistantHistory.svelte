<script lang="ts">
  import { assistantCanvas } from '$lib/assistant/canvas';
  import {
    assistant,
    assistantRestoring,
    changeAssistantConversation,
    deleteAssistantConversations,
    loadAssistantConversation,
    loadAssistantConversations,
    newAssistantConversation
  } from '$lib/assistant/session';
  import { Dropdown, alert, confirm, prompt } from '$lib/components';
  import { closeDropdowns } from '$lib/components/common/interaction/Dropdown.svelte';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { user } from '$lib/stores';
  import type { AssistantConversation } from '$lib/types';

  let historyOpen = $state(false);
  let managing = $state(false);
  let confirming = $state(false);
  let selectedIds = $state<number[]>([]);

  const locked = $derived(!!$assistantCanvas || $assistant.busy || $assistant.historyLoading || $assistantRestoring);
  const selectableIds = $derived($assistant.conversations.filter((item) => !item.busy).map((item) => item.id));
  const selected = $derived(selectedIds.filter((id) => selectableIds.includes(id)));
  const allSelected = $derived(selectableIds.length > 0 && selected.length === selectableIds.length);

  $effect(() => {
    if (!$assistant.open) historyOpen = false;
    if (!historyOpen && !confirming) {
      managing = false;
      selectedIds = [];
    }
  });

  /**
   * Refresh metadata when opening the history menu outside an ongoing operation.
   */
  function showHistory(): void {
    if (!historyOpen && !locked && !confirming) void loadAssistantConversations();
  }

  /**
   * Toggle one idle conversation without opening its transcript.
   *
   * @param id - Loaded conversation identifier.
   * @param checked - Whether the conversation should be selected.
   */
  function select(id: number, checked: boolean): void {
    selectedIds = checked ? [...selected, id] : selected.filter((value) => value !== id);
  }

  /**
   * Confirm and delete the selected conversations while retaining failed selections.
   */
  function removeSelected(): void {
    if (locked || !selected.length || selected.length > 999) return;
    const ids = [...selected];
    const ownerId = $user?.id;
    confirming = true;
    confirm({
      icon: icons.delete,
      title: $_('assistant.delete_chats_confirm', ids.length),
      message: $_('assistant.delete_chats_warning'),
      oncancel: () => {
        // keep outside clicks suspended until the confirmation click has finished
        setTimeout(() => (confirming = false), 0);
      },
      onconfirm: async () => {
        if ($user?.id !== ownerId) {
          confirming = false;
          return;
        }
        const deleted = await deleteAssistantConversations(ids);
        if (deleted) {
          managing = false;
          selectedIds = [];
          alert({ level: 'success', message: $_('assistant.chats_deleted', ids.length) });
        }
        confirming = false;
      }
    });
  }

  /**
   * Start a new conversation and move focus to the composer before the button disappears.
   */
  function start(): void {
    newAssistantConversation();
    document.getElementById('assistant-input')?.focus({ preventScroll: true });
  }

  /**
   * Open the selected conversation and dismiss its history menu.
   *
   * @param id - Owned conversation to restore.
   */
  function open(id: number): void {
    closeDropdowns();
    void loadAssistantConversation(id);
  }

  /**
   * Edit the selected title using the application's existing prompt dialog.
   */
  function rename(): void {
    const conversation = $assistant.conversation;
    if (!conversation) return;
    prompt({
      icon: icons.edit,
      title: $_('assistant.rename_chat'),
      advice: conversation.title,
      onconfirm: (title) => {
        if (title !== undefined) void changeAssistantConversation(conversation.id, title);
      }
    });
  }

  /**
   * Confirm deletion of the selected conversation and its saved records.
   */
  function remove(): void {
    const conversation = $assistant.conversation;
    if (!conversation) return;
    confirm({
      icon: icons.delete,
      title: $_('assistant.delete_chat'),
      message: $_('assistant.delete_chat_confirm'),
      onconfirm: () => void changeAssistantConversation(conversation.id)
    });
  }
</script>

{#snippet record(conversation: AssistantConversation)}
  <span class="min-w-0 flex-1">
    <span class="block truncate text-sm">{conversation.title || $_('assistant.new_chat')}</span>
    <time datetime={conversation.updated_at} class="mt-0.5 block truncate text-xs text-base-content/60">
      {$dateTime(conversation.updated_at)}
    </time>
  </span>
  <span class="flex size-4 shrink-0 items-center justify-center">
    {#if $assistant.conversation?.id === conversation.id}
      <iconify-icon icon={icons.checkmark} width="1rem" class="size-4" aria-hidden="true"></iconify-icon>
      <span class="sr-only">{$_('assistant.current_chat')}</span>
    {:else if managing && conversation.busy}
      <span class="loading loading-xs loading-spinner" aria-hidden="true"></span>
    {/if}
    {#if managing && conversation.busy}
      <span class="sr-only">{$_('assistant.status.streaming')}</span>
    {/if}
  </span>
{/snippet}

<div
  class="flex shrink-0 items-center gap-1 px-3 pb-1"
  aria-busy={$assistant.historyLoading || $assistant.conversationsLoading}
>
  <Dropdown
    bind:open={historyOpen}
    closeOnClick={false}
    closeOnOutsideClick={!confirming}
    class="min-w-0 flex-1"
    triggerClass="block min-w-0 cursor-pointer rounded-field text-base-content/60 transition-colors hover:text-base-content active:scale-100! focus-visible:text-base-content focus-visible:outline-2 focus-visible:outline-primary"
    contentWidth="min(20rem, calc(100vw - 3rem))"
    contentMaxHeight="min(25rem, calc(var(--ks-svh) - 8rem))"
    contentClass="flex flex-col overflow-hidden! scale-100! animate-none transition-none shadow-lg!"
    onclick={showHistory}
  >
    {#snippet trigger()}
      <span class="flex min-h-8 min-w-0 items-center gap-1.5 px-1 text-sm font-medium">
        {#if $assistantRestoring}
          <span class="loading loading-xs loading-spinner" aria-hidden="true"></span>
        {:else}
          <iconify-icon icon={icons.chatMultiple} width="1rem" class="size-4 shrink-0" aria-hidden="true"
          ></iconify-icon>
        {/if}
        <span class="sr-only">{$_('assistant.history')}:</span>
        <span class="min-w-0 flex-1 truncate text-left"
          >{$assistantRestoring
            ? $_('assistant.loading_history')
            : $assistant.conversation?.title || $_('assistant.new_chat')}</span
        >
      </span>
    {/snippet}
    <div class="flex shrink-0 items-center justify-between gap-2 border-b py-1.5 pr-1.5 pl-3">
      <span class="text-xs font-medium" aria-live="polite">
        {managing ? $_('assistant.chats_selected', selected.length) : $_('assistant.history')}
      </span>
      {#if managing || $assistant.conversations.length}
        <button
          type="button"
          class="btn h-8 min-h-8 btn-subtle px-2 text-xs"
          disabled={locked || confirming || $assistant.conversationsLoading}
          onclick={() => {
            managing = !managing;
            selectedIds = [];
          }}>{$_(managing ? 'message.cancel' : 'assistant.manage_history')}</button
        >
      {/if}
    </div>
    <ul
      class="menu min-h-0 w-full flex-nowrap gap-0.5 overflow-y-auto menu-sm p-1.5"
      aria-label={$_('assistant.history')}
    >
      {#each $assistant.conversations as conversation (conversation.id)}
        <li class="w-full min-w-0">
          {#if managing}
            <label
              class="flex w-full min-w-0 items-center gap-2.5 rounded-field px-2.5 py-2 text-base-content hover:bg-base-content/10 has-disabled:cursor-not-allowed has-disabled:opacity-50 {selected.includes(
                conversation.id
              )
                ? 'bg-base-content/5'
                : ''}"
              title={conversation.title || $_('assistant.new_chat')}
            >
              <input
                type="checkbox"
                class="checkbox shrink-0 checkbox-xs"
                checked={selected.includes(conversation.id)}
                disabled={locked || confirming || conversation.busy}
                aria-label={$_('assistant.select_chat', conversation.title || $_('assistant.new_chat'))}
                onchange={(event) => select(conversation.id, event.currentTarget.checked)}
              />
              {@render record(conversation)}
            </label>
          {:else}
            <button
              type="button"
              class="flex w-full min-w-0 items-center gap-3 bg-transparent px-2.5 py-2 text-left text-base-content shadow-none hover:bg-base-content/10 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary"
              title={conversation.title || $_('assistant.new_chat')}
              aria-current={$assistant.conversation?.id === conversation.id ? 'true' : undefined}
              disabled={locked}
              onclick={() => open(conversation.id)}
            >
              {@render record(conversation)}
            </button>
          {/if}
        </li>
      {/each}
      {#if !$assistant.conversations.length && !$assistant.conversationsLoading}
        <li class="px-2 py-3 text-xs text-base-content/60">{$_('assistant.no_history')}</li>
      {/if}
      {#if $assistant.conversationsLoading}
        <li class="pointer-events-none">
          <div class="flex justify-center py-2" role="status" aria-label={$_('assistant.loading_history')}>
            <span class="loading loading-xs loading-spinner" aria-hidden="true"></span>
          </div>
        </li>
      {:else if $assistant.conversationsBefore}
        <li>
          <button
            type="button"
            class="justify-center"
            disabled={locked || confirming || $assistant.conversationsLoading}
            onclick={() => void loadAssistantConversations(true)}>{$_('assistant.load_older')}</button
          >
        </li>
      {/if}
    </ul>
    {#if managing}
      {#if $assistant.error}
        <p class="px-3 py-2 text-xs text-error" role="alert">{$_(`alert.${$assistant.error}`)}</p>
      {/if}
      {#if selected.length > 999}
        <p class="px-3 py-2 text-xs text-error" role="alert">{$_('assistant.delete_chats_limit', 999)}</p>
      {/if}
      <div class="flex shrink-0 items-center justify-between gap-2 border-t px-1.5 py-2">
        <label class="flex min-h-8 cursor-pointer items-center gap-2.5 px-2.5 text-xs text-base-content/60">
          <input
            type="checkbox"
            class="checkbox checkbox-xs"
            checked={allSelected}
            indeterminate={selected.length > 0 && !allSelected}
            disabled={locked || confirming || !selectableIds.length}
            onchange={(event) => (selectedIds = event.currentTarget.checked ? [...selectableIds] : [])}
          />
          {$_('assistant.select_loaded')}
        </label>
        <button
          type="button"
          class="btn h-8 min-h-8 gap-1 px-2 text-xs btn-error"
          disabled={locked || confirming || !selected.length || selected.length > 999}
          onclick={removeSelected}
        >
          {#if $assistant.historyLoading}
            <span class="loading loading-xs loading-spinner" aria-hidden="true"></span>
          {:else}
            <iconify-icon icon={icons.delete} width="0.875rem" aria-hidden="true"></iconify-icon>
          {/if}
          {$_('assistant.delete_chats', selected.length)}
        </button>
      </div>
    {/if}
  </Dropdown>
  {#if $assistant.conversation}
    <button
      type="button"
      class="btn btn-square btn-subtle btn-xs"
      disabled={locked}
      aria-label={$_('assistant.rename_chat')}
      onclick={rename}
    >
      <iconify-icon icon={icons.edit} width="0.875rem" aria-hidden="true"></iconify-icon>
    </button>
    <button
      type="button"
      class="btn btn-square btn-subtle btn-xs"
      disabled={locked || $assistant.conversation.busy}
      aria-label={$_('assistant.delete_chat')}
      onclick={remove}
    >
      <iconify-icon icon={icons.delete} width="0.875rem" aria-hidden="true"></iconify-icon>
    </button>
  {/if}
  {#if $assistant.conversation || $assistant.turns.length}
    <button
      type="button"
      class="btn btn-square btn-subtle btn-xs"
      disabled={locked}
      aria-label={$_('assistant.new_chat')}
      onclick={start}
    >
      <iconify-icon icon={icons.chatAdd} width="1rem" aria-hidden="true"></iconify-icon>
    </button>
  {/if}
</div>
{#if !$assistant.busy && ($assistant.stopping || $assistant.conversation?.busy)}
  <div class="flex items-center justify-between gap-2 px-4 pb-2 text-xs text-base-content/60" role="status">
    <span>{$_($assistant.stopping ? 'assistant.stopping' : 'assistant.remote_busy')}</span>
    <button
      type="button"
      class="btn btn-subtle btn-xs"
      disabled={locked || $assistant.stopping}
      onclick={() => void loadAssistantConversation($assistant.conversation!.id)}>{$_('assistant.refresh_chat')}</button
    >
  </div>
{/if}
