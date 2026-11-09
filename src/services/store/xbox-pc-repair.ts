/**
 * Repair an Xbox PC game whose files are missing.
 *
 * Play on such a game fails in the launcher with `not_downloaded`, and its
 * toast carries a `repair-xbox-pc` action. The launcher-toast poll calls
 * {@link repairXboxPcGame} as soon as it sees that toast, so the missing
 * files download with no extra tap: the backend queues an install into the
 * folder the game already has, which the installer runs as a repair (or a
 * resume of an unfinished download). The download then shows in the queue.
 */
import { rpcRoutes } from "../../api/rpc-routes";
import { downloadErrorToast } from "../../lib/download-errors";
import { callXboxPc } from "../../stores/xbox-pc-store";
import { headerText } from "../toast-text";
import { showToast } from "../show-toast";

/** The toast action verb the launcher sends with `not_downloaded`. */
export const REPAIR_VERB = "repair-xbox-pc";

/** Queue the repair; a refusal toasts why (rule 8). */
export async function repairXboxPcGame(gameId: string): Promise<boolean> {
  const r = await callXboxPc<unknown>(rpcRoutes.repairXboxPcGame, gameId);
  if (r.ok) return true;
  showToast({
    title: headerText("toasts.header.gameRepair"),
    body: downloadErrorToast(r.error),
    duration: 7000,
  });
  return false;
}
