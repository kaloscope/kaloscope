import type { AssistantDestination, AssistantToolResult } from '$lib/types';

export const workflowPath = '/settings/workflows/graphs';

/**
 * Resolve a successful navigation or creation result against the application's route registry.
 *
 * @param result - Server tool result, still checked before presenting a link.
 * @returns An internal destination and its target, or null when the result is not registered.
 */
export function assistantDestination(
  result: AssistantToolResult
): { path: string; target: AssistantDestination } | null {
  if (!result.ok || !result.data) return null;
  let target: AssistantDestination | undefined;
  let path: unknown;
  if (result.name === 'navigate') {
    target = result.data.target as AssistantDestination | undefined;
    path = result.data.path;
  } else if (result.name === 'create_workflow_draft') {
    // a newly created draft opens only in its editable designer
    target = { page: 'workflow_designer', resource_id: result.data.workflow_id as number };
    path = result.data.editor_path;
  }
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
    paths = [`${workflowPath}/${target.resource_id}`];
    if (result.name === 'navigate') paths.push(`${workflowPath}/r/${target.resource_id}`);
  } else return null;
  const match = paths.find((item) => item === path);
  return match ? { path: match, target } : null;
}
