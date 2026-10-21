import { api } from '$lib/api';
import { createLoading } from '$lib/helpers';
import { token, user } from '$lib/stores';
import type {
  AIModelSelection,
  AssistantConversation,
  AssistantStoredContext,
  AssistantModelProvider,
  AssistantPageContext,
  AssistantStreamEvent,
  AssistantToolResult,
  Resp
} from '$lib/types';
import { isHTTPError } from 'ky';
import { get, writable } from 'svelte/store';
import { v4 as uuidv4 } from 'uuid';
import { captureAssistantContext } from './context';
import { consumeAssistantStream } from './stream';

export type AssistantTool = {
  id: string;
  name: string;
  status: 'running' | 'complete' | 'error' | 'interrupted';
  // reply text length when the call started, preserving the streamed order of text and tools
  offset: number;
  result?: AssistantToolResult;
};
export type AssistantTurn = {
  id: string;
  serverId?: number;
  prompt: string;
  context: AssistantStoredContext | null;
  content: string;
  status: 'streaming' | 'complete' | 'stopped' | 'interrupted' | 'error';
  error?: string;
  tools: AssistantTool[];
};
type AssistantState = {
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
    busy: false
  };
}

export const assistant = writable<AssistantState>(initialState());
let active: AbortController | null = null;
let modelRequest: AbortController | null = null;
let historyRequest: AbortController | null = null;
let listRequest: AbortController | null = null;
let accountGeneration = 0;
let conversationRevision = 0;

/**
 * Clear account data and invalidate all late network callbacks.
 */
function resetSession(): void {
  accountGeneration += 1;
  conversationRevision += 1;
  active?.abort();
  modelRequest?.abort();
  historyRequest?.abort();
  listRequest?.abort();
  historyRequest = null;
  listRequest = null;
  active = null;
  modelRequest = null;
  assistant.set(initialState());
}

let accountId = get(user)?.id;
token.subscribe(() => {
  accountId = undefined;
  resetSession();
});
user.subscribe((value) => {
  // the token already clears a new login; its first profile must not close a just-opened panel
  if (value) {
    if (accountId !== undefined && accountId !== value.id) resetSession();
    accountId = value.id;
  }
});

/**
 * Convert request failures to existing translated application error codes.
 *
 * @param error - Transport or application failure.
 * @param fallback - Safe localized code when no public code is available.
 * @returns A public error code without upstream bodies.
 */
function errorCode(error: unknown, fallback: string): string {
  if (isHTTPError(error)) {
    const message = (error.data as { message?: unknown } | undefined)?.message;
    if (typeof message === 'string' && /^[a-z][a-z0-9_]+$/.test(message)) return message;
  }
  if (error instanceof Error && error.message.startsWith('assistant_')) return error.message;
  return fallback;
}

/**
 * Check whether the current catalog still contains a selected model.
 *
 * @param model - Selected provider and model, or `null` before selection.
 * @param providers - Available providers and their configured model IDs.
 * @returns Whether the selection is available for a new conversation turn.
 */
function isModelAvailable(model: AIModelSelection | null, providers: AssistantModelProvider[]): boolean {
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
 * Show the assistant and refresh available models while no reply is streaming.
 */
export function openAssistant(): void {
  assistant.update((state) => ({ ...state, open: true }));
  if (!get(assistant).busy) {
    void loadAssistantModels();
    void loadAssistantConversations(false, !get(assistant).historyLoaded);
  }
}

/**
 * Collapse the assistant while leaving generation and composed text intact.
 */
export function closeAssistant(): void {
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
 * Stop generation independently of the panel's visibility.
 */
export function stopAssistant(): void {
  if (!active) return;
  assistant.update((state) => ({
    ...state,
    turns: state.turns.map((turn) =>
      turn.status === 'streaming'
        ? {
            ...turn,
            status: 'stopped',
            tools: turn.tools.map((tool) => (tool.status === 'running' ? { ...tool, status: 'interrupted' } : tool))
          }
        : turn
    )
  }));
  active.abort();
}

/**
 * Start a fresh conversation while retaining the chosen model and composed input.
 */
export function newAssistantConversation(): void {
  if (get(assistant).busy || get(assistant).historyLoading) return;
  conversationRevision += 1;
  assistant.update((state) => ({ ...state, conversation: null, turns: [], turnsBefore: null, error: null }));
}

/**
 * Load account-owned conversation metadata and optionally restore the latest chat.
 *
 * @param older - Whether to append the next page instead of refreshing.
 * @param restore - Whether an empty account session should reopen its latest chat.
 * @returns A promise that resolves after updating metadata or reporting an error.
 */
export async function loadAssistantConversations(older = false, restore = false): Promise<void> {
  const state = get(assistant);
  if (listRequest || (older && !state.conversationsBefore)) return;
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
    if (
      restore &&
      revision === conversationRevision &&
      !get(assistant).conversation &&
      !get(assistant).turns.length &&
      data.items[0]
    ) {
      await loadAssistantConversation(data.items[0].id);
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
  if (state.busy || historyRequest || (older && (!state.turnsBefore || state.conversation?.id !== id))) return;
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
    assistant.update((value) => ({
      ...value,
      conversation: data.conversation,
      turns: older ? [...items, ...value.turns] : items,
      turnsBefore: data.next_before,
      model: older
        ? value.model
        : !value.modelsLoaded || isModelAvailable(data.conversation.model, value.models)
          ? data.conversation.model
          : value.model
    }));
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
 * Rename or delete an idle owned conversation while preserving the composed input.
 *
 * @param id - Conversation whose metadata or history is being changed.
 * @param title - New title; omission requests deletion.
 * @returns A promise that resolves after the operation or a localized error.
 */
export async function changeAssistantConversation(id: number, title?: string): Promise<void> {
  if (get(assistant).busy || historyRequest) return;
  const controller = new AbortController();
  historyRequest = controller;
  const generation = accountGeneration;
  assistant.update((value) => ({ ...value, historyLoading: true, error: null }));
  try {
    const path = `assistant/conversations/${id}`;
    if (title === undefined) {
      await api.delete(path, { signal: controller.signal, retry: 0, context: { silentErrors: true } });
      if (generation !== accountGeneration) return;
      assistant.update((value) =>
        value.conversation?.id === id ? { ...value, conversation: null, turns: [], turnsBefore: null } : value
      );
    } else {
      if (!title.trim() || title.trim().length > 120) throw new Error('assistant_title_invalid');
      const { data } = await api
        .patch(path, {
          json: { title: title.trim() },
          signal: controller.signal,
          retry: 0,
          context: { silentErrors: true }
        })
        .json<Resp<AssistantConversation>>();
      if (generation !== accountGeneration) return;
      assistant.update((value) => (value.conversation?.id === id ? { ...value, conversation: data } : value));
    }
    await loadAssistantConversations();
  } catch (error) {
    if (generation === accountGeneration)
      assistant.update((value) => ({ ...value, error: errorCode(error, 'assistant_history_failed') }));
  } finally {
    if (historyRequest === controller) {
      historyRequest = null;
      assistant.update((value) => ({ ...value, historyLoading: false }));
    }
  }
}

/**
 * Submit only the new prompt and page snapshot to an owned persistent conversation.
 *
 * @param route - Current pathname used to capture the active page.
 * @param retryId - Incomplete turn linked to a new attempt with fresh context.
 * @returns A promise that resolves after completion, cancellation, or error.
 */
export async function sendAssistant(route: string, retryId?: string): Promise<void> {
  const state = get(assistant);
  if (state.busy || state.historyLoading || state.conversation?.busy || state.modelsLoading !== null) return;
  const retry = retryId ? state.turns.find((turn) => turn.id === retryId && turn.status !== 'complete') : null;
  const prompt = (retry?.prompt ?? state.input).trim();
  if (!prompt) return;
  let context: AssistantPageContext | null;
  try {
    if (!isModelAvailable(state.model, state.models)) throw new Error('assistant_model_required');
    if (prompt.length > 32768 || new TextEncoder().encode(prompt).length > 128 * 1024) {
      throw new Error('assistant_context_limit');
    }
    context = captureAssistantContext(route);
  } catch (error) {
    assistant.update((value) => ({ ...value, error: errorCode(error, 'assistant_context_invalid') }));
    return;
  }
  conversationRevision += 1;
  const turn: AssistantTurn = {
    id: uuidv4(),
    prompt,
    context,
    content: '',
    status: 'streaming',
    tools: []
  };
  const controller = new AbortController();
  active = controller;
  assistant.update((value) => ({
    ...value,
    input: retry ? value.input : '',
    error: null,
    busy: true,
    turns: [...value.turns, turn]
  }));
  let timedOut = false;
  let idle: ReturnType<typeof setTimeout>;
  const timeout = () => {
    timedOut = true;
    controller.abort();
  };
  const deadline = setTimeout(timeout, 150000);
  const activity = () => {
    clearTimeout(idle);
    idle = setTimeout(timeout, 35000);
  };
  activity();
  let started = false;

  /**
   * Publish a turn update only while its original account and request are current.
   *
   * @param change - Mutation applied to the current turn.
   */
  function update(change: (value: AssistantTurn) => void): void {
    if (active !== controller) return;
    assistant.update((value) => {
      const turns = value.turns.map((item) => {
        if (item.id !== turn.id) return item;
        const current = { ...item, tools: item.tools.map((tool) => ({ ...tool })) };
        change(current);
        return current;
      });
      return { ...value, turns };
    });
  }

  /**
   * Apply stream events in protocol order and preserve failed tool outcomes.
   *
   * @param event - Validated stream event.
   */
  function receive(event: AssistantStreamEvent): void {
    controller.signal.throwIfAborted();
    if (event.event === 'start') {
      if (started) throw new Error('assistant_stream_invalid');
      started = true;
      update((value) => {
        value.serverId = Number(event.data.turn_id);
      });
      return;
    }
    if (!started && event.event !== 'error') throw new Error('assistant_stream_invalid');
    if (event.event === 'compacting') {
      if (active === controller)
        assistant.update((value) => ({
          ...value,
          compacting: event.data.active,
          conversation:
            value.conversation && event.data.compacted ? { ...value.conversation, compacted: true } : value.conversation
        }));
      return;
    }
    update((value) => {
      switch (event.event) {
        case 'text_delta':
          value.content += event.data.text;
          break;
        case 'tool_start':
          if (value.tools.some((tool) => tool.id === event.data.id)) throw new Error('assistant_stream_invalid');
          value.tools.push({ ...event.data, status: 'running', offset: value.content.length });
          break;
        case 'tool_result': {
          const tool = value.tools.find((tool) => tool.id === event.data.id && tool.name === event.data.name);
          if (!tool || tool.status !== 'running') throw new Error('assistant_stream_invalid');
          tool.result = event.data;
          tool.status = event.data.ok ? 'complete' : 'error';
          break;
        }
        case 'done':
          if (!value.content.trim() || value.tools.some((tool) => tool.status === 'running')) {
            throw new Error('assistant_stream_invalid');
          }
          value.status = 'complete';
          break;
        case 'error':
          value.status = 'error';
          value.error = event.data.code;
          break;
      }
    });
  }

  try {
    let conversation = state.conversation;
    if (!conversation) {
      const { data } = await api
        .post('assistant/conversations', {
          json: { model: state.model },
          signal: controller.signal,
          retry: 0,
          context: { silentErrors: true }
        })
        .json<Resp<AssistantConversation>>();
      if (active !== controller) return;
      conversation = data;
      assistant.update((value) => ({ ...value, conversation: data }));
    }
    let submittedContext: AssistantStoredContext | null = context;
    if (context?.page_type === 'workflow_designer') {
      const { canvas, ...metadata } = context;
      const { data } = await api
        .post(`assistant/conversations/${conversation.id}/snapshots`, {
          json: { workflow_id: context.resource_id, draft_version: context.draft_version, canvas },
          signal: controller.signal,
          retry: 0,
          context: { silentErrors: true }
        })
        .json<Resp<{ snapshot_id: number }>>();
      if (active !== controller) return;
      submittedContext = { ...metadata, snapshot_id: data.snapshot_id };
      update((value) => {
        value.context = submittedContext;
      });
      activity();
    }
    const response = await api.post(`assistant/conversations/${conversation.id}/chat`, {
      json: {
        model: state.model,
        prompt,
        request_id: turn.id,
        context: submittedContext,
        ...(retry?.serverId ? { retry_of: retry.serverId } : {})
      },
      signal: controller.signal,
      timeout: false,
      retry: 0,
      context: { silentErrors: true },
      headers: { Accept: 'text/event-stream' }
    });
    if (!response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body) {
      throw new Error('assistant_stream_invalid');
    }
    await consumeAssistantStream(response.body, receive, activity);
  } catch (error) {
    update((value) => {
      if (value.status === 'stopped') return;
      value.error = timedOut ? 'ai_timeout' : errorCode(error, 'assistant_stream_interrupted');
      value.status = value.error === 'assistant_stream_interrupted' ? 'interrupted' : 'error';
    });
  } finally {
    clearTimeout(deadline);
    clearTimeout(idle!);
    update((value) => {
      value.tools.forEach((tool) => {
        if (tool.status === 'running') tool.status = 'interrupted';
      });
    });
    if (active === controller) {
      active = null;
      assistant.update((value) => ({ ...value, busy: false, compacting: false }));
      void loadAssistantConversations();
    }
  }
}
