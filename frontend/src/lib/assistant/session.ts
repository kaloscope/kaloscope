import { token, user, watch } from '$lib/stores.svelte';
import { resetMediaBatchWatchers } from '$lib/utils/media-batches';
import { get } from 'svelte/store';
import { resetAssistantCanvas } from './canvas';
import { abortActiveChat } from './chat';
import { getAccountId, resetConversationState, setAccountId } from './conversations';
import { activeDeviceRequest } from './device';
import { clearHandoff } from './handoff';

export { sendAssistant, stopAssistant } from './chat';
export {
  assistant,
  assistantRestoring,
  changeAssistantConversation,
  closeAssistant,
  deleteAssistantConversations,
  loadAssistantConversation,
  loadAssistantConversations,
  loadAssistantModels,
  newAssistantConversation,
  openAssistant,
  toggleAssistant,
  type AssistantTool,
  type AssistantTurn
} from './conversations';
export { assistantHandoff, releaseAssistantHandoff } from './handoff';

/**
 * Clear account data and invalidate all late network callbacks.
 */
function resetSession(): void {
  get(activeDeviceRequest)?.finish(false);
  resetMediaBatchWatchers();
  resetAssistantCanvas();
  clearHandoff();
  abortActiveChat();
  resetConversationState();
}

let sessionToken = token.current;
let sessionUser = user.current;

// handle a token change before the user change of the same login
watch(
  () => [token.current, user.current] as const,
  ([currentToken, currentUser]) => {
    if (currentToken !== sessionToken) {
      sessionToken = currentToken;
      setAccountId(undefined);
      resetSession();
    }
    if (currentUser !== sessionUser) {
      sessionUser = currentUser;
      // the token already clears a new login; its first profile must not close a just-opened panel
      if (currentUser) {
        const accountId = getAccountId();
        if (currentUser.role !== 'admin' || (accountId !== undefined && accountId !== currentUser.id)) resetSession();
        setAccountId(currentUser.id);
      }
    }
  }
);
