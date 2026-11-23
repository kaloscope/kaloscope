import { api } from '$lib/api';
import { user } from '$lib/stores.svelte';
import type {
  AssistantConversation,
  AssistantPageContext,
  AssistantProposal,
  AssistantStoredContext,
  AssistantStreamEvent,
  Resp
} from '$lib/types';
import { watchMediaBatch } from '$lib/utils/media-batches';
import { get } from 'svelte/store';
import { v4 as uuidv4 } from 'uuid';
import { assistantCanvas, beginAssistantCanvas, endAssistantCanvas, updateAssistantCanvas } from './canvas';
import { captureAssistantContext } from './context';
import {
  assistant,
  assistantRestoring,
  bumpConversationRevision,
  cancelStopRecovery,
  isModelAvailable,
  loadAssistantConversation,
  loadAssistantConversations,
  openAssistant,
  recoverStoppedConversation,
  rememberConversation,
  type AssistantTool,
  type AssistantTurn
} from './conversations';
import { errorCode } from './errors';
import { clearHandoff, configureAssistantHandoff, getHandoffRevision, openCreatedWorkflow } from './handoff';
import { assistantDestination } from './navigation';
import { isAssistantReview, refreshDomainTarget } from './reviews';
import { consumeAssistantStream } from './stream';

const promptEncoder = new TextEncoder();

let active: AbortController | null = null;
let flushActiveText: (() => void) | null = null;

configureAssistantHandoff({
  canResume(handoff) {
    const state = get(assistant);
    return (
      state.conversation?.id === handoff.conversationId &&
      !state.busy &&
      !state.stopping &&
      !state.historyLoading &&
      state.modelsLoading === null &&
      !state.conversation.busy
    );
  },
  sendFollowUp(path, prompt) {
    void sendAssistant(path, undefined, prompt);
  }
});

/**
 * Abort any active generation stream when resetting the session.
 */
export function abortActiveChat(): void {
  flushActiveText = null;
  active?.abort();
  active = null;
}

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
 * Check whether a completed tool call has a standalone interaction in the panel.
 *
 * @param tool - Persisted or streamed tool call.
 * @returns Whether the result can finish a turn with no accompanying text.
 */
function hasInteraction(tool: AssistantTool): boolean {
  const result = tool.result;
  if (!result?.ok || !result.data) return false;
  if (isAssistantReview(result)) return true;
  switch (result.name) {
    case 'ask_user':
      return Array.isArray(result.data.questions);
    case 'review_workspace_change':
    case 'review_plan_replacement':
      return typeof result.data.operation_id === 'string';
    case 'review_download_plan_change':
      return typeof result.data.request_id === 'string';
    case 'navigate':
      return assistantDestination(result) !== null;
    default:
      return false;
  }
}

/**
 * Dispatch side-effect refreshes and local preference updates for a completed tool call.
 *
 * @param tool - Tool call whose result just arrived from the stream.
 */
function applyToolSideEffects(tool: AssistantTool): void {
  const event = tool.result;
  if (!event?.ok || !event.data) return;

  if (tool.name === 'create_download_plan' && event.data.created) {
    refreshDomainTarget({ kind: 'download_plan_created' });
  }
  if (event.name === 'set_personal_preference' && user.current?.preferences) {
    Object.assign(user.current.preferences, event.data);
  }
  // an applied or uncertain change refreshes the schedule page's current results
  if (tool.name === 'apply_workflow_schedule' && event.data.applied !== false) {
    refreshDomainTarget({ kind: 'workflow_job_changed' });
  }
  if (tool.name === 'apply_download_plan_change' && event.data.applied !== false) {
    refreshDomainTarget({ kind: 'download_plan_changed' });
  }
  if (tool.name === 'apply_download_task_action' && event.data.applied !== false) {
    refreshDomainTarget({ kind: 'download_task_changed' });
  }

  // refresh batches after execution and show scans, rescrapes or saved settings
  if (tool.name === 'apply_media_change' && event.data.applied !== false) {
    const { action, batch_id, item_id, library_id } = event.data;
    if (action === 'batch') {
      if (typeof batch_id === 'number') watchMediaBatch(batch_id);
    } else if (typeof item_id === 'number') {
      refreshDomainTarget({ kind: 'media_item_changed', item_id, library_id });
    } else {
      refreshDomainTarget({
        kind: action === 'configure' ? 'media_library_changed' : 'media_library_scan',
        library_id
      });
    }
  }
}

/**
 * Stop generation independently of the panel's visibility.
 */
export function stopAssistant(): void {
  if (!active) return;
  flushActiveText?.();
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
 * Submit only the new prompt and page snapshot to an owned persistent conversation.
 *
 * @param route - Current pathname used to capture the active page.
 * @param retryId - Turn whose prompt is sent again with fresh context; only an incomplete turn is linked as a retry.
 * @param handoffPrompt - Fixed follow-up request that builds a newly created draft, leaving the composed input intact.
 * @param answerTo - Accepted question whose selections supply a single continuation.
 * @returns A promise that resolves after completion, cancellation, or error.
 */
export async function sendAssistant(
  route: string,
  retryId?: string,
  handoffPrompt?: string,
  answerTo?: string
): Promise<void> {
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
    if (prompt.length > 32768 || promptEncoder.encode(prompt).length > 128 * 1024) {
      throw new Error('assistant_context_limit');
    }
    context = captureAssistantContext(route);
  } catch (error) {
    assistant.update((value) => ({ ...value, error: errorCode(error, 'assistant_context_invalid') }));
    return;
  }

  // a new request supersedes any created draft still waiting for its designer
  clearHandoff();
  const requestHandoffRevision = getHandoffRevision();
  bumpConversationRevision();

  const turn: AssistantTurn = {
    id: answerTo ? `answer-${answerTo}` : uuidv4(),
    is_answer_turn: !!answerTo || !!retry?.is_answer_turn || !!(retry?.request_id ?? retry?.id)?.startsWith('answer-'),
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
  // allow the backend's ten-minute turn deadline plus transport cleanup
  const deadline = setTimeout(timeout, 630000);
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
  let submittedContext: AssistantStoredContext | null = null;
  let created: number | null = null;
  let canvasUpdates = Promise.resolve();
  let pendingText = '';
  let flushTimer: ReturnType<typeof setTimeout> | undefined;

  /**
   * Publish a turn update only while its original account and request are current.
   *
   * @param change - Mutation applied to the current turn.
   * @param cloneTools - Whether to copy the `tools` array before mutating tool entries.
   */
  function update(change: (value: AssistantTurn) => void, cloneTools = false): void {
    if (active !== controller) return;

    assistant.update((value) => {
      const turns = value.turns.map((item) => {
        if (item.id !== turn.id) return item;
        // preserve existing tool object references across text deltas to avoid redundant child updates
        const current = cloneTools ? { ...item, tools: [...item.tools] } : { ...item };
        change(current);
        return current;
      });
      return { ...value, turns };
    });
  }

  /**
   * Commit buffered text deltas in one store update so rapid token bursts do not reparse Markdown per chunk.
   */
  function flushText(): void {
    if (flushTimer !== undefined) {
      clearTimeout(flushTimer);
      flushTimer = undefined;
    }
    if (!pendingText) return;
    const chunk = pendingText;
    pendingText = '';
    update((value) => {
      value.content += chunk;
    });
  }

  flushActiveText = flushText;

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
        if (typeof event.data.is_answer_turn === 'boolean') value.is_answer_turn = event.data.is_answer_turn;
      });
      return;
    }

    if (!started && event.event !== 'error') throw new Error('assistant_stream_invalid');

    if (event.event === 'text_delta') {
      pendingText += event.data.text;
      if (flushTimer === undefined) flushTimer = setTimeout(flushText, 16);
      return;
    }

    // flush pending text before structural events so tool offsets and completion checks see exact length
    flushText();

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

    const mutatesTools = event.event === 'tool_start' || event.event === 'tool_result';
    update((value) => {
      switch (event.event) {
        case 'tool_start':
          if (value.tools.some((tool) => tool.id === event.data.id)) throw new Error('assistant_stream_invalid');
          value.tools.push({ ...event.data, status: 'running', offset: value.content.length });
          break;
        case 'tool_result': {
          const index = value.tools.findIndex((tool) => tool.id === event.data.id && tool.name === event.data.name);
          const existing = index >= 0 ? value.tools[index] : undefined;
          if (!existing || existing.status !== 'running') throw new Error('assistant_stream_invalid');

          const updated: AssistantTool = {
            ...existing,
            result: event.data,
            status: event.data.ok ? 'complete' : 'error'
          };
          value.tools[index] = updated;
          applyToolSideEffects(updated);
          break;
        }
        case 'done':
          if (
            (!value.content.trim() && createdWorkflow(value) === null && !value.tools.some(hasInteraction)) ||
            value.tools.some((tool) => tool.status === 'running')
          ) {
            throw new Error('assistant_stream_invalid');
          }
          value.status = 'complete';
          if (
            value.tools.some(
              (entry) =>
                isAssistantReview(entry.result) || (entry.name === 'review_download_plan_change' && entry.result?.ok)
            )
          )
            window.dispatchEvent(new Event('assistant-review-changed'));
          break;
        case 'error':
          value.status = 'error';
          value.error = event.data.code;
          break;
      }
    }, mutatesTools);

    if (
      event.event === 'tool_result' &&
      event.data.name === 'preview_workflow_draft' &&
      event.data.ok &&
      submittedConversationId !== null &&
      submittedContext?.page_type === 'workflow_designer' &&
      submittedContext.snapshot_id
    ) {
      const preview = get(assistant)
        .turns.find((item) => item.id === turn.id)
        ?.tools.find((tool) => tool.id === event.data.id);

      if (preview && isSaveablePreview(preview)) {
        const proposalId = preview.result!.data!.proposal_id as number;
        const conversationId = submittedConversationId;
        const snapshotId = submittedContext.snapshot_id;

        // keep checkpoint reads ordered without blocking model events or heartbeat reads
        canvasUpdates = canvasUpdates.then(() =>
          updateAssistantCanvas(
            turn.id,
            conversationId,
            proposalId,
            snapshotId,
            recordAssistantProposal,
            (code) => recordCanvasError(turn.id, code),
            false
          )
        );
      }
    }

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
        ...(answerTo ? { answer_to: answerTo } : {}),
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
    if (terminal === 'done') await canvasUpdates;

    const completed =
      terminal === 'done' && active === controller
        ? get(assistant).turns.find((item) => item.id === turn.id && item.status === 'complete')
        : undefined;
    if (completed) created = createdWorkflow(completed);

    if (completed && submittedContext?.page_type === 'workflow_designer' && submittedContext.snapshot_id) {
      // the backend advances its snapshot chain only after successful previews
      const preview = completed.tools.filter(isSaveablePreview).at(-1);
      if (preview) {
        await updateAssistantCanvas(
          turn.id,
          conversation.id,
          preview.result!.data!.proposal_id as number,
          submittedContext.snapshot_id,
          recordAssistantProposal,
          (code) => recordCanvasError(turn.id, code),
          true
        );
      }
    }
  } catch (error) {
    flushText();
    update((value) => {
      // the server already completed this turn, so a later local failure must not make it retryable
      if (value.status === 'stopped' || value.status === 'complete') return;
      value.error = timedOut ? 'ai_timeout' : errorCode(error, 'assistant_stream_interrupted');
      value.status = value.error === 'assistant_stream_interrupted' ? 'interrupted' : 'error';
    });
  } finally {
    flushText();
    if (flushActiveText === flushText) flushActiveText = null;
    endAssistantCanvas(turn.id);
    disarm();
    update((value) => {
      if (!value.tools.some((tool) => tool.status === 'running')) return;
      value.tools = value.tools.map((tool) => (tool.status === 'running' ? { ...tool, status: 'interrupted' } : tool));
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
      } else if (
        answerTo &&
        submittedConversationId &&
        current.conversation?.id === submittedConversationId &&
        current.turns.some((item) => item.id === turn.id && item.status !== 'complete')
      ) {
        await loadAssistantConversation(submittedConversationId);
      } else {
        cancelStopRecovery();
        void loadAssistantConversations();
      }

      // leaving and returning to the original page still cancels automatic opening
      if (created !== null && submittedConversationId !== null && getHandoffRevision() === requestHandoffRevision) {
        void openCreatedWorkflow(submittedConversationId, created);
      }
    }
  }
}
