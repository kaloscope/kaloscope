import type { FirstPlanExecution, PlanExecution } from '$lib/types';

/**
 * Describe a single observed check, retaining uncertainty in legacy receipts.
 * @param check - Latest attempt or first-execution receipt.
 * @param translate - Message formatter for the active locale.
 * @returns The submission count, a reason for zero submissions, or a pending state.
 */
export function describePlanExecution(
  check: PlanExecution | FirstPlanExecution,
  translate: (id: string, options?: { values?: Record<number, number>; default?: string }) => string
): string {
  const status = 'status' in check ? check.status : undefined;
  let stage = check.stage ?? status ?? 'unavailable';
  if (stage === 'completed') stage = check.added ? 'tasks_submitted' : 'unknown_reason';
  if (stage === 'unconfirmed' && check.added) stage = 'unconfirmed_count';
  return translate('assistant.plan.execution.' + stage, {
    values: { 0: check.added ?? 0, 1: check.errors ?? 0 },
    default: translate('assistant.plan.execution.unavailable')
  });
}
