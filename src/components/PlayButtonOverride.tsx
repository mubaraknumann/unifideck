/**
 * PlaySectionWrapper Component
 *
 * For non-Steam Unifideck games, this component:
 *   - Installed & not downloading: Renders hidden anchor (native PlaySection visible)
 *   - Uninstalled: Hides native PlaySection via CSS, renders custom Install button
 *   - Downloading: Hides native PlaySection via CSS, renders Cancel button with progress
 *
 * Native PlaySection hiding strategy (CSSLoader-aligned):
 *   Style injection is DECOUPLED from React component lifecycle.
 *   Module-level functions inject/remove a <style> tag in document.head.
 *   The patcher calls injectHidePlaySection() synchronously before React reconciles.
 *   The component only calls removeHidePlaySection() when it confirms the game IS installed.
 *   Plugin onDismount calls removeAllHidePlaySectionStyles() for cleanup.
 *
 * Click handling triggers Steam's game action flow, which the interceptor catches.
 */

import { FC, useState, useEffect, useRef } from "react";
import { call, toaster } from "@decky/api";
import {
  DialogButton,
  Focusable,
  showModal,
  ConfirmModal,
} from "@decky/ui";
import { useTranslation } from "react-i18next";
import { updateSingleGameStatus } from "../tabs";
import { setDownloadStateRef as setInterceptorDownloadState } from "../hooks/gameActionInterceptor";
import { GOGLanguageSelectModal } from "./GOGLanguageSelectModal";

// ============================================================
// CDP-based style management (CSSLoader pattern)
// Uses Chrome DevTools Protocol to inject CSS across CEF process boundary.
// ============================================================

// Per-appId operation queue to prevent inject/remove race conditions.
// Each key maps to the last CDP promise for that appId, so operations
// for the same app are chained sequentially.
const pendingCDPOps: Map<number, Promise<void>> = new Map();

function chainCDPOp(appId: number, op: () => Promise<void>): void {
  const prev = pendingCDPOps.get(appId) || Promise.resolve();
  const next = prev.then(op, op); // run op even if prev rejected
  pendingCDPOps.set(appId, next);
  // Clean up after completion to avoid memory leak
  next.finally(() => {
    if (pendingCDPOps.get(appId) === next) {
      pendingCDPOps.delete(appId);
    }
  });
}

/**
 * DEBUG: Find and log native PlaySection structure via CDP
 */
async function debugLogPlaySectionStructureViaCDP(
  appId: number,
): Promise<void> {
  try {
    const result = await call<
      [number],
      { success: boolean; structure?: string; error?: string }
    >("debug_log_playsection_structure", appId);

    if (result.success && result.structure) {
      console.log("[DEBUG CDP] ========== PlaySection Structure ==========");
      console.log(result.structure);
      console.log("[DEBUG CDP] ========================================");
    } else {
      console.error("[DEBUG CDP] Failed:", result.error);
    }
  } catch (error) {
    console.error("[DEBUG CDP] Call failed:", error);
  }
}

/**
 * Inject CSS to hide native PlaySection via CDP.
 * Called from the patcher (asynchronously, non-blocking).
 * Operations for the same appId are chained to prevent race conditions.
 * NOTE: No session guard - CDP must re-hide after every React re-render
 * because React destroys and recreates DOM elements.
 */
export async function injectHidePlaySectionCDP(appId: number): Promise<void> {
  chainCDPOp(appId, async () => {
    try {
      const result = await call<[number], { success: boolean; error?: string }>(
        "hide_native_play_section",
        appId,
      );

      if (result.success) {
        console.log(
          `[PlaySectionWrapper] Hidden native play section via CDP for app ${appId}`,
        );
      } else {
        console.error(`[PlaySectionWrapper] CDP hide failed: ${result.error}`);
      }
    } catch (error) {
      console.error(`[PlaySectionWrapper] CDP hide call failed:`, error);
    }
  });
}

/**
 * Remove hide CSS for specific app via CDP.
 * Called when the component confirms the game IS installed, or on error/unmount.
 * Operations for the same appId are chained to prevent race conditions.
 */
export async function removeHidePlaySectionCDP(appId: number): Promise<void> {
  chainCDPOp(appId, async () => {
    try {
      const result = await call<[number], { success: boolean; error?: string }>(
        "unhide_native_play_section",
        appId,
      );

      if (result.success) {
        console.log(
          `[PlaySectionWrapper] Unhidden native play section via CDP for app ${appId}`,
        );
      }
    } catch (error) {
      console.error(`[PlaySectionWrapper] CDP unhide failed:`, error);
    }
  });
}

let gameInfoCacheRef: Map<number, { info: any; timestamp: number }> | null =
  null;
const CACHE_TTL = 5000;

export function setPlayButtonCacheRef(
  cache: Map<number, { info: any; timestamp: number }>,
) {
  gameInfoCacheRef = cache;
}

// Helper functions for formatting stats
function formatLastPlayed(timestamp: number): string {
  if (!timestamp) return "Never";
  const date = new Date(timestamp * 1000);
  const now = new Date();
  const diffDays = Math.floor(
    (now.getTime() - date.getTime()) / (1000 * 60 * 60 * 24),
  );
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  return date.toLocaleDateString();
}

function formatPlaytime(minutes: number): string {
  if (minutes === 0) return "0 hours";
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  if (mins === 0) return `${hours} hours`;
  return `${hours}.${Math.floor((mins / 60) * 10)} hours`;
}

interface PlaySectionWrapperProps {
  appId: number;
}

export const PlaySectionWrapper: FC<PlaySectionWrapperProps> = ({ appId }) => {
  const [gameInfo, setGameInfo] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [downloadState, setDownloadState] = useState<{
    isDownloading: boolean;
    progress?: number;
    downloadId?: string;
  }>({ isDownloading: false });
  const [isRunning, setIsRunning] = useState(false);
  const [lastPlayedTimestamp, setLastPlayedTimestamp] = useState(0);
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const debugLoggedRef = useRef(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const { t } = useTranslation();

  // Whether we should show our custom UI (and hide native PlaySection)
  // For Unifideck games: always show custom UI (install, cancel, OR play)
  // Non-Unifideck shortcuts: get_game_info returns null → gameInfo is null → false
  const shouldShowCustom = !loading && gameInfo && !gameInfo.error;

  // Style management: The hide style is injected by the PATCHER via CDP.
  // For Unifideck games, native PlaySection stays permanently hidden —
  // our custom section handles all states (install, cancel, play).
  // We do NOT remove on unmount — React re-renders cause unmount/remount cycles,
  // and the patcher's deduplication prevents re-injection on remount.
  // Cleanup of all hide styles happens in plugin _unload via shutdown_cdp_client.

  // Failure handling: if get_game_info fails or returns null/error,
  // remove the hide CSS so the native PlaySection is restored
  // rather than leaving a permanent blank area.
  useEffect(() => {
    if (!loading && !gameInfo) {
      console.warn(
        `[PlaySectionWrapper] get_game_info returned null for app ${appId}, restoring native PlaySection`,
      );
      removeHidePlaySectionCDP(appId);
    }
  }, [loading, gameInfo, appId]);

  // Navigation cleanup: when the user navigates to a DIFFERENT game page,
  // the appId changes. Remove the old appId's hide CSS so it doesn't
  // leak to other games. The new appId's CSS will be injected by the patcher.
  const prevAppIdRef = useRef(appId);
  useEffect(() => {
    const prevAppId = prevAppIdRef.current;
    if (prevAppId !== appId) {
      removeHidePlaySectionCDP(prevAppId);
      prevAppIdRef.current = appId;
    }
  }, [appId]);

  // Fetch game info on mount
  useEffect(() => {
    if (gameInfoCacheRef) {
      const cached = gameInfoCacheRef.get(appId);
      if (cached && Date.now() - cached.timestamp < CACHE_TTL) {
        setGameInfo(cached.info);
        setLoading(false);
        return;
      }
    }

    call<[number], any>("get_game_info", appId)
      .then((info) => {
        const processedInfo = info?.error ? null : info;
        setGameInfo(processedInfo);
        if (gameInfoCacheRef && processedInfo) {
          gameInfoCacheRef.set(appId, {
            info: processedInfo,
            timestamp: Date.now(),
          });
        }
      })
      .catch((err) => {
        console.error(`[PlaySectionWrapper] Error fetching game info:`, err);
        setGameInfo(null);
      })
      .finally(() => setLoading(false));
  }, [appId]);

  // Fetch last played timestamp via GetPlaytime API
  // Confirmed working for both Steam and non-Steam games
  useEffect(() => {
    (window as any).SteamClient?.Apps?.GetPlaytime?.(appId)
      ?.then((result: any) => {
        if (result?.rtLastTimePlayed) {
          setLastPlayedTimestamp(result.rtLastTimePlayed);
        }
      })
      .catch(() => {});
  }, [appId]);

  // Detect game running state by polling display_status from appStore.
  // display_status is a MobX-managed property on the app overview — reliable for non-Steam shortcuts.
  // Also registers for lifetime notifications as a supplementary trigger for faster updates.
  // EDisplayStatus: Running=4, Launching=1, Terminating=35
  useEffect(() => {
    if (!gameInfo?.is_installed) {
      setIsRunning(false);
      return;
    }

    const checkRunningState = () => {
      try {
        const appStore = (window as any).appStore;
        const overview = appStore?.m_mapApps?.get?.(appId);
        if (!overview) return;

        // One-time diagnostic log to help debug running state issues
        if (!debugLoggedRef.current) {
          console.log(
            `[PlaySectionWrapper] Overview for ${appId}:`,
            "local_per_client_data:", JSON.stringify(overview.local_per_client_data),
            "per_client_data:", JSON.stringify(overview.per_client_data),
          );
          debugLoggedRef.current = true;
        }

        // Try local_per_client_data first (local machine), then per_client_data array
        const localData = overview.local_per_client_data;
        const displayStatus =
          localData?.display_status ??
          overview.per_client_data?.[0]?.display_status;

        // Only update state if display_status is actually available.
        // Don't override bRunning-derived state (from lifetime notifications)
        // with false when display_status isn't populated for non-Steam shortcuts.
        if (displayStatus !== undefined && displayStatus !== null) {
          const running = displayStatus === 4 || displayStatus === 1;
          setIsRunning(running);
        }
      } catch {
        // Silently fail — appStore shape may vary
      }
    };

    // Initial check + poll every 2 seconds
    checkRunningState();
    const pollInterval = setInterval(checkRunningState, 2000);

    // Supplementary: lifetime notifications for faster detection
    let unregLifetime: { unregister(): void } | null = null;
    try {
      unregLifetime =
        window.SteamClient?.GameSessions?.RegisterForAppLifetimeNotifications?.(
          (data) => {
            // For non-Steam shortcuts, unAppID may be 0 — accept both
            if (data.unAppID !== 0 && data.unAppID !== appId) return;
            console.log(
              `[PlaySectionWrapper] Lifetime notification: unAppID=${data.unAppID}, running=${data.bRunning}`,
            );
            // Use bRunning directly — proven reliable for non-Steam shortcuts
            // (MoonDeck pattern). display_status is NOT reliably populated for shortcuts.
            setIsRunning(data.bRunning);
          },
        ) ?? null;
    } catch {
      // GameSessions may not be available
    }

    return () => {
      clearInterval(pollInterval);
      unregLifetime?.unregister?.();
    };
  }, [appId, gameInfo?.is_installed]);

  // Listen for game state changes (e.g. uninstall from GameInfoPanel/InstallInfoDisplay)
  // Same CustomEvent pattern as VIEW_MODE_CHANGE_EVENT (confirmed working)
  useEffect(() => {
    const handleGameStateChange = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.appId !== appId) return;

      console.log(
        `[PlaySectionWrapper] Game state changed for app ${appId}: installed=${detail.isInstalled}`,
      );

      // Clear cache for this app
      if (gameInfoCacheRef) {
        gameInfoCacheRef.delete(appId);
      }

      // Ensure CDP hide stays active (re-inject in case it was cleared)
      injectHidePlaySectionCDP(appId);

      // Re-fetch game info to update component state
      setLoading(true);
      call<[number], any>("get_game_info", appId)
        .then((info) => {
          const processedInfo = info?.error ? null : info;
          setGameInfo(processedInfo);
          if (gameInfoCacheRef && processedInfo) {
            gameInfoCacheRef.set(appId, {
              info: processedInfo,
              timestamp: Date.now(),
            });
          }
        })
        .catch((err) => {
          console.error(
            `[PlaySectionWrapper] Error re-fetching game info:`,
            err,
          );
          setGameInfo(null);
        })
        .finally(() => setLoading(false));
    };

    window.addEventListener(
      "unifideck-game-state-changed",
      handleGameStateChange,
    );
    return () => {
      window.removeEventListener(
        "unifideck-game-state-changed",
        handleGameStateChange,
      );
    };
  }, [appId]);

  // Share download state with the game action interceptor
  useEffect(() => {
    setInterceptorDownloadState({
      isDownloading: downloadState.isDownloading,
      downloadId: downloadState.downloadId,
      gameInfo,
    });
  }, [downloadState, gameInfo]);

  // Proton compat tool handling: When a Unifideck game page loads, check if
  // Steam's Force Compatibility is set with a Proton tool. If so:
  // 1. Save the tool to proton_settings.json (launcher reads at Priority 2.5)
  // 2. Set launch options to use %command% bypass trick so the bash launcher
  //    runs natively even when Proton is configured (Proton command is commented out)
  useEffect(() => {
    if (!gameInfo?.is_installed || !gameInfo?.store || !gameInfo?.game_id) return;
    if (appId <= 2000000000) return;

    let cancelled = false;
    const storeGameId = `${gameInfo.store}:${gameInfo.game_id}`;

    (async () => {
      try {
        const result = await call<[string], {
          success: boolean;
          tool_name?: string;
          appid_unsigned?: number;
          is_linux_runtime?: boolean;
          launcher_path?: string;
          error?: string;
        }>("get_compat_tool_for_game", storeGameId);

        if (cancelled || !result?.success) return;

        const launcherPath = result.launcher_path;
        if (!launcherPath) return;

        if (result.tool_name && !result.is_linux_runtime) {
          // Proton compat tool detected - save for the launcher
          await call<[string, string], { success: boolean }>(
            "save_proton_setting", storeGameId, result.tool_name,
          );

          if (cancelled) return;

          // Set launch options with %command% bypass: runs launcher natively,
          // Proton-wrapped command gets commented out after #
          const bypassOptions = `${launcherPath} "${storeGameId}" #%command%`;
          window.SteamClient?.Apps?.SetShortcutLaunchOptions(appId, bypassOptions);

          console.log(
            `[PlaySectionWrapper] Set %command% bypass for "${gameInfo.title}" (${result.tool_name})`,
          );
        } else {
          // No Proton tool (or Linux runtime) - restore original launch options
          window.SteamClient?.Apps?.SetShortcutLaunchOptions(appId, storeGameId);

          // Clear any previously saved proton setting
          await call<[string, string], { success: boolean }>(
            "save_proton_setting", storeGameId, "",
          );
        }
      } catch (error) {
        console.error("[PlaySectionWrapper] Error checking compat tool:", error);
      }
    })();

    return () => { cancelled = true; };
  }, [appId, gameInfo?.is_installed, gameInfo?.store, gameInfo?.game_id]);

  // Poll for download state
  useEffect(() => {
    if (!gameInfo) return;

    const checkDownloadState = async () => {
      try {
        const result = await call<
          [string, string],
          {
            success: boolean;
            is_downloading: boolean;
            download_info?: {
              id: string;
              progress_percent: number;
              status: string;
            };
          }
        >("is_game_downloading", gameInfo.game_id, gameInfo.store);

        setDownloadState((prevState) => {
          const newState = {
            isDownloading: false,
            progress: 0,
            downloadId: undefined as string | undefined,
          };

          if (result.success && result.is_downloading && result.download_info) {
            const status = result.download_info.status;
            if (status === "downloading" || status === "queued") {
              newState.isDownloading = true;
              newState.progress = result.download_info.progress_percent;
              newState.downloadId = result.download_info.id;
            }
          }

          // Detect download completion
          if (prevState.isDownloading && !newState.isDownloading) {
            const finalStatus = result.download_info?.status;

            if (finalStatus === "completed") {
              toaster.toast({
                title: t("toasts.installComplete"),
                body: t("toasts.installCompleteMessage", {
                  title: gameInfo?.title || "Game",
                }),
                duration: 10000,
                critical: true,
              });

              if (gameInfoCacheRef) gameInfoCacheRef.delete(appId);

              call<[number], any>("get_game_info", appId).then((info) => {
                const processedInfo = info?.error ? null : info;
                // Update caches BEFORE setGameInfo to prevent patcher from
                // seeing stale isInstalled=false and re-injecting CDP hide
                if (processedInfo && gameInfoCacheRef) {
                  gameInfoCacheRef.set(appId, {
                    info: processedInfo,
                    timestamp: Date.now(),
                  });
                  updateSingleGameStatus({
                    appId,
                    store: processedInfo.store,
                    isInstalled: processedInfo.is_installed,
                  });
                }
                setGameInfo(processedInfo);
              });
            }
          }

          return newState;
        });
      } catch (error) {
        console.error(
          "[PlaySectionWrapper] Error checking download state:",
          error,
        );
      }
    };

    checkDownloadState();
    pollIntervalRef.current = setInterval(checkDownloadState, 1000);

    return () => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
      }
    };
  }, [gameInfo, appId]);

  // Autofocus: Focus our action button via CDP after hiding the native one.
  // DOM .focus() doesn't work cross-process (plugin CEF ≠ SP tab), so we
  // use CDP to find and focus our button in the SP tab's DOM directly.
  useEffect(() => {
    if (!shouldShowCustom) return;
    const timer = setTimeout(() => {
      call<[number], { success: boolean }>(
        "focus_unifideck_button",
        appId,
      ).catch(() => {});
    }, 300);
    return () => clearTimeout(timer);
  }, [shouldShowCustom, appId]);

  // Start download with optional language (for GOG games) - matches GameInfoPanel behavior
  const startDownload = async (language?: string) => {
    if (!gameInfo) return;

    const result = await call<
      [string, string, string, boolean, string | null],
      any
    >(
      "add_to_download_queue",
      gameInfo.game_id,
      gameInfo.title,
      gameInfo.store,
      gameInfo.is_installed || false,
      language || null,
    );

    if (result.success) {
      toaster.toast({
        title: t("toasts.downloadStarted"),
        body: t("toasts.downloadQueued", { title: gameInfo.title }),
        duration: 5000,
      });

      setDownloadState((prev) => ({
        ...prev,
        isDownloading: true,
        progress: 0,
      }));
    } else {
      toaster.toast({
        title: t("toasts.downloadFailed"),
        body: result.error
          ? t(result.error)
          : t("toasts.downloadFailedMessage"),
        duration: 10000,
        critical: true,
      });
    }
  };

  // Install handler - checks for GOG language selection
  const handleInstall = async () => {
    if (!gameInfo) return;

    // For GOG games, check if multiple languages are available
    if (gameInfo.store === "gog") {
      try {
        const langResult = await call<
          [string],
          { success: boolean; languages: string[]; error?: string }
        >("get_gog_game_languages", gameInfo.game_id);

        const languages = langResult?.languages;
        if (!langResult?.success || !Array.isArray(languages)) {
          console.warn(
            "[PlaySectionWrapper] Invalid language response, falling back to default:",
            langResult?.error || "unknown error",
          );
          startDownload();
          return;
        }

        // Multiple languages - show selection modal
        if (languages.length > 1) {
          showModal(
            <GOGLanguageSelectModal
              gameTitle={gameInfo.title}
              languages={languages}
              onConfirm={(selectedLang) => startDownload(selectedLang)}
            />,
          );
          return;
        }

        // Single or no language - use first available or fallback
        startDownload(languages[0] || undefined);
        return;
      } catch (error) {
        console.error(
          "[PlaySectionWrapper] Error fetching GOG languages:",
          error,
        );
        // Fallback to download without language selection
      }
    }

    // Non-GOG games or fallback - download without language
    startDownload();
  };

  // Cancel download handler
  const handleCancel = async () => {
    const dlId =
      downloadState.downloadId || `${gameInfo.store}:${gameInfo.game_id}`;

    const result = await call<[string], { success: boolean; error?: string }>(
      "cancel_download_by_id",
      dlId,
    );

    if (result.success) {
      toaster.toast({
        title: t("toasts.downloadCancelled"),
        body: t("toasts.downloadCancelledMessage", { title: gameInfo?.title }),
        duration: 5000,
      });
      setDownloadState({ isDownloading: false, progress: 0 });
    } else {
      toaster.toast({
        title: t("toasts.cancelFailed"),
        body: result.error ? t(result.error) : t("toasts.cancelFailedMessage"),
        duration: 5000,
        critical: true,
      });
    }
  };

  // Show install confirmation modal
  const showInstallConfirmation = () => {
    showModal(
      <ConfirmModal
        strTitle={t("confirmModals.installTitle")}
        strDescription={t("confirmModals.installDescription", {
          title: gameInfo?.title,
        })}
        strOKButtonText={t("confirmModals.yes")}
        strCancelButtonText={t("confirmModals.no")}
        onOK={() => {
          handleInstall();
          return true; // Return true to dismiss modal
        }}
      />,
    );
  };

  // Show cancel confirmation modal
  const showCancelConfirmation = () => {
    showModal(
      <ConfirmModal
        strTitle={t("confirmModals.cancelTitle")}
        strDescription={t("confirmModals.cancelDescription", {
          title: gameInfo?.title,
        })}
        strOKButtonText={t("confirmModals.yes")}
        strCancelButtonText={t("confirmModals.no")}
        bDestructiveWarning={true}
        onOK={() => handleCancel()}
      />,
    );
  };

  // Handle install/cancel button click
  const handleClick = () => {
    if (downloadState.isDownloading) {
      showCancelConfirmation();
    } else if (!gameInfo?.is_installed) {
      showInstallConfirmation();
    }
  };

  // Handle play button click — triggers native Steam launch flow.
  // RunGame requires the gameId (from app overview), NOT the appId directly.
  // For non-Steam shortcuts, gameId differs from appId.
  // Uses window.appStore.m_mapApps (MobX map) to get the overview — same as MoonDeck.
  const handlePlay = () => {
    try {
      // Get gameId from appStore (MoonDeck pattern)
      const appStore = (window as any).appStore;
      const overview = appStore?.m_mapApps?.get?.(appId);
      const gameId = overview?.gameid ?? String(appId);
      console.log(
        `[PlaySectionWrapper] Launching game ${appId} via RunGame (gameId=${gameId}, hasOverview=${!!overview})`,
      );
      window.SteamClient?.Apps?.RunGame?.(gameId, "", -1, 100);
    } catch (error) {
      console.error(`[PlaySectionWrapper] RunGame failed:`, error);
    }
  };

  // Handle stop/close button — terminates the running game.
  // Uses TerminateApp(gameId, false) — same as MoonDeck's terminateApp pattern.
  const handleStop = () => {
    try {
      const appStore = (window as any).appStore;
      const overview = appStore?.m_mapApps?.get?.(appId);
      const gameId = overview?.gameid ?? String(appId);
      console.log(
        `[PlaySectionWrapper] Terminating game ${appId} (gameId=${gameId})`,
      );
      window.SteamClient?.Apps?.TerminateApp?.(gameId, false);
    } catch (error) {
      console.error(`[PlaySectionWrapper] TerminateApp failed:`, error);
    }
  };

  // While loading, or not a Unifideck game — render hidden anchor only
  // The anchor div stays in the DOM so our useEffect can find the parent container
  if (!shouldShowCustom) {
    return (
      <div
        ref={wrapperRef}
        data-unifideck-play-wrapper="true"
        style={{ display: "none" }}
      />
    );
  }

  // Render a custom PlaySection that visually matches Steam's native layout.
  // NOTE: appActionButtonClasses (PlayButton, Green) resolve to stale CSS class names
  // that don't match current Steam DOM. Use explicit inline styles + injected <style> instead.

  // Shared inline styles for all action buttons (Install/Play/Resume/Cancel)
  // Native Steam button: wide, left-aligned text, flush to left edge
  const actionBtnStyle: React.CSSProperties = {
    display: "flex",
    alignItems: "center",
    justifyContent: "flex-start",
    gap: "8px",
    minWidth: "200px",
    height: "48px",
    padding: "0 24px",
    color: "#fff",
    fontSize: "16px",
    fontWeight: 500,
    borderRadius: "4px",
    border: "none",
    textTransform: "uppercase",
    letterSpacing: "0.05em",
  };

  // Injected CSS for hover/focus pseudo-classes (can't do with inline styles).
  // Button colors match native Steam behavior:
  //   Install: grey default → blue on focus/hover (matches native Install screenshot)
  //   Cancel:  red default → brighter red on focus/hover
  //   Play:    grey default → green on focus/hover
  //   Resume:  grey default → blue on focus/hover
  //   X:       grey default → lighter on focus/hover
  const buttonStyles = `
    .unifideck-install-btn,
    .unifideck-play-btn,
    .unifideck-resume-btn,
    .unifideck-stop-btn {
      background: rgba(255, 255, 255, 0.1) !important;
      transition: background 0.15s ease, filter 0.15s ease !important;
    }
    .unifideck-install-btn:hover,
    .unifideck-install-btn.gpfocus {
      background: linear-gradient(135deg, #1a9fff 0%, #1570b5 100%) !important;
    }
    .unifideck-cancel-btn {
      background: linear-gradient(135deg, #dc3545 0%, #c82333 100%) !important;
      transition: background 0.15s ease, filter 0.15s ease !important;
    }
    .unifideck-cancel-btn:hover,
    .unifideck-cancel-btn.gpfocus {
      filter: brightness(1.2) !important;
    }
    .unifideck-play-btn:hover,
    .unifideck-play-btn.gpfocus {
      background: linear-gradient(135deg, #59bf40 0%, #459e31 100%) !important;
    }
    .unifideck-resume-btn:hover,
    .unifideck-resume-btn.gpfocus {
      background: linear-gradient(135deg, #1a9fff 0%, #1570b5 100%) !important;
    }
    .unifideck-stop-btn:hover,
    .unifideck-stop-btn.gpfocus {
      background: rgba(255, 255, 255, 0.2) !important;
    }
    .unifideck-install-btn:active,
    .unifideck-cancel-btn:active,
    .unifideck-play-btn:active,
    .unifideck-resume-btn:active,
    .unifideck-stop-btn:active {
      filter: brightness(0.85) !important;
    }
  `;

  // ========== INSTALLED STATE: Play / Resume + X ==========
  if (gameInfo.is_installed && !downloadState.isDownloading) {
    return (
      <div
        ref={wrapperRef}
        data-unifideck-play-wrapper="true"
        style={{
          display: "flex",
          alignItems: "center",
          width: "100%",
          padding: "16px",
          boxSizing: "border-box",
        }}
      >
        <style>{buttonStyles}</style>

        {/* Action buttons — Play or Resume+X depending on running state */}
        <Focusable style={{ flex: "0 0 auto", display: "flex", gap: "4px" }}>
          {isRunning ? (
            <>
              {/* Resume button — brings running game to foreground */}
              <DialogButton
                className="unifideck-resume-btn"
                onClick={handlePlay}
                style={actionBtnStyle}
              >
                <svg
                  viewBox="0 0 24 24"
                  fill="currentColor"
                  width="1em"
                  height="1em"
                >
                  <path d="M8 5v14l11-7z" />
                </svg>
                {t("installButton.resume", "Resume")}
              </DialogButton>
              {/* X close button — terminates the running game */}
              <DialogButton
                className="unifideck-stop-btn"
                onClick={handleStop}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  width: "48px",
                  height: "48px",
                  minWidth: "48px",
                  padding: "0",
                  borderRadius: "4px",
                  border: "none",
                  color: "#fff",
                }}
              >
                <svg
                  viewBox="0 0 24 24"
                  fill="currentColor"
                  width="20"
                  height="20"
                >
                  <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
                </svg>
              </DialogButton>
            </>
          ) : (
            /* Play button — launches the game */
            <DialogButton
              className="unifideck-play-btn"
              onClick={handlePlay}
              style={actionBtnStyle}
            >
              <svg
                viewBox="0 0 24 24"
                fill="currentColor"
                width="1em"
                height="1em"
              >
                <path d="M8 5v14l11-7z" />
              </svg>
              {t("installButton.play", "Play")}
            </DialogButton>
          )}
        </Focusable>

        {/* Stats — LAST PLAYED only (Steam doesn't return playtime for non-Steam shortcuts) */}
        {/* TODO: Add PLAY TIME stat when playtime API is available for non-Steam games */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "32px",
            marginLeft: "20px",
            flex: "0 1 auto",
          }}
        >
          <div>
            <div
              style={{
                fontSize: "11px",
                fontWeight: 600,
                textTransform: "uppercase",
                color: "#8f98a0",
                letterSpacing: "0.08em",
                lineHeight: "1",
                marginBottom: "5px",
              }}
            >
              LAST PLAYED
            </div>
            <div style={{ fontSize: "14px", color: "#dcdedf" }}>
              {formatLastPlayed(lastPlayedTimestamp)}
            </div>
          </div>
        </div>

        {/* Right icon buttons - controller config + app settings */}
        <Focusable
          style={{
            display: "flex",
            gap: "8px",
            marginLeft: "auto",
            flex: "0 0 auto",
          }}
        >
          <DialogButton
            onClick={() =>
              window.SteamClient?.Apps?.ShowControllerConfigurator?.(appId)
            }
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              width: "48px",
              height: "48px",
              minWidth: "48px",
              padding: "0",
              background: "rgba(255, 255, 255, 0.1)",
              borderRadius: "4px",
            }}
          >
            <svg viewBox="35 31 31 24" fill="currentColor" width="28" height="28">
              <path fillRule="evenodd" d="M38.562 35.88C37.724 37.501 36.752 41.257 36.403 44.227C35.895 48.548 36.106 49.963 37.456 51.313C39.925 53.783 41.749 53.387 43.5 50C44.938 47.219 45.452 47 50.547 47C55.406 47 56.172 47.283 57.157 49.445C58.551 52.504 60.548 53.312 63.202 51.892C65.02 50.919 65.198 50.118 64.734 44.999C64.445 41.814 63.594 37.923 62.843 36.354C61.481 33.508 61.446 33.499 50.782 33.217L40.086 32.933 38.562 35.88zM40.037 36.931C39.254 38.395 39.394 39.251 40.618 40.475C41.506 41.363 42.71 41.93 43.295 41.735C45.057 41.148 46.359 38.377 45.691 36.636C44.829 34.391 41.298 34.575 40.037 36.931zM55.445 37.174C54.533 40.048 57.439 42.371 60.138 40.926C61.162 40.378 62 39.532 62 39.047C62 34.795 56.682 33.276 55.445 37.174z" />
            </svg>
          </DialogButton>
          <DialogButton
            onClick={() =>
              window.SteamClient?.Apps?.OpenAppSettingsDialog?.(
                appId,
                "general",
              )
            }
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              width: "48px",
              height: "48px",
              minWidth: "48px",
              padding: "0",
              background: "rgba(255, 255, 255, 0.1)",
              borderRadius: "4px",
            }}
          >
            <svg viewBox="-32 -32 64 64" fill="currentColor" width="24" height="24">
              <path fillRule="evenodd" d="M-5.96-19.09L-5.79-26.37 5.79-26.37 5.96-19.09 9.29-17.71 14.56-22.74 22.74-14.56 17.71-9.29 19.09-5.96 26.37-5.79 26.37 5.79 19.09 5.96 17.71 9.29 22.74 14.56 14.56 22.74 9.29 17.71 5.96 19.09 5.79 26.37-5.79 26.37-5.96 19.09-9.29 17.71-14.56 22.74-22.74 14.56-17.71 9.29-19.09 5.96-26.37 5.79-26.37-5.79-19.09-5.96-17.71-9.29-22.74-14.56-14.56-22.74-9.29-17.71Z M9 0A9 9 0 1 0-9 0A9 9 0 1 0 9 0Z" />
            </svg>
          </DialogButton>
        </Focusable>
      </div>
    );
  }

  // ========== UNINSTALLED / DOWNLOADING STATE: Install/Cancel button ==========
  const isDownloading = downloadState.isDownloading;
  const displayText = isDownloading
    ? `${t("installButton.cancel")} (${Math.max(
        0,
        downloadState.progress || 0,
      ).toFixed(1)}%)`
    : t("installButton.installNative", "Install");

  return (
    <div
      ref={wrapperRef}
      data-unifideck-play-wrapper="true"
      style={{
        display: "flex",
        alignItems: "center",
        width: "100%",
        padding: "16px",
        boxSizing: "border-box",
      }}
    >
      <style>{buttonStyles}</style>

      {/* Install/Cancel button — explicit styling matching native Steam button */}
      <Focusable style={{ flex: "0 0 auto" }}>
        <DialogButton
          className={
            isDownloading ? "unifideck-cancel-btn" : "unifideck-install-btn"
          }
          onClick={handleClick}
          style={actionBtnStyle}
        >
          {!isDownloading && (
            <svg
              viewBox="0 0 24 24"
              fill="currentColor"
              width="1em"
              height="1em"
            >
              <path d="M5 20h14v-2H5v2zM19 9h-4V3H9v6H5l7 7 7-7z" />
            </svg>
          )}
          {displayText}
        </DialogButton>
      </Focusable>

      {/* Stats - inline layout matching native spacing */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "32px",
          marginLeft: "20px",
          flex: "0 1 auto",
        }}
      >
        <div>
          <div
            style={{
              fontSize: "11px",
              fontWeight: 600,
              textTransform: "uppercase",
              color: "#8f98a0",
              letterSpacing: "0.08em",
              lineHeight: "1",
              marginBottom: "5px",
            }}
          >
            SPACE REQUIRED
          </div>
          <div style={{ fontSize: "14px", color: "#dcdedf" }}>
            {gameInfo?.size_formatted || "\u2014"}
          </div>
        </div>
        <div>
          <div
            style={{
              fontSize: "11px",
              fontWeight: 600,
              textTransform: "uppercase",
              color: "#8f98a0",
              letterSpacing: "0.08em",
              lineHeight: "1",
              marginBottom: "5px",
            }}
          >
            LAST PLAYED
          </div>
          <div style={{ fontSize: "14px", color: "#dcdedf" }}>
            {formatLastPlayed(lastPlayedTimestamp)}
          </div>
        </div>
      </div>

      {/* Right icon buttons - controller config + app settings */}
      <Focusable
        style={{
          display: "flex",
          gap: "8px",
          marginLeft: "auto",
          flex: "0 0 auto",
        }}
      >
        <DialogButton
          onClick={() =>
            window.SteamClient?.Apps?.ShowControllerConfigurator?.(appId)
          }
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: "48px",
            height: "48px",
            minWidth: "48px",
            padding: "0",
            background: "rgba(255, 255, 255, 0.1)",
            borderRadius: "4px",
          }}
        >
          <svg viewBox="35 31 31 24" fill="currentColor" width="28" height="28">
            <path fillRule="evenodd" d="M38.562 35.88C37.724 37.501 36.752 41.257 36.403 44.227C35.895 48.548 36.106 49.963 37.456 51.313C39.925 53.783 41.749 53.387 43.5 50C44.938 47.219 45.452 47 50.547 47C55.406 47 56.172 47.283 57.157 49.445C58.551 52.504 60.548 53.312 63.202 51.892C65.02 50.919 65.198 50.118 64.734 44.999C64.445 41.814 63.594 37.923 62.843 36.354C61.481 33.508 61.446 33.499 50.782 33.217L40.086 32.933 38.562 35.88zM40.037 36.931C39.254 38.395 39.394 39.251 40.618 40.475C41.506 41.363 42.71 41.93 43.295 41.735C45.057 41.148 46.359 38.377 45.691 36.636C44.829 34.391 41.298 34.575 40.037 36.931zM55.445 37.174C54.533 40.048 57.439 42.371 60.138 40.926C61.162 40.378 62 39.532 62 39.047C62 34.795 56.682 33.276 55.445 37.174z" />
          </svg>
        </DialogButton>
        <DialogButton
          onClick={() =>
            window.SteamClient?.Apps?.OpenAppSettingsDialog?.(appId, "general")
          }
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: "48px",
            height: "48px",
            minWidth: "48px",
            padding: "0",
            background: "rgba(255, 255, 255, 0.1)",
            borderRadius: "4px",
          }}
        >
          <svg viewBox="-32 -32 64 64" fill="currentColor" width="24" height="24">
            <path fillRule="evenodd" d="M-5.96-19.09L-5.79-26.37 5.79-26.37 5.96-19.09 9.29-17.71 14.56-22.74 22.74-14.56 17.71-9.29 19.09-5.96 26.37-5.79 26.37 5.79 19.09 5.96 17.71 9.29 22.74 14.56 14.56 22.74 9.29 17.71 5.96 19.09 5.79 26.37-5.79 26.37-5.96 19.09-9.29 17.71-14.56 22.74-22.74 14.56-17.71 9.29-19.09 5.96-26.37 5.79-26.37-5.79-19.09-5.96-17.71-9.29-22.74-14.56-14.56-22.74-9.29-17.71Z M9 0A9 9 0 1 0-9 0A9 9 0 1 0 9 0Z" />
          </svg>
        </DialogButton>
      </Focusable>
    </div>
  );
};

export default PlaySectionWrapper;
