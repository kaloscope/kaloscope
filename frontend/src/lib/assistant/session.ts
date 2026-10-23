import { api } from '$lib/api';
import { createLoading } from '$lib/helpers';
import { persisted, token, user } from '$lib/stores';
import type {
  AIModelSelection,
  AssistantConversation,
  AssistantStoredContext,
  AssistantModelProvider,
  AssistantPageContext,
  AssistantProposal,
  AssistantStreamEvent,
  AssistantToolResult,
  Resp
} from '$lib/types';
import { get, writable } from 'svelte/store';
import { v4 as uuidv4 } from 'uuid';
import { captureAssistantContext } from './context';
import {
  assistantCanvas,
  beginAssistantCanvas,
  endAssistantCanvas,
  finishAssistantCanvas,
  resetAssistantCanvas
} from './canvas';
import { errorCode } from './errors';
import { consumeAssistantStream } from './stream';

export { errorCode } from './errors';

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
  serverId?: number;
  prompt: string;
  context: AssistantStoredContext | null;
  content: string;
  status: 'streaming' | 'complete' | 'stopped' | 'interrupted' | 'error';
  error?: string;
  // a completed reply whose canvas edit could not be applied keeps its status
  canvasError?: string;
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
const selectedConversations = persisted<Record<number, number | null>>('assistant-conversations');
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
  resetAssistantCanvas();
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
 * Show the durable outcome of a canvas edit on its tool call.
 *
 * @param proposal - Recorded canvas edit returned by the server.
 */
function recordAssistantProposal(proposal: AssistantProposal): void {
  assistant.update((value) => {
    const turns = value.turns.map((turn) => {
      if (!turn.tools.some((tool) => tool.id === proposal.call_id)) return turn;
      const tools = turn.tools.map((tool) => {
        if (tool.id !== proposal.call_id) return tool;
        return { ...tool, proposal };
      });
      return { ...turn, tools };
    });
    return { ...value, turns };
  });
}

/**
 * Show why a completed reply's canvas edit was not applied without failing the reply.
 *
 * @param id - Local identity of the completed turn.
 * @param code - Public error code describing the stale or invalid edit.
 */
function recordCanvasError(id: string, code: string): void {
  assistant.update((value) => ({
    ...value,
    turns: value.turns.map((turn) => (turn.id === id ? { ...turn, canvasError: code } : turn))
  }));
}

/**
 * Check whether a tool call produced a saveable canvas edit.
 *
 * @param tool - Streamed tool call.
 * @returns Whether the call is a successful preview with an internal save checkpoint.
 */
function isSaveablePreview(tool: AssistantTool): boolean {
  return (
    tool.name === 'preview_workflow_draft' &&
    tool.status === 'complete' &&
    !!tool.result?.ok &&
    Number.isSafeInteger(tool.result.data?.proposal_id) &&
    (tool.result.data!.proposal_id as number) > 0
  );
}

/**
 * Remember the selected conversation for this account without storing its content.
 *
 * @param id - Conversation identifier, or `null` for an explicitly blank conversation.
 */
function rememberConversation(id: number | null): void {
  const ownerId = accountId;
  if (ownerId === undefined) return;
  selectedConversations.update((value) => ({ ...value, [ownerId]: id }));
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
 * Collapse the assistant only after its canvas edit has been resolved.
 */
export function closeAssistant(): void {
  if (get(assistantCanvas)) return;
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
    turns: state.turns.map((turn) => {
      if (turn.status !== 'streaming') return turn;
      const tools = turn.tools.map((tool) => {
        if (tool.status !== 'running') return tool;
        return { ...tool, status: 'interrupted' as const };
      });
      return { ...turn, status: 'stopped', tools };
    })
  }));
  active.abort();
}

/**
 * Start a fresh conversation while retaining the chosen model and composed input.
 */
export function newAssistantConversation(): void {
  if (get(assistantCanvas) || get(assistant).busy || get(assistant).historyLoading) return;
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
    if (restore && revision === conversationRevision && !get(assistant).conversation && !get(assistant).turns.length) {
      const selected = accountId === undefined ? undefined : get(selectedConversations)?.[accountId];
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
    (older && (!state.turnsBefore || state.conversation?.id !== id))
  )
    return;
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
  if (get(assistantCanvas) || get(assistant).busy || historyRequest) return;
  const controller = new AbortController();
  historyRequest = controller;
  const generation = accountGeneration;
  assistant.update((value) => ({ ...value, historyLoading: true, error: null }));
  try {
    const path = `assistant/conversations/${id}`;
    if (title === undefined) {
      await api.delete(path, { signal: controller.signal, retry: 0, context: { silentErrors: true } });
      if (generation !== accountGeneration) return;
      if (get(assistant).conversation?.id === id) rememberConversation(null);
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
 * @param retryId - Turn whose prompt is sent again with fresh context; only an incomplete turn is linked as a retry.
 * @returns A promise that resolves after completion, cancellation, or error.
 */
export async function sendAssistant(route: string, retryId?: string): Promise<void> {
  const state = get(assistant);
  if (state.busy || state.historyLoading || state.conversation?.busy || state.modelsLoading !== null) return;
  if (get(assistantCanvas)) return;
  const retry = retryId
    ? state.turns.find((turn) => turn.id === retryId && (turn.status !== 'complete' || turn.canvasError))
    : null;
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
  controller.signal.addEventListener('abort', () => endAssistantCanvas(turn.id), { once: true });
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
  let generating = true;
  const activity = () => {
    if (!generating) return;
    clearTimeout(idle);
    idle = setTimeout(timeout, 35000);
  };
  // the watchdogs guard model generation only; canvas loading has its own request lifetime
  const disarm = () => {
    generating = false;
    clearTimeout(deadline);
    clearTimeout(idle);
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
    if (event.event === 'tool_start' && event.data.name === 'preview_workflow_draft') {
      beginAssistantCanvas(turn.id, context, stopAssistant);
      // reveal the decision controls if the panel was closed before editing started
      if (get(assistantCanvas)) openAssistant();
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
    if (event.event === 'done' || event.event === 'error') disarm();
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
      rememberConversation(data.id);
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
        // a completed turn cannot be retried, so its prompt starts a fresh request
        ...(retry?.serverId && retry.status !== 'complete' ? { retry_of: retry.serverId } : {})
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
    const terminal = await consumeAssistantStream(response.body, receive, activity);
    if (terminal === 'done' && submittedContext?.page_type === 'workflow_designer' && submittedContext.snapshot_id) {
      const completed = get(assistant).turns.find((item) => item.id === turn.id);
      // the backend advances its snapshot chain only after successful previews
      const preview =
        active === controller && completed?.status === 'complete'
          ? completed.tools.filter(isSaveablePreview).at(-1)
          : undefined;
      if (preview) {
        await finishAssistantCanvas(
          turn.id,
          conversation.id,
          preview.result!.data!.proposal_id as number,
          submittedContext.snapshot_id,
          recordAssistantProposal,
          (code) => recordCanvasError(turn.id, code)
        );
      }
    }
  } catch (error) {
    update((value) => {
      // the server already completed this turn, so a later local failure must not make it retryable
      if (value.status === 'stopped' || value.status === 'complete') return;
      value.error = timedOut ? 'ai_timeout' : errorCode(error, 'assistant_stream_interrupted');
      value.status = value.error === 'assistant_stream_interrupted' ? 'interrupted' : 'error';
    });
  } finally {
    endAssistantCanvas(turn.id);
    disarm();
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
