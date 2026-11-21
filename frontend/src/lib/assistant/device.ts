import { get, writable } from 'svelte/store';

export type DeviceAction = 'play_video' | 'resume_reading';
export type DeviceRequest = {
  ownerId: number;
  itemId: number;
  action: DeviceAction;
  finish: (opened: boolean) => void;
};
export const activeDeviceRequest = writable<DeviceRequest | null>(null);

/**
 * Await an explicit user click being handled by the matching media page on this device.
 * @param ownerId - Account that clicked the action.
 * @param itemId - Media item checked by the server.
 * @param action - Device action selected by the user.
 * @returns Whether the matching reader or player confirmed opening.
 */
export function requestDeviceAction(ownerId: number, itemId: number, action: DeviceAction): Promise<boolean> {
  get(activeDeviceRequest)?.finish(false);
  return new Promise((resolve) => {
    const request: DeviceRequest = { ownerId, itemId, action, finish };
    const timer = setTimeout(() => finish(false), 20000);

    /**
     * Settle this request and release its listener.
     * @param opened - Whether the target surface opened.
     */
    function finish(opened: boolean) {
      clearTimeout(timer);
      if (get(activeDeviceRequest) === request) activeDeviceRequest.set(null);
      resolve(opened);
    }
    activeDeviceRequest.set(request);
  });
}
