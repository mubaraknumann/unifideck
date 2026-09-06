/**
 * useCloudSaveStatus — out-of-band cloud-save status for the
 * cloud-save button.
 *
 * Deliberately NOT folded into `useGameInfo` (the App-Details hot
 * path that must stay non-blocking). `get_cloud_save_status` can hit
 * the store's metadata/CLI, so it is fetched separately — the same
 * way `useGameUpdate` and `useGameSize` are kept off the render
 * path. Only enabled for GOG/Epic (the stores with cloud-save sync).
 *
 * Live-updates: subscribes to the four CLOUD_SYNC_* events and
 * refetches when one fires for this game, so the button repaints when
 * an auto-pull (or another device's push) completes.
 */
import { useCallback } from "react";
import { rpcRoutes } from "../api/rpc-routes";
import { useRPCQuery, type QueryState } from "../api/useRPC";
import { useEventBus } from "../api/event-bus-client";
import { Events } from "../types/events";
import { useStoreCapability } from "./useStoreCapability";

export interface SaveSnapshot {
  timestamp: number;
  file_count: number;
  total_bytes: number;
}

export interface CloudSaveStatus {
  supported: boolean;
  in_progress: boolean;
  auto_pull: boolean;
  auto_push: boolean;
  /** Native cloud support for this store (null = unknown). */
  cloud_supported: boolean | null;
  save_path: string | null;
  save_path_resolved: boolean;
  /** True when `save_path` came from a manual override the user can reset. */
  save_path_is_override: boolean;
  has_local_saves: boolean;
  local_snapshot: Partial<SaveSnapshot>;
  /** True/false when known, null when undeterminable without a download. */
  has_cloud_saves: boolean | null;
  remote_snapshot: SaveSnapshot | null;
  last_sync_ts: number;
  /** Best starting folder for the manual save-location picker. */
  browse_start: string;
}

export interface CloudSaveStatusResult extends QueryState<CloudSaveStatus> {
  /** False for stores without cloud-save sync — caller hides the button. */
  enabled: boolean;
}

export function useCloudSaveStatus(
  store: string | undefined,
  gameId: string | undefined,
): CloudSaveStatusResult {
  // Was a local `new Set(["gog", "epic"])`, duplicated verbatim in
  // PlayMeta.tsx. The backend derives this from the cloud-save strategies it
  // actually registers (audit register items 26/31).
  const supportsCloudSaves = useStoreCapability(store, "supports_cloud_saves");
  const enabled = !!store && !!gameId && supportsCloudSaves;
  const query = useRPCQuery<[string, string], CloudSaveStatus>(
    rpcRoutes.getCloudSaveStatus,
    [store ?? "", gameId ?? ""],
    { enabled },
  );

  const onSyncEvent = useCallback(
    (payload: Record<string, unknown>) => {
      if (payload.store === store && payload.game_id === gameId) {
        void query.refetch();
      }
    },
    [store, gameId, query],
  );

  useEventBus(Events.CLOUD_SYNC_DOWN_COMPLETE, onSyncEvent, [store, gameId]);
  useEventBus(Events.CLOUD_SYNC_DOWN_FAILED, onSyncEvent, [store, gameId]);
  useEventBus(Events.CLOUD_SYNC_UP_COMPLETE, onSyncEvent, [store, gameId]);
  useEventBus(Events.CLOUD_SYNC_UP_FAILED, onSyncEvent, [store, gameId]);
  useEventBus(Events.GAME_STOPPED, onSyncEvent, [store, gameId]);

  return { ...query, enabled };
}
