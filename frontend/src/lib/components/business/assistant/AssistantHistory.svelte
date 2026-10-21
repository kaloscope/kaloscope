<script lang="ts">
  import {
    assistant,
    changeAssistantConversation,
    loadAssistantConversation,
    loadAssistantConversations,
    newAssistantConversation
  } from '$lib/assistant/session';
  import { Dropdown, confirm, prompt } from '$lib/components';
  import { closeDropdowns } from '$lib/components/common/interaction/Dropdown.svelte';
  import { _, dateTime } from '$lib/i18n';
  import { icons } from '$lib/icons';

  const locked = $derived($assistant.busy || $assistant.historyLoading);

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

<div
  class="flex shrink-0 items-center gap-1 px-3 pb-1"
  aria-busy={$assistant.historyLoading || $assistant.conversationsLoading}
>
  <Dropdown
    class="min-w-0 flex-1"
    triggerClass="block min-w-0 cursor-pointer rounded-field transition-none active:scale-100! focus-visible:outline-2 focus-visible:outline-primary"
    contentWidth="min(20rem, calc(100vw - 3rem))"
    contentMaxHeight="14rem"
    contentClass="scale-100! animate-none transition-none shadow-lg!"
    onclick={() => void loadAssistantConversations()}
  >
    {#snippet trigger()}
      <span class="flex min-h-8 min-w-0 items-center gap-1.5 px-1 text-xs">
        <iconify-icon icon={icons.chatMultiple} width="1rem" class="size-4 shrink-0" aria-hidden="true"></iconify-icon>
        <span class="sr-only">{$_('assistant.history')}:</span>
        <span class="min-w-0 flex-1 truncate text-left"
          >{$assistant.conversation?.title || $_('assistant.new_chat')}</span
        >
      </span>
    {/snippet}
    <ul class="menu w-full flex-nowrap gap-0.5 menu-sm p-1.5" aria-label={$_('assistant.history')}>
      {#each $assistant.conversations as conversation (conversation.id)}
        <li class="w-full min-w-0">
          <button
            type="button"
            class="flex w-full min-w-0 items-center gap-3 bg-transparent px-2.5 py-2 text-left text-base-content shadow-none hover:bg-base-content/10 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary"
            title={conversation.title || $_('assistant.new_chat')}
            aria-current={$assistant.conversation?.id === conversation.id ? 'true' : undefined}
            disabled={locked}
            onclick={() => open(conversation.id)}
          >
            <span class="min-w-0 flex-1">
              <span class="block truncate text-sm">{conversation.title || $_('assistant.new_chat')}</span>
              <time datetime={conversation.updated_at} class="mt-0.5 block truncate text-xs text-base-content/60">
                {$dateTime(conversation.updated_at)}
              </time>
            </span>
            <span class="flex size-4 shrink-0 items-center justify-center" aria-hidden="true">
              {#if $assistant.conversation?.id === conversation.id}
                <iconify-icon icon={icons.checkmark} width="1rem" class="size-4"></iconify-icon>
              {/if}
            </span>
          </button>
        </li>
      {/each}
      {#if !$assistant.conversations.length && !$assistant.conversationsLoading}
        <li class="px-2 py-3 text-xs text-base-content/60">{$_('assistant.no_history')}</li>
      {/if}
      {#if $assistant.conversationsBefore}
        <li>
          <button
            type="button"
            disabled={$assistant.conversationsLoading}
            onclick={() => void loadAssistantConversations(true)}>{$_('assistant.load_older')}</button
          >
        </li>
      {/if}
    </ul>
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
{#if $assistant.conversation?.busy && !$assistant.busy}
  <div class="flex items-center justify-between gap-2 px-4 pb-2 text-xs text-base-content/60" role="status">
    <span>{$_('assistant.remote_busy')}</span>
    <button
      type="button"
      class="btn btn-subtle btn-xs"
      disabled={locked}
      onclick={() => void loadAssistantConversation($assistant.conversation!.id)}>{$_('assistant.refresh_chat')}</button
    >
  </div>
{/if}
