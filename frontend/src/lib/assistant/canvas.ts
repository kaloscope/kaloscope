import { api } from '$lib/api';
import { trackGraphWrite, type GraphUpdate } from '$lib/components/business/flow/graphWrites';
import type { AssistantPageContext, AssistantProposal, FlowGraph, GraphValidation, Resp } from '$lib/types';
import { get, writable } from 'svelte/store';
import { snapshotAssistantCanvas } from './context';
import { errorCode } from './errors';

type Document = NonNullable<FlowGraph['draft']>;
type DesignerContext = Extract<AssistantPageContext, { page_type: 'workflow_designer' }>;
type CanvasResult = {
  proposal: AssistantProposal;
  base_snapshot_id: number | null;
  current_version: number;
  editable: boolean;
  canvas: Document | null;
  validation: GraphValidation;
  change_count: number;
  workflow: GraphUpdate;
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
  phase: 'editing' | 'review' | 'saving';
  changes: number;
  issues: number;
  error: string | null;
  uncertain: boolean;
};
type Edit = {
  editor: CanvasEditor;
  context: DesignerContext;
  turnId: string;
  controller: AbortController;
  stop: () => void;
  path?: string;
  result?: CanvasResult;
  decision?: (proposal: AssistantProposal) => void;
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
  active = null;
  if (edit) {
    edit.controller.abort();
    edit.stop();
    edit.editor.restore();
  }
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
  if (context?.page_type !== 'workflow_designer' || context.route.includes('/graphs/r/')) return;
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
  active = { editor: surface, context, turnId, controller: new AbortController(), stop };
  assistantCanvas.set({
    workflowId: context.resource_id,
    turnId,
    phase: 'editing',
    changes: 0,
    issues: 0,
    error: null,
    uncertain: false
  });
}

/**
 * Read an edit's complete canvas or durable outcome within its mounted lifetime.
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
 * Apply the final successful edit once after the model finishes its summary.
 *
 * @param turnId - Request that owns the canvas lock.
 * @param conversationId - Conversation owning the generated edit.
 * @param proposalId - Last successful preview's internal save checkpoint.
 * @param snapshotId - Original captured canvas identity, preserved across tool calls.
 * @param decision - Callback updating the conversation's recorded outcome.
 * @returns A promise resolving after the canvas is ready to inspect.
 * @throws If validation, permissions, the base, or the mounted page changed.
 */
export async function finishAssistantCanvas(
  turnId: string,
  conversationId: number,
  proposalId: number,
  snapshotId: number,
  decision: (proposal: AssistantProposal) => void
): Promise<void> {
  const edit = active;
  if (!edit || edit.turnId !== turnId) return;
  edit.path = `assistant/conversations/${conversationId}/proposals/${proposalId}`;
  edit.decision = decision;
  const result = await readResult(edit);
  if (!result.editable) throw new Error('assistant_canvas_readonly');
  if (
    result.proposal.status !== 'pending' ||
    result.proposal.workflow_id !== edit.context.resource_id ||
    result.base_snapshot_id !== snapshotId ||
    result.current_version !== edit.context.draft_version ||
    result.proposal.base_version !== edit.context.draft_version
  ) {
    throw new Error('assistant_canvas_changed');
  }
  if (!result.canvas) throw new Error('assistant_canvas_invalid');
  const current = edit.editor.read();
  if (!current || !sameCanvas(current.canvas, edit.context.canvas)) throw new Error('assistant_canvas_changed');
  edit.result = result;
  decision(result.proposal);
  if (!result.change_count) {
    discardAssistantCanvas();
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
}

/**
 * Release a failed or cancelled generation without touching another live edit.
 *
 * @param turnId - Request that is ending.
 */
export function endAssistantCanvas(turnId: string): void {
  if (active?.turnId === turnId && get(assistantCanvas)?.phase === 'editing') resetAssistantCanvas();
}

/**
 * Restore the pre-assistant canvas, including its unsaved manual work.
 */
export function discardAssistantCanvas(): void {
  const edit = active;
  const state = get(assistantCanvas);
  if (!edit || state?.phase === 'saving' || state?.uncertain) return;
  const result = edit.result;
  if (result && edit.path) {
    const { workflow_id, base_version, digest } = result.proposal;
    // cancelling an unaccepted checkpoint never writes the workflow draft
    void api
      .post(`${edit.path}/cancel`, {
        json: { workflow_id, base_version, digest },
        retry: 0,
        context: { silentErrors: true }
      })
      .catch(() => {});
    edit.decision?.({ ...result.proposal, status: 'cancelled' });
  }
  resetAssistantCanvas();
}

/**
 * Mark a confirmed save in the existing editor history without reloading the page.
 *
 * @param edit - Live edit that owns the saved canvas.
 * @param result - Durable save outcome and its corresponding metadata.
 * @returns Whether the result represents a successful save.
 */
function acceptSaved(edit: Edit, result: CanvasResult): boolean {
  if (!['saved', 'unchanged'].includes(result.proposal.status)) return false;
  edit.decision?.(result.proposal);
  edit.editor.saved(result.workflow);
  active = null;
  assistantCanvas.set(null);
  return true;
}

/**
 * Save the visible edit once, recovering a lost response before allowing reversal.
 *
 * @returns A promise resolving with the canvas saved or a visible retryable error.
 */
export async function keepAssistantCanvas(): Promise<void> {
  const edit = active;
  const state = get(assistantCanvas);
  if (!edit?.result || !state || state.phase !== 'review') return;
  assistantCanvas.set({ ...state, phase: 'saving', error: null });
  let submitted = state.uncertain;
  try {
    await trackGraphWrite(edit.context.resource_id, async (signal) => {
      const current = await readResult(edit);
      if (acceptSaved(edit, current)) return current.workflow;
      if (current.proposal.status !== 'pending') throw new Error('assistant_canvas_conflict');
      const { workflow_id, base_version, digest } = current.proposal;
      submitted = true;
      await api.post(`${edit.path}/confirm`, {
        json: { workflow_id, base_version, digest, canvas: edit.context.canvas },
        signal: AbortSignal.any([signal, edit.controller.signal]),
        retry: 0,
        context: { silentErrors: true }
      });
      const result = await readResult(edit);
      edit.result = result;
      if (!acceptSaved(edit, result)) throw new Error('assistant_canvas_conflict');
      return result.workflow;
    });
  } catch (error) {
    if (active !== edit) return;
    let uncertain = submitted;
    if (submitted) {
      try {
        const result = await readResult(edit);
        if (acceptSaved(edit, result)) {
          await trackGraphWrite(edit.context.resource_id, async () => result.workflow);
          return;
        }
        edit.result = result;
        uncertain = false;
      } catch {
        // keep reversal disabled until the server's durable save outcome is known
      }
    }
    if (active === edit) {
      assistantCanvas.set({
        ...state,
        phase: 'review',
        error: uncertain ? 'assistant_canvas_uncertain' : errorCode(error, 'assistant_canvas_save_failed'),
        uncertain
      });
    }
  }
}
