import type { AssistantDestination, AssistantToolResult } from '$lib/types';

export const workflowPath = '/settings/workflows/graphs';

/**
 * Resolve a successful navigation result against the application's route registry.
 *
 * @param result - Server tool result, still checked before presenting a link.
 * @returns An internal destination or null when the result is not registered.
 */
export function assistantDestination(result: AssistantToolResult): string | null {
  if (result.name !== 'navigate' || !result.ok || !result.data) return null;
  const target = result.data.target as AssistantDestination | undefined;
  if (!target) return null;
  let paths: string[];
  if (target.page === 'workflows' && target.resource_id == null) {
    paths = [workflowPath];
  } else if (
    target.page === 'workflow_designer' &&
    typeof target.resource_id === 'number' &&
    Number.isSafeInteger(target.resource_id) &&
    target.resource_id > 0
  ) {
    paths = [`${workflowPath}/${target.resource_id}`, `${workflowPath}/r/${target.resource_id}`];
  } else return null;
  return paths.find((path) => path === result.data?.path) ?? null;
}
