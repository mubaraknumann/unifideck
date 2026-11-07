/**
 * useXboxSaveStatus: how an installed Xbox PC game's saves compare with the
 * cloud. Read-only: the Xodus runtime does all the syncing, inside the game,
 * so this has no Download or Upload to offer.
 *
 * Asks when the page opens and again when the game stops. The second ask skips
 * the backend's short cache, because the game may have just changed its saves.
 */
import { useCallback, useState } from "react";
import { useRPCQuery } from "../api";
import { rpcRoutes } from "../api/rpc-routes";
import { useEventBus } from "../api/event-bus-client";
import { Events } from "../types/events";
import { isXboxPcRow } from "../lib/xbox-pc-setup";
import type { Game } from "../types/api";

export type XboxSaveState =
  | "in_sync"
  | "waiting_upload"
  | "cloud_newer"
  | "cloud_removed"
  | "both_changed"
  | "unknown"
  | "no_saves"
  | "not_launched"
  | "running"
  | "offline";

export interface XboxSaveStatus {
  /** False for anything but an installed Xbox PC game: show no line. */
  applicable: boolean;
  state: XboxSaveState | "";
  waiting: number;
  checked_at: number;
}

export interface XboxSaveLine {
  /** Steam's own word for the state, when it has one. */
  token?: string;
  /** Our text, and the fallback for `token`. */
  key: string;
}

/**
 * The line for a state, or null when there is nothing to say: no saves yet,
 * or a game that has not run on this account. Everything the next launch
 * settles reads the same. The backend log keeps the real state.
 */
export function xboxSaveLine(
  state: XboxSaveStatus["state"] | undefined,
): XboxSaveLine | null {
  switch (state) {
    case "in_sync":
      return {
        key: "play.cloudSave.statusInSync",
        token: "#AppDetails_CloudStatus_Synchronized",
      };
    case "waiting_upload":
      return { key: "play.cloudSave.xboxWaitingUpload" };
    case "cloud_newer":
    case "cloud_removed":
    case "both_changed":
    case "running":
      return { key: "play.cloudSave.xboxWaitingLaunch" };
    case "unknown":
    case "offline":
      return { key: "play.cloudSave.checkConnection" };
    default:
      return null;
  }
}

/** Whether this row gets the Xbox PC save line instead of the store's. */
export function usesXboxSaveLine(
  game: Pick<Game, "store_tags" | "is_installed" | "installed">,
): boolean {
  return isXboxPcRow(game) && (game.installed ?? game.is_installed) === true;
}

export function useXboxSaveStatus(
  gameId: string | undefined,
): XboxSaveStatus | null {
  const [force, setForce] = useState(false);
  const query = useRPCQuery<[string, boolean], XboxSaveStatus>(
    rpcRoutes.getXboxPcSaveStatus,
    [gameId ?? "", force],
    { enabled: !!gameId },
  );
  const { refetch } = query;
  const onStopped = useCallback(
    (payload: Record<string, unknown>) => {
      if (payload.game_id !== gameId) return;
      // The first stop changes the argument, which refetches by itself.
      if (force) void refetch();
      else setForce(true);
    },
    [force, gameId, refetch],
  );
  useEventBus(Events.GAME_STOPPED, onStopped, [onStopped]);
  return query.data;
}
