import { goto } from '$app/navigation';
import { page } from '$app/state';
import { api } from '$lib/api';
import { createLoading } from '$lib/helpers';
import { _ } from '$lib/i18n';
import { persisted, token, user } from '$lib/stores';
import { normalizePathname } from '$lib/utils/url';
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
import { derived, get, writable } from 'svelte/store';
import { v4 as uuidv4 } from 'uuid';
import { captureAssistantContext } from './context';
import {
  assistantCanvas,
  assistantEditorRevision,
  beginAssistantCanvas,
  endAssistantCanvas,
  finishAssistantCanvas,
  readAssistantEditor,
  resetAssistantCanvas
} from './canvas';
import { errorCode } from './errors';
import { assistantDestination, workflowPath } from './navigation';
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
  context: AssistantPageContext | AssistantStoredContext | null;
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
  stopping: boolean;
};
type AssistantHandoff = {
  id: number;
  conversationId: number;
  workflowId: number;
  path: string;
  // `blocked` keeps the draft available after cancelled navigation or a designer that did not load
  phase: 'opening' | 'blocked';
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
export const assistantRestoring = derived(
  assistant,
  (state) =>
    !state.conversation &&
    !state.turns.length &&
    ((!state.historyLoaded && state.conversationsLoading) || state.historyLoading)
);
const selectedConversations = persisted<Record<number, number | null>>('assistant-conversations');
let active: AbortController | null = null;
let modelRequest: AbortController | null = null;
let historyRequest: AbortController | null = null;
let listRequest: AbortController | null = null;
let stopRequest: AbortController | null = null;
let stopTimer: ReturnType<typeof setTimeout> | undefined;
let conversationRevision = 0;
// a created draft waiting for its own designer before the follow-up request builds it
export const assistantHandoff = writable<AssistantHandoff | null>(null);
let handoffRevision = 0;
let handoffTimer: ReturnType<typeof setTimeout> | undefined;

/**
 * Clear account data and invalidate all late network callbacks.
 */
function resetSession(): void {
  resetAssistantCanvas();
  cancelStopRecovery();
  clearHandoff();
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

/**
 * Cancel status checks when their selected conversation or account is replaced.
 */
function cancelStopRecovery(): void {
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
function recoverStoppedConversation(conversationId: number, requestId: string): void {
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
 * Find the workflow created, or replayed, by a completed reply.
 *
 * @param turn - Completed turn with its streamed tool results.
 * @returns The last created workflow ID with a registered designer path, or `null`.
 */
function createdWorkflow(turn: AssistantTurn): number | null {
  const tool = turn.tools.filter((item) => item.name === 'create_workflow_draft' && item.status === 'complete').at(-1);
  const destination = tool?.result && assistantDestination(tool.result);
  return destination ? (destination.target.resource_id as number) : null;
}

/**
 * Forget a pending handoff so no later designer receives it.
 */
function clearHandoff(): void {
  clearTimeout(handoffTimer);
  handoffTimer = undefined;
  assistantHandoff.set(null);
}

/**
 * Invalidate automatic opening after navigation and clear a handoff outside its draft.
 *
 * @param pathname - Page reached by a completed navigation.
 */
export function releaseAssistantHandoff(pathname: string): void {
  // also invalidate creating replies that have not established a handoff yet
  handoffRevision += 1;
  const handoff = get(assistantHandoff);
  if (handoff && normalizePathname(pathname) !== handoff.path) clearHandoff();
}

/**
 * Open the draft created by a completed reply, then build it once its designer is ready.
 *
 * Navigation passes through the designer's unsaved-edit and pending-decision guards. A
 * cancelled navigation or a designer that does not load leaves the empty draft to open later.
 *
 * @param conversationId - Conversation whose reply created the draft.
 * @param workflowId - Workflow ID recorded by the creation tool.
 * @returns A promise resolving after navigation has been attempted.
 */
async function openCreatedWorkflow(conversationId: number, workflowId: number): Promise<void> {
  clearHandoff();
  const id = ++handoffRevision;
  const path = `${workflowPath}/${workflowId}`;
  assistantHandoff.set({ id, conversationId, workflowId, path, phase: 'opening' });
  const block = () => assistantHandoff.update((value) => (value?.id === id ? { ...value, phase: 'blocked' } : value));
  // a decision pending on the current canvas must be resolved before leaving it
  if (get(assistantCanvas)) {
    block();
    return;
  }
  if (normalizePathname(page.url.pathname) !== path) {
    try {
      await goto(path);
    } catch {
      // a failed navigation is reported below like a cancelled one
    }
  }
  if (get(assistantHandoff)?.id !== id) return;
  if (normalizePathname(page.url.pathname) !== path) {
    block();
    return;
  }
  handoffTimer = setTimeout(block, 15000);
  resumeHandoff();
}

/**
 * Send the follow-up request once the created draft's own editable designer has loaded.
 *
 * The request captures that designer's real workflow ID, draft version, and canvas, so
 * a previous page's context or snapshot is never reused.
 */
function resumeHandoff(): void {
  const handoff = get(assistantHandoff);
  const state = get(assistant);
  if (!handoff || state.conversation?.id !== handoff.conversationId) return;
  if (
    state.busy ||
    state.stopping ||
    state.historyLoading ||
    state.modelsLoading !== null ||
    state.conversation.busy ||
    get(assistantCanvas)
  )
    return;
  const designer = readAssistantEditor();
  if (designer?.resource_id !== handoff.workflowId || designer.route !== handoff.path || !designer.editable) return;
  clearHandoff();
  void sendAssistant(handoff.path, undefined, get(_)('assistant.create.prompt'));
}

// recheck a pending handoff when a designer loads or the conversation becomes idle
assistantEditorRevision.subscribe(() => queueMicrotask(resumeHandoff));
assistant.subscribe(() => {
  if (get(assistantHandoff)) queueMicrotask(resumeHandoff);
});

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
 * Show the assistant and refresh its catalogs outside generation and history changes.
 */
export function openAssistant(): void {
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
 * Stop generation independently of the panel's visibility.
 */
export function stopAssistant(): void {
  if (!active) return;
  assistant.update((state) => ({
    ...state,
    stopping: state.turns.some((turn) => turn.status === 'streaming'),
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

/**
 * Submit only the new prompt and page snapshot to an owned persistent conversation.
 *
 * @param route - Current pathname used to capture the active page.
 * @param retryId - Turn whose prompt is sent again with fresh context; only an incomplete turn is linked as a retry.
 * @param handoffPrompt - Fixed follow-up request that builds a newly created draft, leaving the composed input intact.
 * @returns A promise that resolves after completion, cancellation, or error.
 */
export async function sendAssistant(route: string, retryId?: string, handoffPrompt?: string): Promise<void> {
  const state = get(assistant);
  if (
    state.busy ||
    state.stopping ||
    state.historyLoading ||
    get(assistantRestoring) ||
    state.conversation?.busy ||
    state.modelsLoading !== null
  )
    return;
  if (get(assistantCanvas)) return;
  const retry = retryId
    ? state.turns.find((turn) => turn.id === retryId && (turn.status !== 'complete' || turn.canvasError))
    : null;
  const prompt = (handoffPrompt ?? retry?.prompt ?? state.input).trim();
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
  // a new request supersedes any created draft still waiting for its designer
  clearHandoff();
  const requestHandoffRevision = handoffRevision;
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
    input: retry || handoffPrompt ? value.input : '',
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
  let submittedConversationId: number | null = null;
  let created: number | null = null;

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
          if (tool.name === 'create_download_plan' && event.data.ok && event.data.data?.created) {
            window.dispatchEvent(new Event('download-plan-created'));
          }
          // an applied or uncertain change refreshes the schedule page's current results
          if (tool.name === 'apply_workflow_schedule' && event.data.ok && event.data.data?.applied !== false) {
            window.dispatchEvent(new Event('workflow-job-changed'));
          }
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
    let submittedContext: AssistantStoredContext | null;
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
    } else {
      submittedContext = context;
    }
    submittedConversationId = conversation.id;
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
    const completed =
      terminal === 'done' && active === controller
        ? get(assistant).turns.find((item) => item.id === turn.id && item.status === 'complete')
        : undefined;
    if (completed) created = createdWorkflow(completed);
    if (completed && submittedContext?.page_type === 'workflow_designer' && submittedContext.snapshot_id) {
      // the backend advances its snapshot chain only after successful previews
      const preview = completed.tools.filter(isSaveablePreview).at(-1);
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
      const current = get(assistant);
      assistant.update((value) => ({ ...value, busy: false, compacting: false }));
      if (
        current.open &&
        current.stopping &&
        submittedConversationId !== null &&
        current.conversation?.id === submittedConversationId
      ) {
        recoverStoppedConversation(submittedConversationId, turn.id);
      } else {
        cancelStopRecovery();
        void loadAssistantConversations();
      }
      // leaving and returning to the original page still cancels automatic opening
      if (created !== null && submittedConversationId !== null && handoffRevision === requestHandoffRevision) {
        void openCreatedWorkflow(submittedConversationId, created);
      }
    }
  }
}
