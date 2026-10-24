import type { AssistantDestination, AssistantToolResult } from '$lib/types';

export const workflowPath = '/settings/workflows/graphs';
export const mediaPath = '/medialibs';

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
  const id = target.resource_id;
  if (target.page === 'workflows') {
    return id == null && path === workflowPath ? { path, target } : null;
  }
  if (typeof id !== 'number' || !Number.isSafeInteger(id) || id <= 0 || typeof path !== 'string') return null;
  let valid = false;
  if (target.page === 'workflow_designer') {
    valid = path === `${workflowPath}/${id}` || (result.name === 'navigate' && path === `${workflowPath}/r/${id}`);
  } else if (target.page === 'media_library') {
    valid = path === `${mediaPath}/${id}`;
  } else if (target.page === 'media_item') {
    // the server resolves the item's own library into the path
    valid = new RegExp(`^${mediaPath}/[1-9]\\d*/${id}$`).test(path);
  }
  return valid ? { path, target } : null;
}
