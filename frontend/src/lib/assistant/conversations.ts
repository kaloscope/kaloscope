import { api } from '$lib/api';
import { createLoading } from '$lib/helpers';
import { persisted, user } from '$lib/stores.svelte';
import type {
  AIModelSelection,
  AssistantConversation,
  AssistantModelProvider,
  AssistantPageContext,
  AssistantProposal,
  AssistantStoredContext,
  AssistantToolResult,
  Resp
} from '$lib/types';
import { untrack } from 'svelte';
import { derived, get, writable } from 'svelte/store';
import { assistantCanvas } from './canvas';
import { errorCode } from './errors';
import { assistantHandoff, clearHandoff, resumeHandoff } from './handoff';

export type AssistantTool = {
  id: string;
  name: string;
  status: 'running' | 'complete' | 'error' | 'interrupted';
  // reply text length when the call started, preserving the streamed order of text and tools
  offset: number;
  result?: AssistantToolResult;
  // latest recorded decision for a preview; live previews start pending without it
  proposal?: AssistantProposal;
};

export type AssistantTurn = {
  id: string;
  request_id?: string;
  is_answer_turn?: boolean;
  serverId?: number;
  prompt: string;
  context: AssistantPageContext | AssistantStoredContext | null;
  content: string;
  status: 'streaming' | 'complete' | 'stopped' | 'interrupted' | 'error';
  error?: string;
  // a completed reply whose canvas edit could not be applied keeps its status
  canvasError?: string;
  tools: AssistantTool[];
};

export type AssistantState = {
  open: boolean;
  input: string;
  model: AIModelSelection | null;
  models: AssistantModelProvider[];
  modelsLoading: boolean | null;
  modelsLoaded: boolean;
  modelsError: string | null;
  turns: AssistantTurn[];
  conversation: AssistantConversation | null;
  conversations: AssistantConversation[];
  conversationsBefore: number | null;
  turnsBefore: number | null;
  historyLoaded: boolean;
  historyLoading: boolean;
  conversationsLoading: boolean;
  compacting: boolean;
  error: string | null;
  busy: boolean;
  stopping: boolean;
};

/**
 * Create an empty account view before loading persisted conversations.
 *
 * @returns Independent initial state with no retained conversation data.
 */
function initialState(): AssistantState {
  return {
    open: false,
    input: '',
    model: null,
    models: [],
    modelsLoading: null,
    modelsLoaded: false,
    modelsError: null,
    turns: [],
    conversation: null,
    conversations: [],
    conversationsBefore: null,
    turnsBefore: null,
    historyLoaded: false,
    historyLoading: false,
    conversationsLoading: false,
    compacting: false,
    error: null,
    busy: false,
    stopping: false
  };
}

export const assistant = writable<AssistantState>(initialState());

/**
 * Replace one recorded tool call in the loaded turns.
 *
 * @param id - Tool call to change.
 * @param change - Builds the updated call from the current one.
 */
export function updateAssistantTool(id: string, change: (tool: AssistantTool) => AssistantTool): void {
  assistant.update((value) => ({
    ...value,
    turns: value.turns.map((turn) =>
      turn.tools.some((tool) => tool.id === id)
        ? { ...turn, tools: turn.tools.map((tool) => (tool.id === id ? change(tool) : tool)) }
        : turn
    )
  }));
}

export const assistantRestoring = derived(
  assistant,
  (state) =>
    !state.conversation &&
    !state.turns.length &&
    ((!state.historyLoaded && state.conversationsLoading) || state.historyLoading)
);

const selectedConversations = persisted<Record<number, number | null>>('assistant-conversations');

let modelRequest: AbortController | null = null;
let historyRequest: AbortController | null = null;
let listRequest: AbortController | null = null;
let stopRequest: AbortController | null = null;
let stopTimer: ReturnType<typeof setTimeout> | undefined;
let conversationRevision = 0;
let accountId: number | undefined = user.current?.id;

// recheck a pending handoff when the conversation becomes idle
assistant.subscribe(() => {
  if (get(assistantHandoff)) queueMicrotask(resumeHandoff);
});

/**
 * Read the account ID currently bound to persisted conversation selection.
 *
 * @returns Active account ID, or `undefined` while signed out.
 */
export function getAccountId(): number | undefined {
  return accountId;
}

/**
 * Bind persisted conversation selection to the active signed-in account.
 *
 * @param id - Account identifier, or `undefined` when cleared.
 */
export function setAccountId(id: number | undefined): void {
  accountId = id;
}

/**
 * Advance the conversation revision so in-flight restores ignore superseded selections.
 */
export function bumpConversationRevision(): void {
  conversationRevision += 1;
}

/**
 * Abort all conversation/model requests and reset assistant store state.
 */
export function resetConversationState(): void {
  cancelStopRecovery();
  conversationRevision += 1;
  modelRequest?.abort();
  historyRequest?.abort();
  listRequest?.abort();
  historyRequest = null;
  listRequest = null;
  modelRequest = null;
  assistant.set(initialState());
}

/**
 * Cancel status checks when their selected conversation or account is replaced.
 */
export function cancelStopRecovery(): void {
  clearTimeout(stopTimer);
  stopTimer = undefined;
  stopRequest?.abort();
  stopRequest = null;
  assistant.update((state) => ({ ...state, stopping: false }));
}

/**
 * Recheck an explicitly stopped request until its server lease is released.
 *
 * Ten checks tolerate delayed disconnect cleanup without polling indefinitely.
 * Exhaustion preserves the last authoritative busy state for manual refresh.
 *
 * @param conversationId - Selected conversation receiving the stopped request.
 * @param requestId - Client request identity whose completion resolves this stop.
 */
export function recoverStoppedConversation(conversationId: number, requestId: string): void {
  cancelStopRecovery();
  // a late list response must not overwrite a newer authoritative status
  listRequest?.abort();
  listRequest = null;

  const controller = new AbortController();
  stopRequest = controller;
  assistant.update((state) => ({ ...state, stopping: true, conversationsLoading: false }));
  let attempts = 0;

  /**
   * Read the selected conversation without replacing its local stopped transcript.
   *
   * @returns A promise resolving after this check or scheduling the next one.
   */
  async function check(): Promise<void> {
    attempts += 1;

    try {
      const { data } = await api
        .get(`assistant/conversations/${conversationId}/turns`, {
          signal: controller.signal,
          timeout: 5000,
          retry: 0,
          context: { silentErrors: true }
        })
        .json<
          Resp<{
            conversation: AssistantConversation;
            items: { request_id: string; status: AssistantTurn['status'] }[];
          }>
        >();

      if (stopRequest !== controller || get(assistant).conversation?.id !== conversationId) return;

      assistant.update((state) => ({
        ...state,
        conversation: data.conversation,
        conversations: state.conversations.map((item) => (item.id === conversationId ? data.conversation : item))
      }));

      const stopped = data.items.find((item) => item.request_id === requestId);
      if (!data.conversation.busy || (stopped && stopped.status !== 'streaming')) cancelStopRecovery();
    } catch {
      // a failed status read never proves that the server stopped generating
    } finally {
      if (stopRequest === controller) {
        if (attempts < 10) stopTimer = setTimeout(() => void check(), 2000);
        else cancelStopRecovery();
      }
    }
  }

  stopTimer = setTimeout(() => void check(), 500);
}

/**
 * Remember the selected conversation for this account without storing its content.
 *
 * @param id - Conversation identifier, or `null` for an explicitly blank conversation.
 */
export function rememberConversation(id: number | null): void {
  const ownerId = accountId;
  if (ownerId === undefined) return;
  selectedConversations.current = untrack(() => ({ ...selectedConversations.current, [ownerId]: id }));
}

/**
 * Check whether the current catalog still contains a selected model.
 *
 * @param model - Selected provider and model, or `null` before selection.
 * @param providers - Available providers and their configured model IDs.
 * @returns Whether the selection is available for a new conversation turn.
 */
export function isModelAvailable(model: AIModelSelection | null, providers: AssistantModelProvider[]): boolean {
  return (
    !!model &&
    providers.some((provider) => provider.id === model.provider_id && provider.model_ids.includes(model.model_id))
  );
}

/**
 * Refresh choices, retaining only valid selections and delaying the spinner.
 *
 * @returns A promise that resolves after loading models or reporting a request error.
 */
export async function loadAssistantModels(): Promise<void> {
  if (modelRequest) return;

  const controller = new AbortController();
  modelRequest = controller;

  // use a request-local timer so a previous fast refresh cannot reveal this spinner early
  const loading = createLoading();
  const unsubscribe = loading.subscribe((modelsLoading) => {
    if (modelRequest === controller) assistant.update((state) => ({ ...state, modelsLoading }));
  });

  loading.start();
  assistant.update((state) => ({ ...state, modelsError: null }));

  try {
    const { data } = await api
      .get('assistant/models', {
        signal: controller.signal,
        context: { silentErrors: true }
      })
      .json<Resp<AssistantModelProvider[]>>();

    if (modelRequest !== controller) return;

    assistant.update((state) => {
      let model = state.model;
      if (model) {
        if (!isModelAvailable(model, data)) model = null;
      } else if (!state.models.length && data[0]) {
        model = { provider_id: data[0].id, model_id: data[0].model_ids[0] };
      }
      return { ...state, models: data, modelsLoaded: true, model };
    });
  } catch (error) {
    if (modelRequest === controller) {
      assistant.update((state) => ({ ...state, modelsError: errorCode(error, 'assistant_models_failed') }));
    }
  } finally {
    loading.end();
    unsubscribe();
    if (modelRequest === controller) {
      modelRequest = null;
    }
  }
}

/**
 * Show the assistant and refresh its catalogs outside generation and history changes.
 */
export function openAssistant(): void {
  if (user.current?.role !== 'admin') return;
  assistant.update((state) => ({ ...state, open: true }));
  if (!get(assistant).busy && !get(assistant).historyLoading) {
    void loadAssistantModels();
    void loadAssistantConversations(false, !get(assistant).historyLoaded);
  }
}

/**
 * Collapse the assistant only after its canvas edit has been resolved.
 */
export function closeAssistant(): void {
  if (get(assistantCanvas)) return;
  cancelStopRecovery();
  assistant.update((state) => ({ ...state, open: false }));
}

/**
 * Show or collapse the assistant from its global entry points.
 */
export function toggleAssistant(): void {
  if (get(assistant).open) closeAssistant();
  else openAssistant();
}

/**
 * Start a fresh conversation while retaining the chosen model and composed input.
 */
export function newAssistantConversation(): void {
  if (get(assistantCanvas) || get(assistant).busy || get(assistant).historyLoading) return;
  cancelStopRecovery();
  clearHandoff();
  conversationRevision += 1;
  rememberConversation(null);
  assistant.update((state) => ({
    ...state,
    conversation: null,
    turns: [],
    turnsBefore: null,
    error: null
  }));
}

/**
 * Load account-owned metadata and optionally restore the last selected conversation.
 *
 * @param older - Whether to append the next page instead of refreshing.
 * @param restore - Whether to restore the account's selection, falling back to the latest chat when none was recorded.
 * @returns A promise that resolves after updating metadata or reporting an error.
 */
export async function loadAssistantConversations(older = false, restore = false): Promise<void> {
  const state = get(assistant);
  if (stopRequest || listRequest || (older && !state.conversationsBefore)) return;

  const controller = new AbortController();
  listRequest = controller;
  const revision = conversationRevision;
  assistant.update((value) => ({ ...value, conversationsLoading: true }));

  try {
    const { data } = await api
      .get('assistant/conversations', {
        searchParams: older ? { before: state.conversationsBefore! } : {},
        signal: controller.signal,
        context: { silentErrors: true }
      })
      .json<Resp<{ items: AssistantConversation[]; next_before: number | null }>>();

    if (listRequest !== controller) return;

    assistant.update((value) => ({
      ...value,
      conversations: older ? [...value.conversations, ...data.items] : data.items,
      conversationsBefore: data.next_before,
      historyLoaded: true,
      conversation: value.conversation
        ? (data.items.find((item) => item.id === value.conversation!.id) ?? value.conversation)
        : null
    }));

    if (restore && revision === conversationRevision && !get(assistant).conversation && !get(assistant).turns.length) {
      const selected = accountId === undefined ? undefined : selectedConversations.current?.[accountId];
      const id =
        typeof selected === 'number' && Number.isSafeInteger(selected) && selected > 0 ? selected : data.items[0]?.id;
      if (selected !== null && id !== undefined) await loadAssistantConversation(id);
    }
  } catch (error) {
    if (listRequest === controller)
      assistant.update((value) => ({ ...value, error: errorCode(error, 'assistant_history_failed') }));
  } finally {
    if (listRequest === controller) {
      listRequest = null;
      assistant.update((value) => ({ ...value, conversationsLoading: false }));
    }
  }
}

/**
 * Restore a conversation or prepend an older page without resending its history.
 *
 * @param id - Owned conversation identifier.
 * @param older - Whether to load earlier turns in the current conversation.
 * @returns A promise that resolves after loading the page or reporting an error.
 */
export async function loadAssistantConversation(id: number, older = false): Promise<void> {
  const state = get(assistant);
  if (
    get(assistantCanvas) ||
    state.busy ||
    historyRequest ||
    (older && (state.stopping || !state.turnsBefore || state.conversation?.id !== id))
  )
    return;

  if (!older) {
    cancelStopRecovery();
    clearHandoff();
  }

  const controller = new AbortController();
  historyRequest = controller;
  if (!older) conversationRevision += 1;
  assistant.update((value) => ({ ...value, historyLoading: true, error: null }));

  try {
    const { data } = await api
      .get(`assistant/conversations/${id}/turns`, {
        searchParams: older ? { before: state.turnsBefore! } : {},
        signal: controller.signal,
        context: { silentErrors: true }
      })
      .json<Resp<{ conversation: AssistantConversation; items: AssistantTurn[]; next_before: number | null }>>();

    if (historyRequest !== controller) return;

    const items = data.items.map((turn) => ({ ...turn, serverId: Number(turn.id) }));
    assistant.update((value) => {
      let model = value.model;
      if (!older && (!value.modelsLoaded || isModelAvailable(data.conversation.model, value.models))) {
        model = data.conversation.model;
      }
      return {
        ...value,
        conversation: data.conversation,
        turns: older ? [...items, ...value.turns] : items,
        turnsBefore: data.next_before,
        model
      };
    });

    if (!older) rememberConversation(data.conversation.id);
  } catch (error) {
    if (historyRequest === controller) {
      const code = errorCode(error, 'assistant_history_failed');
      if (!older && code === 'not_found') rememberConversation(null);
      assistant.update((value) => ({ ...value, error: code }));
    }
  } finally {
    if (historyRequest === controller) {
      historyRequest = null;
      assistant.update((value) => ({ ...value, historyLoading: false }));
    }
  }
}

/**
 * Rename or delete an idle owned conversation while preserving the composed input.
 *
 * @param id - Conversation whose metadata or history is being changed.
 * @param title - New title; omission requests deletion.
 * @returns A promise that resolves after the operation or a localized error.
 */
export async function changeAssistantConversation(id: number, title?: string): Promise<void> {
  if (title === undefined) {
    await deleteAssistantConversations([id]);
    return;
  }

  if (get(assistantCanvas) || get(assistant).busy || historyRequest) return;

  cancelStopRecovery();
  const controller = new AbortController();
  historyRequest = controller;
  assistant.update((value) => ({ ...value, historyLoading: true, error: null }));

  try {
    if (!title.trim() || title.trim().length > 120) throw new Error('assistant_title_invalid');

    const { data } = await api
      .patch(`assistant/conversations/${id}`, {
        json: { title: title.trim() },
        signal: controller.signal,
        retry: 0,
        context: { silentErrors: true }
      })
      .json<Resp<AssistantConversation>>();

    if (historyRequest !== controller) return;

    assistant.update((value) => (value.conversation?.id === id ? { ...value, conversation: data } : value));
    await loadAssistantConversations();
  } catch (error) {
    if (historyRequest === controller)
      assistant.update((value) => ({ ...value, error: errorCode(error, 'assistant_history_failed') }));
  } finally {
    if (historyRequest === controller) {
      historyRequest = null;
      assistant.update((value) => ({ ...value, historyLoading: false }));
    }
  }
}

/**
 * Delete selected conversations together while preserving other history and the composed input.
 *
 * @param ids - Owned conversation IDs selected for deletion.
 * @returns Whether the batch was deleted before the account changed.
 */
export async function deleteAssistantConversations(ids: number[]): Promise<boolean> {
  const state = get(assistant);
  if (get(assistantCanvas) || state.busy || historyRequest || !ids.length) return false;

  const selected = new Set(ids);
  const controller = new AbortController();
  historyRequest = controller;
  conversationRevision += 1;

  // discard in-flight metadata pages so they cannot restore deleted records
  listRequest?.abort();
  listRequest = null;
  assistant.update((value) => ({ ...value, historyLoading: true, conversationsLoading: false, error: null }));

  try {
    await api.post('assistant/conversations/delete', {
      json: { ids: [...selected] },
      signal: controller.signal,
      retry: 0,
      context: { silentErrors: true }
    });

    if (historyRequest !== controller) return false;

    const current = get(assistant).conversation;
    if (current && selected.has(current.id)) {
      cancelStopRecovery();
      clearHandoff();
      rememberConversation(null);
    }

    assistant.update((value) => ({
      ...value,
      conversations: value.conversations.filter((item) => !selected.has(item.id)),
      ...(value.conversation && selected.has(value.conversation.id)
        ? { conversation: null, turns: [], turnsBefore: null }
        : {})
    }));

    return true;
  } catch (error) {
    if (historyRequest === controller) {
      assistant.update((value) => ({ ...value, error: errorCode(error, 'assistant_history_failed') }));
      await loadAssistantConversations();
    }
    return false;
  } finally {
    if (historyRequest === controller) {
      historyRequest = null;
      assistant.update((value) => ({ ...value, historyLoading: false }));
    }
  }
}
