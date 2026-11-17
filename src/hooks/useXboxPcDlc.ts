/**
 * useXboxPcDlc: the DLC an Xbox PC game's account owns, loaded with the rest
 * of the game page (never from a library sync).
 *
 * Asks when the page opens, and again when a download ends, so a DLC that
 * just installed or failed shows its new state. The game page's DLC button,
 * the DLC modal and the install picker all read this one answer.
 */
import { useCallback } from "react";
import { useRPCQuery } from "../api";
import { rpcRoutes } from "../api/rpc-routes";
import { useEventBus } from "../api/event-bus-client";
import { Events } from "../types/events";
import { isXboxPcRow } from "../lib/xbox-pc-setup";
import { callXboxPc } from "../stores/xbox-pc-store";
import type { Game, XboxPcDlc, XboxPcDlcList } from "../types/api";

/** States that mean "chosen and not installed yet": Play shows Update. */
export const PENDING_DLC_STATES: ReadonlySet<XboxPcDlc["state"]> = new Set([
  "selected",
  "partial",
  "failed",
]);

/** Whether an installed game is waiting for DLC the user chose. */
export function hasPendingDlc(list: XboxPcDlcList | null | undefined): boolean {
  return (
    !!list?.installed && list.dlc.some((d) => PENDING_DLC_STATES.has(d.state))
  );
}

/** The list for a row, or an empty answer for anything but Xbox PC.
 *  `callXboxPc` unwraps the RPC envelope (a payload without a top-level
 *  `success` key arrives as `{success, data}`). */
export async function fetchXboxPcDlc(
  game: Pick<Game, "store_tags" | "store_game_id">,
): Promise<XboxPcDlcList> {
  const empty: XboxPcDlcList = { applicable: false, installed: false, dlc: [] };
  if (!isXboxPcRow(game)) return empty;
  const r = await callXboxPc<XboxPcDlcList>(
    rpcRoutes.getXboxPcDlc,
    game.store_game_id,
    false,
  );
  if (r.ok && r.data && Array.isArray(r.data.dlc)) return r.data;
  if (!r.ok) console.error("[useXboxPcDlc] DLC list not read:", r.error);
  return empty;
}

/** Tick DLC: every product id that should be installed (before or after
 *  the game is installed). */
export async function setXboxPcDlc(
  gameId: string,
  selected: string[],
): Promise<{ success: boolean; error?: string | null }> {
  const r = await callXboxPc<unknown>(rpcRoutes.setXboxPcDlc, gameId, selected);
  return r.ok ? { success: true } : { success: false, error: r.error };
}

export interface XboxPcDlcQuery {
  list: XboxPcDlcList | null;
  refetch: () => Promise<void>;
}

export function useXboxPcDlc(
  game: Pick<Game, "store_tags" | "store_game_id"> | null | undefined,
): XboxPcDlcQuery {
  const gameId = game && isXboxPcRow(game) ? game.store_game_id : "";
  const query = useRPCQuery<[string, boolean], XboxPcDlcList>(
    rpcRoutes.getXboxPcDlc,
    [gameId, false],
    { enabled: !!gameId },
  );
  const { refetch } = query;
  const onDownloadEnded = useCallback(
    (payload: Record<string, unknown>) => {
      const id = payload.store_game_id ?? payload.game_id;
      if (gameId && id === gameId) void refetch();
    },
    [gameId, refetch],
  );
  useEventBus(Events.DOWNLOAD_COMPLETE, onDownloadEnded, [onDownloadEnded]);
  useEventBus(Events.DOWNLOAD_FAILED, onDownloadEnded, [onDownloadEnded]);
  return { list: query.data, refetch };
}
