import { api } from '$lib/api';
import type { MediaItem, MediaProgress, Resp } from '$lib/types';

export const WATCHED_THRESHOLD = 80;

export async function loadMediaProgress(ids: number[]): Promise<Map<number, MediaProgress>> {
  const uniqueIds = [...new Set(ids.filter((id) => Number.isFinite(id)))];
  if (uniqueIds.length === 0) {
    return new Map();
  }
  const result = new Map<number, MediaProgress>();
  for (let offset = 0; offset < uniqueIds.length; offset += 999) {
    const resp = await api
      .post('media/progress/list', {
        json: { ids: uniqueIds.slice(offset, offset + 999) }
      })
      .json<Resp<MediaProgress[]>>();
    for (const progress of resp.data) {
      result.set(progress.media_id, progress);
    }
  }
  return result;
}

export function attachMediaProgress<T extends MediaItem>(items: T[], progresses: Map<number, MediaProgress>) {
  for (const item of items) {
    item.progress = progresses.get(item.id) ?? null;
  }
}

export function isWatched(progress: MediaProgress | null | undefined): boolean {
  return progress?.status === 'watched' || (progress?.percentage ?? 0) >= WATCHED_THRESHOLD;
}

export function hasProgress(progress: MediaProgress | null | undefined): boolean {
  return !!progress;
}
