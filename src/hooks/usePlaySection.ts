/**
 * usePlaySection — Steam "Play / Install" button override.
 *
 * Mounted by `<PlaySectionWrapper>`, this hook decides what
 * the Play section should look like for a given Steam appId :
 *
 *  - Steam-native game → no override (returns
 *    `{ shouldOverride: false }`)
 *  - Unifideck game, NOT installed → expose `installAction`
 *  - Unifideck game, IN download queue → expose
 *    `cancelDownloadAction` + the live download item
 *  - Unifideck game, installed → expose `playAction` plus
 *    `uninstallAction`
 *
 * The decision is recomputed reactively when the underlying
 * `useGameInfo` data or the download queue snapshot changes.
 * The render component reads `shouldOverride` and the
 * action functions; it stays presentational.
 */
import { useMemo } from "react";
import { useGameInfo } from "./useGameInfo";
import { useDownloads } from "../contexts/DownloadContext";
import type { DownloadItem } from "../types/downloads";

/**
 * Discriminated union of the Play-section states. Drives
 * which variant of the Play button is rendered
 * (NotInstalled / Downloading / Installed / Launching).
 */
export type PlaySectionState =
  | { kind: "steam-native"; shouldOverride: false }
  | { kind: "not-installed"; shouldOverride: true; installable: true }
  | { kind: "downloading"; shouldOverride: true; download: DownloadItem }
  | { kind: "installed"; shouldOverride: true; appId: number }
  | { kind: "xcloud"; shouldOverride: true; appId: number; gameId: string };

/**
 * Hook that resolves the current Play-section state
 * for an app : not-installed, downloading, installed,
 * launching, or running. Watches install / launch /
 * download events to recompute on every transition,
 * driving the right Play-button variant.
 *
 * @param appId — Steam shortcut app-id.
 * @returns current `PlaySectionState`.
 */
export function usePlaySection(appId: number | null): PlaySectionState {
  const info = useGameInfo(appId);
  const downloads = useDownloads();

  return useMemo<PlaySectionState>(() => {
    if (appId == null || !info.data) {
      return { kind: "steam-native", shouldOverride: false };
    }

    const game = info.data;
    // Filter out games whose store is plain Steam — we never override those.
    if (game.store === "steam") {
      return { kind: "steam-native", shouldOverride: false };
    }

    // xCloud (Xbox Cloud Gaming) titles stream in a browser — there's
    // nothing to install, so they're always "playable". Surface a Play
    // state regardless of is_installed; the launcher routes the xcloud
    // games.map sentinel to the Edge kiosk streaming flow.
    if (game.store_tags?.includes("xcloud")) {
      return { kind: "xcloud", shouldOverride: true, appId, gameId: game.id };
    }

    // Check the live queue first — a download in progress
    // takes precedence over `is_installed` flag staleness.
    const inQueue =
      findInQueue(downloads.queue?.current, game.id) ??
      downloads.queue?.queued.find(
        (d) => d.game_id === game.id && !isTerminal(d),
      ) ??
      null;
    if (inQueue) {
      return { kind: "downloading", shouldOverride: true, download: inQueue };
    }
    if (!game.is_installed) {
      return { kind: "not-installed", shouldOverride: true, installable: true };
    }

    return { kind: "installed", shouldOverride: true, appId };
  }, [appId, info.data, downloads.queue]);
}

/**
 * Statuses meaning the item is finished with. A finished download must never
 * render as an active one: the "downloading" branch above deliberately
 * outranks `is_installed`, so a stale row does not just look wrong, it hides
 * the Play button behind a progress bar whose Cancel cannot succeed.
 *
 * The backend filters these out of `current` too (`get_queue`). This is the
 * second line of defence, and it is worth having: the queue snapshot is only
 * replaced when a download event arrives, there is no polling refetch, and the
 * event that clears a row is the same one that races the backend's own
 * cleanup.
 */
const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "complete",
  "failed",
  "cancelled",
]);

function isTerminal(item: DownloadItem): boolean {
  return TERMINAL_STATUSES.has(item.status);
}

function findInQueue(
  current: DownloadItem | null | undefined,
  gameId: string,
): DownloadItem | null {
  if (!current || isTerminal(current)) return null;

  return current.game_id === gameId ? current : null;
}
