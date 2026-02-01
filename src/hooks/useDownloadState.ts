/**
 * useDownloadState hook
 *
 * Polls the backend for download progress and status.
 * Handles completion detection and triggers callbacks.
 */

import { useState, useEffect, useRef } from "react";
import { call } from "@decky/api";
import { GameInfo } from "./useGameInfo";

export interface DownloadState {
  isDownloading: boolean;
  progress: number;
  downloadId?: string;
}

interface DownloadInfo {
  id: string;
  progress_percent: number;
  status: string;
}

interface DownloadStateResult {
  success: boolean;
  is_downloading: boolean;
  download_info?: DownloadInfo;
}

/**
 * Hook to poll and manage download state for a game
 * @param gameInfo - Game information object
 * @param onComplete - Callback when download completes successfully
 * @returns Current download state
 */
export function useDownloadState(
  gameInfo: GameInfo | null,
  onComplete?: () => void,
): DownloadState {
  const [downloadState, setDownloadState] = useState<DownloadState>({
    isDownloading: false,
    progress: 0,
    downloadId: undefined,
  });
  const pollIntervalRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    if (!gameInfo) return;

    const checkDownloadState = async () => {
      try {
        const result = await call<[string, string], DownloadStateResult>(
          "is_game_downloading",
          gameInfo.game_id,
          gameInfo.store,
        );

        setDownloadState((prevState) => {
          const newState: DownloadState = {
            isDownloading: false,
            progress: 0,
            downloadId: undefined,
          };

          if (result.success && result.is_downloading && result.download_info) {
            const status = result.download_info.status;
            // Only show as downloading if status is actively downloading or queued
            // Cancelled/error items should not be shown as active downloads
            if (status === "downloading" || status === "queued") {
              newState.isDownloading = true;
              newState.progress = result.download_info.progress_percent;
              newState.downloadId = result.download_info.id;
            }
          }

          // Detect transition from Downloading -> Not Downloading (Completion)
          if (prevState.isDownloading && !newState.isDownloading) {
            console.log(
              "[useDownloadState] Download stopped, checking status...",
            );

            const finalStatus = result.download_info?.status;

            if (finalStatus === "completed") {
              console.log("[useDownloadState] Download successfully finished");
              // Trigger completion callback
              onComplete?.();
            } else if (finalStatus === "cancelled") {
              console.log(
                "[useDownloadState] Download was cancelled - suppressing success message",
              );
            } else if (finalStatus === "error") {
              console.log(
                "[useDownloadState] Download failed - suppressing success message",
              );
            }
          }

          return newState;
        });
      } catch (error) {
        console.error(
          "[useDownloadState] Error checking download state:",
          error,
        );
      }
    };

    // Initial check
    checkDownloadState();

    // Poll every second when displaying
    pollIntervalRef.current = setInterval(checkDownloadState, 1000);

    return () => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
      }
    };
  }, [gameInfo, onComplete]);

  return downloadState;
}
