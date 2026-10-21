import { api } from '$lib/api';
import { createLoading } from '$lib/helpers';
import { token, user } from '$lib/stores';
import type {
  AIModelSelection,
  AssistantMessage,
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
  prompt: string;
  context: AssistantPageContext | null;
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
  error: string | null;
  busy: boolean;
};

/**
 * Create an empty in-memory account session.
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
    error: null,
    busy: false
  };
}

export const assistant = writable<AssistantState>(initialState());
let active: AbortController | null = null;
let modelRequest: AbortController | null = null;

/**
 * Clear account data and invalidate all late network callbacks.
 */
function resetSession(): void {
  active?.abort();
  modelRequest?.abort();
  active = null;
  modelRequest = null;
  assistant.set(initialState());
}

token.subscribe(resetSession);
let accountId = get(user)?.id;
user.subscribe((value) => {
  // a missing profile can mean a refresh; token changes already clear signed-out sessions
  if (value && accountId !== value.id) {
    accountId = value.id;
    resetSession();
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
  if (!get(assistant).busy) void loadAssistantModels();
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
  if (get(assistant).busy) return;
  assistant.update((state) => ({ ...state, turns: [], error: null }));
}

/**
 * Send completed turns with their original snapshots and historical tool results.
 *
 * Ignore new submissions while a reply is streaming or the model catalog is loading.
 *
 * @param route - Current pathname used to select the active context reader.
 * @param retryId - Incomplete turn to replace with a new attempt and fresh context.
 * @returns A promise that resolves after the attempt completes, stops, or reports an error.
 */
export async function sendAssistant(route: string, retryId?: string): Promise<void> {
  const state = get(assistant);
  if (state.busy || state.modelsLoading !== null) return;
  const retry = retryId ? state.turns.find((turn) => turn.id === retryId && turn.status !== 'complete') : null;
  const prompt = (retry?.prompt ?? state.input).trim();
  if (!prompt) return;
  let context: AssistantPageContext | null;
  const history = state.turns
    .filter((turn) => turn.status === 'complete')
    .flatMap<AssistantMessage>((turn) => [
      { role: 'user', content: turn.prompt, context: turn.context },
      {
        role: 'assistant',
        content: turn.content,
        tool_results: turn.tools.flatMap((tool) => (tool.result ? [tool.result] : []))
      }
    ]);
  const messages: AssistantMessage[] = [...history, { role: 'user', content: prompt }];
  try {
    if (!isModelAvailable(state.model, state.models)) throw new Error('assistant_model_required');
    if (
      messages.length > 32 ||
      messages.some((message) => message.content.length > 32768) ||
      messages.reduce((size, message) => size + new TextEncoder().encode(message.content).length, 0) > 128 * 1024 ||
      new TextEncoder().encode(
        JSON.stringify(
          history.map((message) =>
            message.role === 'user' ? { context: message.context } : { tool_results: message.tool_results }
          )
        )
      ).length >
        256 * 1024
    ) {
      throw new Error('assistant_history_limit');
    }
    context = captureAssistantContext(route);
  } catch (error) {
    assistant.update((value) => ({ ...value, error: errorCode(error, 'assistant_context_invalid') }));
    return;
  }
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
    turns: [...value.turns.filter((item) => item.id !== retry?.id), turn]
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
      return;
    }
    if (!started) throw new Error('assistant_stream_invalid');
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
    const response = await api.post('assistant/chat', {
      json: { model: state.model, messages, context },
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
      assistant.update((value) => ({ ...value, busy: false }));
    }
  }
}
