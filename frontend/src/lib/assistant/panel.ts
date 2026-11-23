import type { AIModelSelection, AssistantDestination } from '$lib/types';
import { normalizePathname } from '$lib/utils/url';
import type { AssistantTool, AssistantTurn } from './conversations';
import type { AssistantTranslator } from './errors';
import { assistantDestination } from './navigation';

export type AssistantDestinationAction = {
  path: string;
  label: string;
  target: AssistantDestination;
  id?: string;
};

export type AssistantTurnSegment = { key: string; text: string } | { key: string; tools: AssistantTool[] };

/**
 * Encode a provider/model pair as a stable option key without JSON serialization.
 *
 * @param providerId - Configured provider ID.
 * @param modelId - Model identifier within that provider.
 * @returns Bijective key safe even when `modelId` contains colons.
 */
export function encodeModelKey(providerId: number, modelId: string): string {
  return `${providerId}:${modelId}`;
}

/**
 * Encode the selected model for the native `<select>` value binding.
 *
 * @param model - Active provider and model selection, or `null`.
 * @returns Option key or `null` when nothing is selected.
 */
export function encodeModelSelection(model: AIModelSelection | null): string | null {
  return model ? encodeModelKey(model.provider_id, model.model_id) : null;
}

/**
 * Parse a `<select>` option key back into an `AIModelSelection`.
 *
 * @param value - Option value produced by `encodeModelKey`.
 * @returns Validated selection, or `null` when malformed.
 */
export function decodeModelSelection(value: string): AIModelSelection | null {
  const sep = value.indexOf(':');
  if (sep <= 0) return null;
  const provider_id = Number(value.slice(0, sep));
  const model_id = value.slice(sep + 1);
  return Number.isSafeInteger(provider_id) && provider_id > 0 && model_id ? { provider_id, model_id } : null;
}

/**
 * Clamp floating panel coordinates inside the current visual viewport.
 *
 * @param x - Requested left coordinate.
 * @param y - Requested top coordinate.
 * @param panelWidth - Current rendered panel width.
 * @param panelHeight - Current rendered panel height.
 * @returns Coordinates kept at least 8px inside all viewport edges.
 */
export function clampPanelPosition(
  x: number,
  y: number,
  panelWidth: number,
  panelHeight: number
): { x: number; y: number } {
  const viewport = window.visualViewport;
  const left = viewport?.offsetLeft ?? 0;
  const top = viewport?.offsetTop ?? 0;
  const width = viewport?.width ?? window.innerWidth;
  const height = viewport?.height ?? window.innerHeight;
  return {
    x: Math.max(left + 8, Math.min(x, left + width - panelWidth - 8)),
    y: Math.max(top + 8, Math.min(y, top + height - panelHeight - 8))
  };
}

/**
 * Extract deduplicated, validated navigation actions offered by a turn's tools.
 *
 * @param turn - Most recent turn in the conversation.
 * @param currentPathname - Current browser pathname.
 * @param translate - Active reactive i18n formatter.
 * @returns Ordered destination actions that are not already open.
 */
export function turnDestinations(
  turn: AssistantTurn | undefined,
  currentPathname: string,
  translate: AssistantTranslator
): AssistantDestinationAction[] {
  const items: AssistantDestinationAction[] = [];
  const pathname = normalizePathname(currentPathname);
  for (const { result } of turn?.tools ?? []) {
    const destination = result && assistantDestination(result);
    if (!destination) continue;
    const { path, target } = destination;
    const device = target.page === 'resume_reading' || target.page === 'play_video';
    if (
      result?.data?.device_outcome === 'opened' ||
      (path === pathname && !device) ||
      items.some((item) => item.path === path && item.target.page === target.page)
    )
      continue;
    const name = translate(`assistant.pages.${target.page}`);
    const label = device
      ? name
      : translate('action.open', target.resource_id ? `${name} #${target.resource_id}` : name);
    items.push({ path, label, target, id: result?.name === 'navigate' ? result.id : undefined });
  }
  return items;
}

/**
 * Find creations whose first result still belongs in this reply.
 *
 * @param turn - Current or restored turn, including older creation receipts.
 * @returns Distinct plan IDs to follow independently of subsequent checks.
 */
export function followedPlans(turn: AssistantTurn): number[] {
  const ids: number[] = [];
  for (const tool of turn.tools) {
    const data = tool.result?.data;
    if (
      tool.name !== 'create_download_plan' ||
      !tool.result?.ok ||
      !data?.created ||
      data.already_exists ||
      typeof data.plan_id !== 'number'
    )
      continue;
    const receipt = data.first_execution;
    const olderPending =
      data.follow_first_check === undefined &&
      receipt &&
      typeof receipt === 'object' &&
      'status' in receipt &&
      ['queued', 'waiting', 'unconfirmed'].includes(String(receipt.status));
    if ((data.follow_first_check || data.show_first_check || olderPending) && !ids.includes(data.plan_id)) {
      ids.push(data.plan_id);
    }
  }
  return ids;
}

/**
 * Choose which consecutive tool calls can share a collapsed activity entry.
 *
 * @param tool - Tool call as displayed in the conversation.
 * @returns A group key, or `null` for a call that needs its own visible status.
 */
function activityGroup(tool: AssistantTool): string | null {
  if (tool.status !== 'complete') return null;
  // keep search results and plan checks identifiable beside their previews
  if (['search_indexer', 'validate_download_plan', 'discover_actions'].includes(tool.name)) return tool.name;
  if (/^(get|list|read|validate|inspect|diagnose)_/.test(tool.name) || tool.name === 'probe_http_request') {
    return 'lookup';
  }
  return null;
}

/**
 * Split a reply into text and tool runs in the order they were streamed.
 *
 * @param turn - Conversation turn with tool start offsets.
 * @returns Text segments and consecutive tool groups with stable keys.
 */
export function turnSegments(turn: AssistantTurn): AssistantTurnSegment[] {
  const parts: AssistantTurnSegment[] = [];
  let offset = 0;

  for (const tool of turn.tools) {
    const text = turn.content.slice(offset, tool.offset);
    if (text.trim()) parts.push({ key: `text-${offset}`, text });
    offset = Math.max(offset, tool.offset);
    const previous = parts.at(-1);
    const group = activityGroup(tool);
    if (group && previous && 'tools' in previous && group === activityGroup(previous.tools[0])) {
      previous.tools.push(tool);
    } else {
      parts.push({ key: tool.id, tools: [tool] });
    }
  }

  const rest = turn.content.slice(offset);
  if (rest.trim()) parts.push({ key: `text-${offset}`, text: rest });
  return parts;
}

/**
 * Describe the captured page before or after its snapshot upload.
 *
 * @param context - Snapshot captured when the turn was sent.
 * @param translate - Active reactive i18n formatter.
 * @returns A short localized label with the resource and draft state.
 */
export function contextLabel(context: NonNullable<AssistantTurn['context']>, translate: AssistantTranslator): string {
  const parts = [translate(`assistant.pages.${context.page_type}`)];
  if (context.page_type === 'workflow_designer') {
    parts.push(`#${context.resource_id}`, translate(context.unsaved ? 'assistant.unsaved' : 'assistant.saved'));
  } else if (context.page_type === 'media_library' || context.page_type === 'media_item') {
    parts.push(`#${context.resource_id}`);
  }
  return parts.join(' · ');
}
