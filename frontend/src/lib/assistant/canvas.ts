import { api } from '$lib/api';
import { alert } from '$lib/components';
import { trackGraphWrite, type GraphUpdate } from '$lib/components/business/flow/graphWrites';
import type { AssistantPageContext, AssistantProposal, FlowGraph, GraphValidation, Resp } from '$lib/types';
import { isHTTPError } from 'ky';
import { get, writable } from 'svelte/store';
import { snapshotAssistantCanvas } from './context';
import { errorCode } from './errors';

type Document = NonNullable<FlowGraph['draft']>;
type DesignerContext = Extract<AssistantPageContext, { page_type: 'workflow_designer' }>;
type Outcome = {
  proposal: AssistantProposal;
  // persisted metadata of exactly the saved version, omitted when the workflow is
  // gone, no longer readable, or already advanced by another write
  workflow: GraphUpdate | null;
};
type CanvasResult = {
  proposal: AssistantProposal;
  base_snapshot_id: number | null;
  current_version: number;
  editable: boolean;
  canvas: Document | null;
  validation: GraphValidation;
  // changes relative to the original captured canvas, including unsaved edits
  change_count: number;
  workflow: GraphUpdate | null;
};
type CanvasEditor = {
  read: () => DesignerContext | null;
  begin: () => void;
  apply: (document: Document) => void;
  restore: () => void;
  saved: (metadata: GraphUpdate) => void;
};
type CanvasState = {
  workflowId: number;
  turnId: string;
  // `failed` keeps a completed reply's edit locked so a transient load error can be retried
  phase: 'editing' | 'failed' | 'review' | 'saving' | 'reverting';
  changes: number;
  issues: number;
  error: string | null;
  // a confirmation may have reached the server and no terminal outcome is known yet
  uncertain: boolean;
  // the server recorded a conflict or failure, so the edit can only be reverted
  rejected: boolean;
};
type Edit = {
  editor: CanvasEditor;
  context: DesignerContext;
  turnId: string;
  controller: AbortController;
  stop: () => void;
  path?: string;
  snapshotId?: number;
  result?: CanvasResult;
  // set once a confirmation is sent, after which a pending record no longer proves that nothing was saved
  submitted: boolean;
  decision?: (proposal: AssistantProposal) => void;
  fail?: (code: string) => void;
};

export const assistantCanvas = writable<CanvasState | null>(null);
let editor: CanvasEditor | null = null;
let active: Edit | null = null;

/**
 * Compare authored canvas data without selection or measured node dimensions.
 *
 * @param left - First canvas.
 * @param right - Second canvas.
 * @returns Whether both canvases represent the same authored document.
 */
function sameCanvas(left: Document, right: Document): boolean {
  return JSON.stringify(snapshotAssistantCanvas(left)) === JSON.stringify(snapshotAssistantCanvas(right));
}

/**
 * Check whether a recorded outcome wrote, or already matched, the workflow draft.
 *
 * @param proposal - Recorded canvas edit.
 * @returns Whether the edit is durably part of the draft.
 */
function isSaved(proposal: AssistantProposal): boolean {
  return proposal.status === 'saved' || proposal.status === 'unchanged';
}

/**
 * Register the mounted designer as the only target for live assistant edits.
 *
 * @param surface - Callbacks owned by this designer instance.
 * @returns Cleanup that invalidates late responses when the designer unmounts.
 */
export function registerAssistantEditor(surface: CanvasEditor): () => void {
  editor = surface;
  return () => {
    if (active?.editor === surface) resetAssistantCanvas();
    if (editor === surface) editor = null;
  };
}

/**
 * Restore the original canvas and cancel work when its page or account disappears.
 */
export function resetAssistantCanvas(): void {
  const edit = active;
  if (edit) {
    release(edit);
    edit.stop();
  }
  assistantCanvas.set(null);
}

/**
 * Restore the original canvas without stopping generation that is already ending.
 *
 * Nothing is applied, cancelled, or saved, so earlier unsaved manual edits keep
 * their dirty state and normal auto-save resumes.
 *
 * @param edit - Live edit to close.
 */
function release(edit: Edit): void {
  active = null;
  edit.controller.abort();
  edit.editor.restore();
  assistantCanvas.set(null);
}

/**
 * Lock the original canvas only when the assistant starts a modifying tool.
 *
 * @param turnId - Live request identity, never a restored historical turn.
 * @param context - Canvas captured before the request started.
 * @param stop - Callback cancelling generation when the editor disappears.
 * @throws If newer local edits or an in-flight save invalidate the capture.
 */
export function beginAssistantCanvas(turnId: string, context: AssistantPageContext | null, stop: () => void): void {
  if (active?.turnId === turnId) return;
  // read-only designers keep their context for inspection but never lock for direct edits
  if (context?.page_type !== 'workflow_designer' || !context.editable) return;
  const current = editor?.read();
  if (
    active ||
    !current ||
    current.resource_id !== context.resource_id ||
    current.draft_version !== context.draft_version ||
    !sameCanvas(current.canvas, context.canvas)
  ) {
    throw new Error('assistant_canvas_changed');
  }
  const surface = editor!;
  surface.begin();
  active = { editor: surface, context, turnId, controller: new AbortController(), stop, submitted: false };
  assistantCanvas.set({
    workflowId: context.resource_id,
    turnId,
    phase: 'editing',
    changes: 0,
    issues: 0,
    error: null,
    uncertain: false,
    rejected: false
  });
}

/**
 * Read an edit's complete canvas within its mounted lifetime.
 *
 * @param edit - Current live edit with an owned API path.
 * @returns Authorized canvas data after checking request lifetime.
 */
async function readResult(edit: Edit): Promise<CanvasResult> {
  const { data } = await api
    .get(`${edit.path}/canvas`, { signal: edit.controller.signal, context: { silentErrors: true } })
    .json<Resp<CanvasResult>>();
  edit.controller.signal.throwIfAborted();
  if (active !== edit) throw new Error('assistant_canvas_changed');
  return data;
}

/**
 * Read only the recorded outcome, which stays available after workflow access is lost.
 *
 * @param edit - Current live edit with an owned API path.
 * @returns The authoritative recorded outcome.
 */
async function readOutcome(edit: Edit): Promise<Outcome> {
  const { data } = await api
    .get(`${edit.path}/outcome`, { signal: edit.controller.signal, context: { silentErrors: true } })
    .json<Resp<Outcome>>();
  edit.controller.signal.throwIfAborted();
  return data;
}

/**
 * Atomically close an open edit, or learn the outcome that already closed it.
 *
 * @param edit - Live edit whose displayed identity is known.
 * @param signal - Optional request lifetime.
 * @returns The authoritative recorded outcome.
 */
async function cancelEdit(edit: Edit, signal?: AbortSignal): Promise<Outcome> {
  const { workflow_id, base_version, digest } = edit.result!.proposal;
  const { data } = await api
    .post(`${edit.path}/cancel`, {
      json: { workflow_id, base_version, digest },
      signal,
      retry: 0,
      context: { silentErrors: true }
    })
    .json<Resp<Outcome>>();
  return data;
}

/**
 * Check whether a canvas load failure may succeed when retried with the same live edit.
 *
 * @param error - Load or application failure.
 * @returns Whether the failure came from the network or a temporary server condition.
 */
function isTransient(error: unknown): boolean {
  if (isHTTPError(error)) return error.response.status >= 500 || [408, 429].includes(error.response.status);
  return !(error instanceof Error && error.message.startsWith('assistant_'));
}

/**
 * Apply the final successful edit once after the model finishes its summary.
 *
 * @param turnId - Request that owns the canvas lock.
 * @param conversationId - Conversation owning the generated edit.
 * @param proposalId - Last successful preview's internal save checkpoint.
 * @param snapshotId - Original captured canvas identity, preserved across tool calls.
 * @param decision - Callback updating the conversation's recorded outcome.
 * @param fail - Callback reporting why a completed reply's edit was not applied.
 * @returns A promise resolving after the canvas is ready, retryable, or restored.
 */
export async function finishAssistantCanvas(
  turnId: string,
  conversationId: number,
  proposalId: number,
  snapshotId: number,
  decision: (proposal: AssistantProposal) => void,
  fail: (code: string) => void
): Promise<void> {
  const edit = active;
  if (!edit || edit.turnId !== turnId) return;
  edit.path = `assistant/conversations/${conversationId}/proposals/${proposalId}`;
  edit.snapshotId = snapshotId;
  edit.decision = decision;
  edit.fail = fail;
  await loadEdit(edit);
}

/**
 * Validate and apply a live edit, keeping transient failures retryable.
 *
 * @param edit - Live edit whose reply has completed.
 * @returns A promise resolving after applying, pausing for retry, or restoring the canvas.
 */
async function loadEdit(edit: Edit): Promise<void> {
  try {
    const result = await readResult(edit);
    if (!result.editable) throw new Error('assistant_canvas_readonly');
    if (
      result.proposal.status !== 'pending' ||
      result.proposal.workflow_id !== edit.context.resource_id ||
      result.base_snapshot_id !== edit.snapshotId ||
      result.current_version !== edit.context.draft_version ||
      result.proposal.base_version !== edit.context.draft_version
    ) {
      throw new Error('assistant_canvas_changed');
    }
    if (!result.canvas) throw new Error('assistant_canvas_invalid');
    const current = edit.editor.read();
    if (!current || !sameCanvas(current.canvas, edit.context.canvas)) throw new Error('assistant_canvas_changed');
    edit.result = result;
    edit.decision?.(result.proposal);
    // the count compares with the captured canvas, so a no-op leaves nothing to keep or revert
    if (!result.change_count) {
      release(edit);
      return;
    }
    edit.editor.apply(result.canvas);
    assistantCanvas.update(
      (state) =>
        state && {
          ...state,
          phase: 'review',
          changes: result.change_count,
          issues: result.validation.issues.length + result.validation.readiness.warning_count
        }
    );
  } catch (error) {
    if (active !== edit) return;
    if (!edit.result && isTransient(error)) {
      assistantCanvas.update((state) => state && { ...state, phase: 'failed', error: 'assistant_canvas_load_failed' });
      return;
    }
    // a stale or invalid edit can never apply to this canvas, so release it for a fresh request
    release(edit);
    edit.fail?.(errorCode(error, 'assistant_canvas_not_applied'));
  }
}

/**
 * Load a completed reply's edit again while its original canvas remains locked.
 *
 * @returns A promise resolving after applying, pausing for retry, or restoring the canvas.
 */
export async function retryAssistantCanvas(): Promise<void> {
  const edit = active;
  const state = get(assistantCanvas);
  if (!edit?.path || state?.phase !== 'failed') return;
  assistantCanvas.set({ ...state, phase: 'editing', error: null });
  await loadEdit(edit);
}

/**
 * Release a failed or cancelled generation without touching another live edit.
 *
 * @param turnId - Request that is ending.
 */
export function endAssistantCanvas(turnId: string): void {
  // the request is already ending, so a timeout is not reported as a user stop
  if (active?.turnId === turnId && get(assistantCanvas)?.phase === 'editing') release(active);
}

/**
 * Mark a durable save in the existing editor history without reloading the page.
 *
 * @param edit - Live edit that owns the saved canvas.
 * @param outcome - Saved outcome and any readable workflow metadata.
 */
function acceptSaved(edit: Edit, { proposal, workflow }: Outcome): void {
  // without exact metadata, mark only the saved version this canvas represents so a newer draft still conflicts
  edit.editor.saved(workflow ?? { id: proposal.workflow_id, draft_version: proposal.saved_version! });
  active = null;
  assistantCanvas.set(null);
  if (workflow) void trackGraphWrite(proposal.workflow_id, async () => workflow).catch(() => {});
}

/**
 * Resolve the live edit from an authoritative terminal outcome.
 *
 * @param edit - Live edit whose outcome was recorded by the server.
 * @param outcome - Recorded outcome other than `pending`.
 */
function settle(edit: Edit, outcome: Outcome): void {
  const { proposal } = outcome;
  edit.result = { ...edit.result!, proposal };
  edit.decision?.(proposal);
  if (isSaved(proposal)) {
    acceptSaved(edit, outcome);
    return;
  }
  if (proposal.status === 'cancelled') {
    // an earlier revert reached the server after its response was lost
    alert({ level: 'warning', message: 'assistant_canvas_already_reverted' });
    resetAssistantCanvas();
    return;
  }
  let error = 'assistant_canvas_save_failed';
  if (proposal.status === 'conflict' || proposal.error === 'flow_draft_conflict') error = 'assistant_canvas_conflict';
  else if (proposal.error === 'flow_not_editable') error = 'assistant_canvas_not_editable';
  else if (proposal.error === 'not_found') error = 'assistant_canvas_missing';
  else if (proposal.error === 'flow_graph_invalid') error = 'assistant_canvas_save_invalid';
  assistantCanvas.update((state) => state && { ...state, phase: 'review', error, uncertain: false, rejected: true });
}

/**
 * Restore the pre-assistant canvas, including its unsaved manual work.
 *
 * An edit that was never submitted, or whose save was rejected, is restored at once.
 * After a confirmation may have reached the server, only an atomic cancellation or a
 * recorded save resolves the race, so the canvas stays as shown until one is known.
 *
 * @returns A promise resolving after the canvas is restored, saved, or still awaiting a known outcome.
 */
export async function discardAssistantCanvas(): Promise<void> {
  const edit = active;
  const state = get(assistantCanvas);
  if (!edit || !state || state.phase === 'saving' || state.phase === 'reverting') return;
  if (!edit.result || !edit.path) {
    resetAssistantCanvas();
    return;
  }
  if (!edit.submitted || state.rejected) {
    resetAssistantCanvas();
    // record the decision only when the server confirms it, never as a local assumption
    void cancelEdit(edit)
      .then(({ proposal }) => edit.decision?.(proposal))
      .catch(() => {});
    return;
  }
  assistantCanvas.set({ ...state, phase: 'reverting', error: null });
  try {
    const outcome = await cancelEdit(edit, edit.controller.signal);
    if (active !== edit) return;
    edit.decision?.(outcome.proposal);
    if (isSaved(outcome.proposal)) {
      alert({ level: 'info', message: 'assistant_canvas_already_saved' });
      acceptSaved(edit, outcome);
    } else if (outcome.proposal.status === 'pending') {
      throw new Error('assistant_canvas_revert_failed');
    } else {
      resetAssistantCanvas();
    }
  } catch (error) {
    if (active === edit) {
      assistantCanvas.set({ ...state, phase: 'review', error: errorCode(error, 'assistant_canvas_revert_failed') });
    }
  }
}

/**
 * Save the visible edit once, recovering a lost response before reporting its outcome.
 *
 * @returns A promise resolving with the canvas saved, rejected, or awaiting a known outcome.
 */
export async function keepAssistantCanvas(): Promise<void> {
  const edit = active;
  const state = get(assistantCanvas);
  if (!edit?.result || !edit.path || state?.phase !== 'review' || state.rejected) return;
  assistantCanvas.set({ ...state, phase: 'saving', error: null });
  const earlier = edit.submitted;
  edit.submitted = true;
  let outcome: Outcome | undefined;
  let failure: unknown;
  try {
    // serialize with designer saves to the same workflow
    await trackGraphWrite(edit.context.resource_id, async (signal) => {
      const { workflow_id, base_version, digest } = edit.result!.proposal;
      const { data } = await api
        .post(`${edit.path}/confirm`, {
          json: { workflow_id, base_version, digest, canvas: edit.context.canvas },
          signal: AbortSignal.any([signal, edit.controller.signal]),
          retry: 0,
          context: { silentErrors: true }
        })
        .json<Resp<Outcome>>();
      outcome = data;
    });
  } catch (error) {
    failure = error;
  }
  if (active !== edit) return;
  if (!outcome) {
    // a validation or identity rejection proves that only this first attempt wrote nothing
    if (!earlier && isHTTPError(failure) && [400, 409, 422].includes(failure.response.status)) {
      edit.submitted = false;
      assistantCanvas.set({ ...state, phase: 'review', error: errorCode(failure, 'assistant_canvas_save_failed') });
      return;
    }
    try {
      outcome = await readOutcome(edit);
    } catch {
      // neither a failed read nor a missing response proves that the save failed
    }
    if (active !== edit) return;
  }
  if (!outcome || outcome.proposal.status === 'pending') {
    // a pending record only means no save has committed yet; the sent confirmation may still claim it
    assistantCanvas.set({
      ...state,
      phase: 'review',
      error: errorCode(failure, 'assistant_canvas_uncertain'),
      uncertain: true
    });
    return;
  }
  settle(edit, outcome);
}
