/**
 * useDlcLink: the game page's DLC button for an Xbox PC game.
 *
 * Returns the press action when the account owns installable DLC for the
 * game (the list loads with the page, `useXboxPcDlc`), else undefined so the
 * button keeps its Steam store link or stays hidden. After a change it
 * refetches the list, and the backend's update scan turns Play into Update
 * while a ticked DLC is not installed.
 */
import { useCallback } from "react";
import { manageXboxPcDlc } from "../services/store/xbox-pc-dlc";
import type { Game } from "../types/api";
import { useXboxPcDlc } from "./useXboxPcDlc";

export function useDlcLink(
  game: Game | null | undefined,
): (() => void) | undefined {
  const { list, refetch } = useXboxPcDlc(game);
  const open = useCallback(() => {
    if (!game || !list) return;
    void manageXboxPcDlc(game.store_game_id, game.title, list).then(
      (changed) => {
        if (changed) void refetch();
      },
    );
  }, [game, list, refetch]);
  return list?.applicable && list.dlc.length > 0 ? open : undefined;
}
