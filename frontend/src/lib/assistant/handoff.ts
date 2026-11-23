import { goto } from '$app/navigation';
import { page } from '$app/state';
import { _ } from '$lib/i18n';
import { normalizePathname } from '$lib/utils/url';
import { get, writable } from 'svelte/store';
import { assistantCanvas, assistantEditorRevision, readAssistantEditor } from './canvas';
import { workflowPath } from './navigation';

export type AssistantHandoff = {
  id: number;
  conversationId: number;
  workflowId: number;
  path: string;
  // `blocked` keeps the draft available after cancelled navigation or a designer that did not load
  phase: 'opening' | 'blocked';
};

type HandoffHooks = {
  canResume: (handoff: AssistantHandoff) => boolean;
  sendFollowUp: (path: string, prompt: string) => void;
};

// a created draft waiting for its own designer before the follow-up request builds it
export const assistantHandoff = writable<AssistantHandoff | null>(null);

let handoffRevision = 0;
let handoffTimer: ReturnType<typeof setTimeout> | undefined;
let hooks: HandoffHooks | null = null;

/**
 * Connect handoff resumption to conversation readiness and the chat sender.
 *
 * @param next - Readiness predicate and follow-up sender supplied by the chat layer.
 */
export function configureAssistantHandoff(next: HandoffHooks): void {
  hooks = next;
}

/**
 * Read the current handoff invalidation counter to detect intervening navigation.
 *
 * @returns Monotonic counter incremented whenever navigation cancels automatic opening.
 */
export function getHandoffRevision(): number {
  return handoffRevision;
}

/**
 * Forget a pending handoff so no later designer receives it.
 */
export function clearHandoff(): void {
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
export async function openCreatedWorkflow(conversationId: number, workflowId: number): Promise<void> {
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
export function resumeHandoff(): void {
  const handoff = get(assistantHandoff);
  if (!handoff || !hooks?.canResume(handoff) || get(assistantCanvas)) return;

  const designer = readAssistantEditor();
  if (designer?.resource_id !== handoff.workflowId || designer.route !== handoff.path || !designer.editable) return;

  clearHandoff();
  hooks.sendFollowUp(handoff.path, get(_)('assistant.create.prompt'));
}

// recheck a pending handoff when a designer finishes loading
assistantEditorRevision.subscribe(() => queueMicrotask(resumeHandoff));
