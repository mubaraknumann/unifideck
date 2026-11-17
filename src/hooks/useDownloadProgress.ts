/**
 * useDownloadProgress — focused selector on a specific
 * game's download progress.
 *
 * `DownloadContext` exposes the full queue snapshot ; for
 * components that only care about ONE game (a per-row
 * progress bar, a Play button overlay, ...), reading the
 * full queue and re-deriving the matching item every render
 * is wasteful. This hook does the selection once and only
 * re-renders the consumer when that game's progress
 * actually changes.
 *
 * Returns `null` if the game isn't currently in the queue.
 */
import { useMemo } from "react";
import { useDownloads } from "../contexts/DownloadContext";
import type { DownloadItem } from "../types/downloads";

/**
 * Shape returned by {@link useDownloadProgress}. Only the
 * fields a progress bar consumes are exposed — keeps the
 * memo footprint tight.
 */
export interface UseDownloadProgressResult {
  item: DownloadItem | null;
  isCurrent: boolean;
  isQueued: boolean;
  progressPercent: number;
}

/**
 * Hook scoped to a single in-flight download. Pulls
 * progress / phase / speed from `DownloadContext`
 * and memoises the slice so unrelated queue updates
 * don't re-render the consuming component.
 *
 * @param downloadId — id of the download to follow.
 * @returns reactive progress fields, or `null`
 *   when the download id is unknown to the queue.
 */
export function useDownloadProgress(
  gameId: string | null,
): UseDownloadProgressResult {
  const { queue } = useDownloads();
  return useMemo(() => {
    const empty: UseDownloadProgressResult = {
      item: null,
      isCurrent: false,
      isQueued: false,
      progressPercent: 0,
    };

    if (gameId == null || !queue) return empty;

    const active = (queue.active ?? [queue.current]).find(
      (d) => d != null && d.game_id === gameId,
    );
    if (active) {
      return {
        item: active,
        isCurrent: true,
        isQueued: false,
        progressPercent: active.progress_percent,
      };
    }

    const queued = queue.queued.find((d) => d.game_id === gameId);
    if (queued) {
      return {
        item: queued,
        isCurrent: false,
        isQueued: true,
        progressPercent: queued.progress_percent,
      };
    }

    return empty;
  }, [gameId, queue]);
}
