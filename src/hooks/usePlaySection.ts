/**
 * usePlaySection — Steam "Play / Install" button override.
 *
 * Mounted by `<PlaySectionWrapper>`, this hook decides what
 * the Play section should look like for a given Steam appId :
 *
 *  - Steam-native game → no override (returns
 *    `{ shouldOverride: false }`)
 *  - backend not answering → `unavailable`, also no override, so
 *    the page falls back to Steam's own Play section
 *  - Unifideck game, NOT installed → expose `installAction`
 *  - Unifideck game, IN download queue → expose
 *    `cancelDownloadAction` + the live download item
 *  - Unifideck game, installed → expose `playAction` plus
 *    `uninstallAction`
 *  - Xbox PC row that cannot install right now → `pc-unsupported`
 *
 * The decision itself is the pure `resolvePlaySection`; this hook only
 * gathers its inputs (game info, the download queue, the Xbox PC status)
 * and recomputes when any of them changes. The render component reads
 * `shouldOverride` and the state; it stays presentational.
 */
import { useMemo } from "react";
import { useGameInfo } from "./useGameInfo";
import { useXboxPcStatus } from "./useXboxPcStatus";
import { useDownloads } from "../contexts/DownloadContext";
import {
  resolvePlaySection,
  type PlaySectionState,
} from "./resolvePlaySection";

export type { PlaySectionState } from "./resolvePlaySection";

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
  const xbox = useXboxPcStatus();

  const { data, status } = info;
  return useMemo<PlaySectionState>(
    () => resolvePlaySection(appId, { data, status }, downloads.queue, xbox),
    [appId, data, status, downloads.queue, xbox],
  );
}
