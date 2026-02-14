import { definePlugin, call, toaster, routerHook } from "@decky/api";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  afterPatch,
  findInReactTree,
  createReactTreePatcher,
  appDetailsClasses,
  appActionButtonClasses,
  playSectionClasses,
  appDetailsHeaderClasses,
  basicAppDetailsSectionStylerClasses,
  DialogButton,
  ToggleField,
  showModal,
  Navigation,
} from "@decky/ui";
import React, { FC, useState, useEffect, useRef } from "react";
import { FaGamepad } from "react-icons/fa";
import { loadTranslations, t, changeLanguage } from "./i18n";
import { I18nextProvider, useTranslation } from "react-i18next";
import i18n from "i18next";

// Load translations on startup
loadTranslations();

// Import views

// Import tab system
import {
  patchLibrary,
  loadCompatCacheFromBackend,
  updateSingleGameStatus,
} from "./tabs";

import { syncUnifideckCollections } from "./spoofing/CollectionManager";
import {
  loadSteamAppIdMappings,
  patchSteamStores,
  injectGameToAppinfo,
} from "./spoofing/SteamStorePatcher";

// Import Downloads feature components
import { DownloadsTab } from "./components/DownloadsTab";
import { StorageSettings } from "./components/StorageSettings";
import { UninstallConfirmModal } from "./components/UninstallConfirmModal";
import { SteamRestartModal } from "./components/SteamRestartModal";
import { AccountSwitchModal } from "./components/AccountSwitchModal";
import { LanguageSelector } from "./components/LanguageSelector";
import StoreConnections from "./components/settings/StoreConnections";
import { Store } from "./types/store";
import LibrarySync from "./components/settings/LibrarySync";
import StoreIcon from "./components/StoreIcon";
import GameInfoPanel from "./components/GameInfoPanel";
import {
  PlaySectionWrapper,
  setPlayButtonCacheRef,
  injectHidePlaySectionCDP,
} from "./components/PlayButtonOverride";
import { unifideckGameCache, gameStateVersion, setForceRefreshCallback } from "./tabs";
import {
  registerGameActionInterceptor,
  setGameInfoCacheRef,
} from "./hooks/gameActionInterceptor";
import { SyncProgress } from "./types/syncProgress";

// ========== INSTALL BUTTON FEATURE ==========
//
// CRITICAL: Use routerHook.addPatch + React.createElement ONLY
// Vanilla DOM manipulation does NOT work due to CEF process isolation.
//
// WHY THIS PATTERN IS REQUIRED:
// - Steam Deck UI runs in Chromium Embedded Framework (CEF)
// - Decky plugins execute in separate CEF process (about:blank?createflags=...)
// - Steam UI renders in different process (steamloopback.host)
// - DOM elements cannot be directly injected across process boundaries
//
// WHAT DOESN'T WORK (tried in v52-v68):
// - document.createElement() + appendChild() - creates elements in wrong process
// - ReactDOM.createPortal() - portal target not accessible
// - Direct DOM manipulation - Steam's React overwrites changes
//
// WHAT WORKS (ProtonDB/HLTB pattern):
// - routerHook.addPatch() intercepts React route rendering IN Steam's process
// - React.createElement() creates components in Steam's React tree
// - Steam's reconciler renders these in its own DOM
// - ✅ This is the ONLY way to inject UI into Steam's game details page
//
// ARCHITECTURE:
// - GameDetailsWithInstallButton: Wrapper component with React hooks for state management
// - InstallButtonComponent: Button UI with loading states (shows in game header)
// - InstallOverlayComponent: Modal overlay (click-triggered, not auto-show)
//
// STATE FLOW:
// 1. User navigates to game details → GameDetailsWithInstallButton mounts
// 2. useEffect fetches game info (async) → Shows "Checking..." button
// 3. Game info loaded → Shows "Install [Game]" button
// 4. User clicks Install button → showOverlay = true
// 5. Overlay shows → User clicks "Install Now"
// 6. Installation runs → onInstallComplete() → Toast notification
// 7. Button updates → "Restart Steam to Play" message
// 8. User restarts Steam → Shortcut updated and functional
//
// KEY FIX (v70):
// - Component-level state prevents async/sync race conditions
// - useEffect with [appId] dependency ensures state resets per-game
// - 30-second cache reduces redundant backend calls
//
// ================================================

// Global cache for game info (5-second TTL for faster updates after installation)
const gameInfoCache = new Map<number, { info: any; timestamp: number }>();
const CACHE_TTL = 5000; // 5 seconds - reduced from 30s for faster button state updates

// ========== END INSTALL BUTTON FEATURE ==========

// ========== GAME DETAILS VIEW MODE SETTING ==========
// Persisted toggle between "simple" (top-right download button) and "detailed" (metadata panel)
type GameDetailsViewMode = "simple" | "detailed";
const GAME_DETAILS_VIEW_MODE_KEY = "unifideck-game-details-view-mode";

const getStoredViewMode = (): GameDetailsViewMode => {
  try {
    const stored = localStorage.getItem(GAME_DETAILS_VIEW_MODE_KEY);
    return stored === "simple" ? "simple" : "detailed"; // Default to detailed
  } catch {
    return "detailed";
  }
};

const setStoredViewMode = (mode: GameDetailsViewMode) => {
  try {
    localStorage.setItem(GAME_DETAILS_VIEW_MODE_KEY, mode);
    // Dispatch custom event for components to listen to
    window.dispatchEvent(
      new CustomEvent(VIEW_MODE_CHANGE_EVENT, { detail: mode }),
    );
  } catch {
    console.error("[Unifideck] Failed to save view mode to localStorage");
  }
};

// Custom event name for view mode changes
const VIEW_MODE_CHANGE_EVENT = "unifideck-view-mode-change";
// ========== END GAME DETAILS VIEW MODE SETTING ==========

// ========== NATIVE PLAY BUTTON OVERRIDE ==========
//
// This component shows alongside the native Play button for uninstalled Unifideck games.
// For installed games, we hide this and let Steam's native Play button work.
// For uninstalled games, we show an Install button with size info.
//
// ================================================

// Install Info Display Component - shows download size next to play section
const InstallInfoDisplay: FC<{ appId: number }> = ({ appId }) => {
  const [gameInfo, setGameInfo] = useState<any>(null);
  const [processing, setProcessing] = useState(false);
  const [downloadState, setDownloadState] = useState<{
    isDownloading: boolean;
    progress?: number;
    downloadId?: string;
  }>({ isDownloading: false });
  const pollIntervalRef = useRef<NodeJS.Timeout | null>(null);

  // Track view mode with event listener for live updates
  const [viewMode, setViewMode] = useState<GameDetailsViewMode>(
    getStoredViewMode(),
  );

  useEffect(() => {
    const handleViewModeChange = (e: Event) => {
      const mode = (e as CustomEvent).detail as GameDetailsViewMode;
      setViewMode(mode);
    };
    window.addEventListener(VIEW_MODE_CHANGE_EVENT, handleViewModeChange);
    return () =>
      window.removeEventListener(VIEW_MODE_CHANGE_EVENT, handleViewModeChange);
  }, []);

  const { t } = useTranslation();

  // Fetch game info on mount
  useEffect(() => {
    const cached = gameInfoCache.get(appId);
    if (cached && Date.now() - cached.timestamp < CACHE_TTL) {
      console.log("[InstallInfoDisplay] Using cached game info:", cached.info);
      setGameInfo(cached.info);
      return;
    }

    call<[number], any>("get_game_info", appId)
      .then((info) => {
        console.log("[InstallInfoDisplay] Fetched game info:", info);
        const processedInfo = info?.error ? null : info;
        setGameInfo(processedInfo);
        gameInfoCache.set(appId, {
          info: processedInfo,
          timestamp: Date.now(),
        });
      })
      .catch(() => setGameInfo(null));
  }, [appId]);

  // Poll for download state when we have game info
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
            // Only show as downloading if status is actively downloading or queued
            // Cancelled/error items should not be shown as active downloads
            if (status === "downloading" || status === "queued") {
              newState.isDownloading = true;
              newState.progress = result.download_info.progress_percent;
              newState.downloadId = result.download_info.id;
            }
            // If status is cancelled/error/completed, isDownloading stays false
          }

          // Detect transition from Downloading -> Not Downloading (Completion)
          if (prevState.isDownloading && !newState.isDownloading) {
            console.log(
              "[InstallInfoDisplay] Download stopped, checking status...",
            );

            // Check the status from the download info to determine actual completion
            // result.download_info might be available even if is_downloading is false
            const finalStatus = result.download_info?.status;

            if (finalStatus === "completed") {
              console.log(
                "[InstallInfoDisplay] Download successfully finished",
              );

              // Show installation complete toast
              toaster.toast({
                title: t("toasts.installComplete"),
                body: t("toasts.installCompleteMessage", {
                  title: gameInfo?.title || "Game",
                }),
                duration: 10000,
                critical: true,
              });

              // Invalidate cache first to ensure fresh data
              gameInfoCache.delete(appId);

              // Refresh game info to update button state (Install -> Play/Uninstall)
              call<[number], any>("get_game_info", appId).then((info) => {
                const processedInfo = info?.error ? null : info;
                setGameInfo(processedInfo);
                if (processedInfo) {
                  gameInfoCache.set(appId, {
                    info: processedInfo,
                    timestamp: Date.now(),
                  });
                  // Update tab cache immediately so UI reflects change
                  updateSingleGameStatus({
                    appId,
                    store: processedInfo.store,
                    isInstalled: processedInfo.is_installed,
                  });
                }
              });
            } else if (finalStatus === "cancelled") {
              console.log(
                "[InstallInfoDisplay] Download was cancelled - suppressing success message",
              );
            } else if (finalStatus === "error") {
              console.log(
                "[InstallInfoDisplay] Download failed - suppressing success message",
              );
            } else {
              // Fallback: If no status info (legacy behavior or edge case), verify installation again
              console.log(
                "[InstallInfoDisplay] No final status, verifying installation...",
              );
              call<[number], any>("get_game_info", appId).then((info) => {
                if (info && info.is_installed) {
                  // It is installed, likely success
                  toaster.toast({
                    title: t("toasts.installComplete"),
                    body: t("toasts.installCompleteMessage", {
                      title: gameInfo?.title || "Game",
                    }),
                    duration: 10000,
                  });
                  setGameInfo(info);
                }
              });
            }
          }

          return newState;
        });
      } catch (error) {
        console.error(
          "[InstallInfoDisplay] Error checking download state:",
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
  }, [gameInfo, appId]);

  const handleUninstall = async (deletePrefix: boolean = false) => {
    if (!gameInfo) return;
    setProcessing(true);

    toaster.toast({
      title: t("toasts.uninstalling"),
      body: deletePrefix
        ? t("toasts.uninstallingMessageProton", { title: gameInfo.title })
        : t("toasts.uninstallingMessage", { title: gameInfo.title }),
      duration: 5000,
    });

    const result = await call<[number, boolean], any>(
      "uninstall_game_by_appid",
      appId,
      deletePrefix,
    );

    if (result.success) {
      setGameInfo({ ...gameInfo, is_installed: false });
      gameInfoCache.delete(appId);

      // Update tab cache immediately so UI reflects change without restart
      if (result.game_update) {
        updateSingleGameStatus(result.game_update);
      }

      toaster.toast({
        title: t("toasts.uninstallComplete"),
        body: deletePrefix
          ? t("toasts.uninstallCompleteMessageProton", {
              title: gameInfo.title,
            })
          : t("toasts.uninstallCompleteMessage", { title: gameInfo.title }),
        duration: 10000,
      });
    }
    // Note: Failure case removed - current logic handles all edge cases:
    // 1. Missing game files -> updates flag to not installed
    // 2. Missing mapping -> updates flag to not installed
    // 3. User clicks uninstall -> removes all flags/files
    setProcessing(false);
  };

  const showUninstallConfirmation = () => {
    showModal(
      <UninstallConfirmModal
        gameTitle={gameInfo?.title || "this game"}
        onConfirm={(deletePrefix) => handleUninstall(deletePrefix)}
      />,
    );
  };

  // Not a Unifideck game - return null
  if (!gameInfo || gameInfo.error) return null;

  const isInstalled = gameInfo.is_installed;

  // Install/Cancel buttons have been moved to PlayButtonOverride (in the play section).
  // This component now only shows:
  // - Uninstall button (for installed games, in simple view mode)
  // - Nothing for uninstalled games (install is handled by PlayButtonOverride)

  // Base button style for uninstall button
  const baseButtonStyle: React.CSSProperties = {
    padding: "10px 16px",
    minHeight: "44px",
    minWidth: "180px",
    fontSize: "14px",
    fontWeight: 600,
    borderRadius: "4px",
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    gap: "8px",
    opacity: 1,
    visibility: "visible",
    backdropFilter: "none",
    WebkitBackdropFilter: "none",
  };

  // CSS for controller focus state
  const focusStyles = `
    .unifideck-install-button.gpfocus,
    .unifideck-install-button:hover {
      filter: brightness(1.2) !important;
      box-shadow: 0 0 12px rgba(26, 159, 255, 0.8) !important;
      transform: scale(1.02);
      transition: all 0.15s ease;
    }
    .unifideck-install-button.gpfocus {
      outline: 2px solid #1a9fff !important;
      outline-offset: 2px !important;
    }
  `;

  // In "detailed" mode, the info is shown in GameInfoPanel
  if (viewMode === "detailed") {
    return null;
  }

  // For uninstalled games or active downloads, show nothing here
  // (Install/Cancel is now in PlayButtonOverride in the play section)
  if (!isInstalled || downloadState.isDownloading) {
    return null;
  }

  // Simple mode: Show Uninstall button for installed games
  const sizeText = gameInfo.size_formatted
    ? ` (${gameInfo.size_formatted})`
    : " (- GB)";
  const buttonText =
    t("installButton.uninstall", { title: gameInfo.title }) + sizeText;

  return (
    <>
      <style>{focusStyles}</style>
      <div
        style={{
          position: "absolute",
          top: "40px",
          right: "35px",
          zIndex: 9999,
        }}
        className="unifideck-install-button-container"
      >
        <DialogButton
          onClick={showUninstallConfirmation}
          disabled={processing}
          style={{
            ...baseButtonStyle,
            backgroundColor: "#d32f2f",
            color: "#ffffff",
            border: "2px solid #ff6b6b",
            boxShadow: "0 2px 8px rgba(211, 47, 47, 0.5)",
          }}
          className="unifideck-install-button"
        >
          {processing ? (
            t("installButton.processing")
          ) : (
            <>
              <StoreIcon store={gameInfo.store} size="16px" color="#ffffff" />
              {buttonText}
            </>
          )}
        </DialogButton>
      </div>
    </>
  );
};

/**
 * Find the correct insertion index for PlaySectionWrapper in InnerContainer.
 * Strategy: Insert right after the native PlaySection element.
 * Identification heuristics (in priority order):
 *   1. Child with className matching playSectionClasses.Container
 *   2. Second non-injected native child (native order: [Header, PlaySection, Content])
 *   3. Fallback: Math.min(2, length)
 * Returns the index to splice AT (i.e., the element will appear at this index).
 */
function findPlaySectionInsertIndex(children: any[]): number {
  // Heuristic 1: Find by PlaySection container class
  if (playSectionClasses?.Container) {
    const idx = children.findIndex(
      (child: any) =>
        child?.props?.className?.includes?.(playSectionClasses.Container),
    );
    if (idx >= 0) return idx + 1;
  }

  // Heuristic 2: Insert after the second native child (skip our injected elements).
  // InnerContainer native order: [HeaderCapsule, PlaySection, AboutThisGame].
  // We want to insert after PlaySection (the second native child).
  let nativeCount = 0;
  for (let i = 0; i < children.length; i++) {
    if (children[i]?.key?.startsWith?.("unifideck-")) continue;
    nativeCount++;
    if (nativeCount === 2) return i + 1; // After second native child (PlaySection)
  }

  // Final fallback
  return Math.min(2, children.length);
}

// Patch function for game details route - EXTRACTED TO MODULE SCOPE (ProtonDB/HLTB pattern)
// This ensures the patch is registered in the correct Decky loader context
function patchGameDetailsRoute() {
  return routerHook.addPatch("/library/app/:appid", (routerTree: any) => {
    const routeProps = findInReactTree(routerTree, (x: any) => x?.renderFunc);
    if (!routeProps) return routerTree;

    // Create tree patcher (SAFE: mutates BEFORE React reconciles)
    const patchHandler = createReactTreePatcher(
      [
        // Finder function: return children array (NOT overview object) - ProtonDB pattern
        (tree) =>
          findInReactTree(tree, (x: any) => x?.props?.children?.props?.overview)
            ?.props?.children,
      ],
      (_, ret) => {
        // Patcher function: SAFE to mutate here (before reconciliation)
        // Extract appId from ret (not from finder closure)
        const overview = findInReactTree(
          ret,
          (x: any) => x?.props?.children?.props?.overview,
        )?.props?.children?.props?.overview;

        if (!overview) return ret;
        const appId = overview.appid;

        // DISABLED: Store patching disabled, so no metadata injection
        // TODO: Re-enable once we figure out what's breaking Steam
        // const isShortcut = appId > 2000000000;
        // if (isShortcut) {
        //   const signedAppId = appId > 0x7FFFFFFF ? appId - 0x100000000 : appId;
        //   console.log(`[Unifideck] Game details opened for shortcut: ${appId} (signed: ${signedAppId})`);
        //   injectGameToAppinfo(signedAppId);
        // }

        try {
          // Strategy: Find the Header area (contains Play button and game info)
          // The Header is at the top of the game details page, above the scrollable content

          // Look for the AppDetailsHeader container first (best position)
          const headerContainer = findInReactTree(
            ret,
            (x: any) =>
              Array.isArray(x?.props?.children) &&
              (x?.props?.className?.includes(appDetailsClasses?.Header) ||
                x?.props?.className?.includes(
                  appDetailsHeaderClasses?.TopCapsule,
                )),
          );

          // Find the PlaySection container (where Play button lives)
          const playSection = findInReactTree(
            ret,
            (x: any) =>
              Array.isArray(x?.props?.children) &&
              x?.props?.className?.includes(playSectionClasses?.Container),
          );

          // Alternative: Find the AppButtonsContainer
          const buttonsContainer = findInReactTree(
            ret,
            (x: any) =>
              Array.isArray(x?.props?.children) &&
              x?.props?.className?.includes(
                playSectionClasses?.AppButtonsContainer,
              ),
          );

          // Find the game info row (typically contains play button, shortcuts, settings)
          const gameInfoRow = findInReactTree(
            ret,
            (x: any) =>
              Array.isArray(x?.props?.children) &&
              x?.props?.style?.display === "flex" &&
              x?.props?.children?.some?.(
                (c: any) =>
                  c?.props?.className?.includes?.(
                    appActionButtonClasses?.PlayButtonContainer,
                  ) || c?.type?.toString?.()?.includes?.("PlayButton"),
              ),
          );

          // Find InnerContainer as fallback (original approach)
          const innerContainer = findInReactTree(
            ret,
            (x: any) =>
              Array.isArray(x?.props?.children) &&
              x?.props?.className?.includes(appDetailsClasses?.InnerContainer),
          );

          // Determine game type early — needed for container selection and splice index decisions
          const isNonSteamGame = appId > 2000000000;

          // CONTAINER SELECTION
          // For non-Steam games: REQUIRE InnerContainer. Fallback containers have different
          // children structures, causing hardcoded splice indices to put elements at the bottom.
          // If InnerContainer isn't found (partial tree, timing), skip — patcher retries on next render.
          // For Steam games: use fallback cascade (only InstallInfoDisplay, less position-critical).
          let container: any;
          if (isNonSteamGame) {
            if (!innerContainer) {
              console.log(
                `[Unifideck] InnerContainer not found for non-Steam app ${appId}, skipping injection (will retry)`,
              );
              return ret;
            }
            container = innerContainer;
          } else {
            container =
              innerContainer ||
              headerContainer ||
              playSection ||
              buttonsContainer ||
              gameInfoRow;

            if (!container) {
              console.log(
                `[Unifideck] No suitable container found for app ${appId}, skipping injection`,
              );
              return ret;
            }
          }

          // Ensure children is an array
          if (!Array.isArray(container.props.children)) {
            container.props.children = [container.props.children];
          }

          // DEBUG: Log container children structure to understand injection points
          if (isNonSteamGame) {
            console.log(
              `[Unifideck DEBUG] Container children count: ${container.props.children.length}`,
            );
            container.props.children.forEach((child: any, idx: number) => {
              const key = child?.key || "no-key";
              const type =
                child?.type?.name ||
                child?.type?.toString?.()?.substring(0, 50) ||
                typeof child?.type;
              const className =
                child?.props?.className?.substring?.(0, 80) || "no-className";
              console.log(
                `[Unifideck DEBUG] Child[${idx}]: key=${key}, type=${type}, className=${className}`,
              );
            });
          }

          // DEDUPLICATION: Check if we've already injected our components
          // React re-renders can cause the patcher to run multiple times
          const installInfoKey = `unifideck-install-info-${appId}`;
          const gameInfoKey = `unifideck-game-info-${appId}`;

          const alreadyHasInstallInfo = container.props.children.some(
            (child: any) => child?.key?.startsWith?.(`unifideck-install-info-${appId}`),
          );
          const alreadyHasGameInfo = container.props.children.some(
            (child: any) => child?.key?.startsWith?.(`unifideck-game-info-${appId}`),
          );

          // For non-Steam games: splice at index 4 (after HeaderCapsule[0],
          // native PlaySection[1], PlaySectionWrapper[2], GameInfoPanel[3]).
          // For Steam games: splice at index 2 (ProtonDB uses index 1).
          // InstallInfoDisplay uses position: absolute, so visual position is CSS-controlled.
          // NOTE: For non-Steam games, InstallInfoDisplay injection is deferred
          // until after PlaySectionWrapper and GameInfoPanel are spliced.

          // For Steam games, inject InstallInfoDisplay now (non-Steam handled below)
          if (!isNonSteamGame && !alreadyHasInstallInfo) {
            const spliceIndex = Math.min(2, container.props.children.length);
            container.props.children.splice(
              spliceIndex,
              0,
              React.createElement(InstallInfoDisplay, {
                key: installInfoKey,
                appId,
              }),
            );

            console.log(
              `[Unifideck] Injected install info badge for app ${appId} in ${
                innerContainer
                  ? "InnerContainer"
                  : headerContainer
                  ? "Header"
                  : playSection
                  ? "PlaySection"
                  : buttonsContainer
                  ? "ButtonsContainer"
                  : "GameInfoRow"
              } at index ${spliceIndex}`,
            );
          }

          // ========== PLAY SECTION WRAPPER ==========
          // For non-Steam Unifideck games, inject PlaySectionWrapper into InnerContainer.
          // The wrapper component:
          //   - Installed: renders hidden anchor, native PlaySection stays visible
          //   - Uninstalled: hides native PlaySection via DOM, shows Install button
          //   - Downloading: hides native PlaySection via DOM, shows Cancel button
          const playWrapperKey = `unifideck-play-wrapper-${appId}`;

          if (isNonSteamGame && container) {
            const alreadyHasWrapper = container.props.children.some(
              (child: any) => child?.key?.startsWith?.(playWrapperKey),
            );

            if (!alreadyHasWrapper) {
              // For Unifideck games, always hide native PlaySection —
              // our custom section handles all states (install, cancel, play).
              // Non-Unifideck shortcuts won't be in the cache and are unaffected.
              const cached = unifideckGameCache.get(appId);
              if (cached) {
                // Inject hide style via CDP (async, non-blocking).
                // CDP crosses the CEF process boundary to inject into Steam's SP tab DOM.
                // Style persists across patcher re-runs (idempotent backend check).
                injectHidePlaySectionCDP(appId);
              }

              // Include version in key to force remount when state changes
              const version = gameStateVersion.get(appId) || 0;
              const versionedKey = `${playWrapperKey}-v${version}`;

              // Anchor-based insertion: find native PlaySection and insert after it.
              // Avoids hardcoded indices that break when children order shifts.
              const wrapperSpliceIndex = findPlaySectionInsertIndex(
                container.props.children,
              );
              container.props.children.splice(
                wrapperSpliceIndex,
                0,
                React.createElement(PlaySectionWrapper, {
                  key: versionedKey,
                  appId,
                  playSectionClassName: basicAppDetailsSectionStylerClasses?.PlaySection,
                }),
              );
              console.log(
                `[Unifideck] Injected PlaySectionWrapper at index ${wrapperSpliceIndex} for app ${appId} (version ${version})`,
              );
            } else {
              // POSITION CORRECTION: If wrapper exists but drifted to wrong position
              // (e.g., at the bottom after restart), reposition it.
              const currentIdx = container.props.children.findIndex(
                (child: any) => child?.key?.startsWith?.(playWrapperKey),
              );
              const expectedMaxIdx = 3;
              if (currentIdx > expectedMaxIdx) {
                console.log(
                  `[Unifideck] PlaySectionWrapper at wrong index ${currentIdx} (expected <= ${expectedMaxIdx}), repositioning`,
                );
                const [element] = container.props.children.splice(
                  currentIdx,
                  1,
                );
                const correctIdx = findPlaySectionInsertIndex(
                  container.props.children,
                );
                container.props.children.splice(correctIdx, 0, element);
              }
            }
          }

          // ========== GAME INFO PANEL INJECTION ==========
          // For non-Steam games, inject our custom GameInfoPanel to display metadata
          // Non-Steam shortcuts have appId > 2000000000
          // Positioned right after PlaySectionWrapper (anchored, not hardcoded)
          if (isNonSteamGame && !alreadyHasGameInfo) {
            try {
              // Insert after the PlaySection wrapper
              const wrapperIndex = container.props.children.findIndex(
                (child: any) => child?.key?.startsWith?.(playWrapperKey),
              );
              const gameInfoSpliceIndex =
                wrapperIndex >= 0
                  ? wrapperIndex + 1
                  : findPlaySectionInsertIndex(container.props.children) + 1;

              const version = gameStateVersion.get(appId) || 0;
              const versionedGameInfoKey = `${gameInfoKey}-v${version}`;

              container.props.children.splice(
                gameInfoSpliceIndex,
                0,
                React.createElement(GameInfoPanel, {
                  key: versionedGameInfoKey,
                  appId,
                }),
              );
              console.log(
                `[Unifideck] Injected GameInfoPanel at index ${gameInfoSpliceIndex} (version ${version})`,
              );
            } catch (panelError) {
              console.error(
                `[Unifideck] Error creating GameInfoPanel:`,
                panelError,
              );
            }
          } else if (isNonSteamGame && alreadyHasGameInfo) {
            // POSITION CORRECTION: GameInfoPanel should be right after PlaySectionWrapper
            const wrapperIdx = container.props.children.findIndex(
              (child: any) => child?.key?.startsWith?.(playWrapperKey),
            );
            const gameInfoIdx = container.props.children.findIndex(
              (child: any) =>
                child?.key?.startsWith?.(`unifideck-game-info-${appId}`),
            );
            if (
              wrapperIdx >= 0 &&
              gameInfoIdx >= 0 &&
              gameInfoIdx !== wrapperIdx + 1
            ) {
              console.log(
                `[Unifideck] GameInfoPanel at index ${gameInfoIdx}, expected ${wrapperIdx + 1}, repositioning`,
              );
              const [element] = container.props.children.splice(
                gameInfoIdx,
                1,
              );
              // Re-find wrapper index after splice (indices shifted)
              const newWrapperIdx = container.props.children.findIndex(
                (child: any) => child?.key?.startsWith?.(playWrapperKey),
              );
              container.props.children.splice(
                newWrapperIdx + 1,
                0,
                element,
              );
            }
          }

          // ========== INSTALL INFO BADGE (Non-Steam) ==========
          // For non-Steam games, inject InstallInfoDisplay AFTER GameInfoPanel.
          // Position: absolute, so visual placement is CSS-controlled.
          // Uses relative positioning (after GameInfoPanel) instead of hardcoded index.
          if (isNonSteamGame && !alreadyHasInstallInfo) {
            const gameInfoIdx = container.props.children.findIndex(
              (child: any) =>
                child?.key?.startsWith?.(`unifideck-game-info-${appId}`),
            );
            const installInfoSpliceIndex =
              gameInfoIdx >= 0
                ? gameInfoIdx + 1
                : Math.min(4, container.props.children.length);

            const version = gameStateVersion.get(appId) || 0;
            const versionedInstallInfoKey = `${installInfoKey}-v${version}`;

            container.props.children.splice(
              installInfoSpliceIndex,
              0,
              React.createElement(InstallInfoDisplay, {
                key: versionedInstallInfoKey,
                appId,
              }),
            );
            console.log(
              `[Unifideck] Injected install info badge for app ${appId} at index ${installInfoSpliceIndex} (version ${version})`,
            );
          }
        } catch (error) {
          console.error("[Unifideck] Error injecting install info:", error);
        }

        return ret; // Always return modified tree
      },
    );

    // Apply patcher to renderFunc
    afterPatch(routeProps, "renderFunc", patchHandler);

    return routerTree;
  });
}

// Persistent tab state (survives component remounts)
let persistentActiveTab: "settings" | "downloads" = "settings";

// Settings panel in Quick Access Menu
const Content: FC = () => {
  const { t } = useTranslation();

  // Tab navigation state - initialize from persistent value
  const [activeTab, setActiveTab] = useState<"settings" | "downloads">(
    persistentActiveTab,
  );

  // Update persistent state whenever tab changes
  const handleTabChange = (tab: "settings" | "downloads") => {
    persistentActiveTab = tab;
    setActiveTab(tab);
  };

  const [syncing, setSyncing] = useState(false);
  const [syncCooldown, setSyncCooldown] = useState(false);
  const [cooldownSeconds, setCooldownSeconds] = useState(0);
  const [deleting, setDeleting] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [deleteFiles, setDeleteFiles] = useState(false);

  // Auto-focus ref
  const mountRef = useRef<HTMLDivElement>(null);

  // Auto-focus logic
  useEffect(() => {
    // Focus the first focusable element on mount
    const timer = setTimeout(() => {
      if (mountRef.current) {
        const focusable = mountRef.current.querySelector(
          'button, [tabindex="0"]',
        );
        if (focusable instanceof HTMLElement) {
          focusable.focus();
          focusable.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      }
    }, 100);
    return () => clearTimeout(timer);
  }, []);

  const [storeStatus, setStoreStatus] = useState<Record<Store, string>>({
    epic: "checking",
    gog: "checking",
    amazon: "checking",
  });

  // Game Details View Mode - persisted via localStorage
  const [gameDetailsViewMode, setGameDetailsViewMode] =
    useState<GameDetailsViewMode>(getStoredViewMode());

  const handleViewModeChange = (mode: GameDetailsViewMode) => {
    setGameDetailsViewMode(mode);
    setStoredViewMode(mode); // This dispatches the custom event
  };

  const [syncProgress, setSyncProgress] = useState<SyncProgress | null>(null);

  // Store polling interval ref to allow cleanup on unmount
  const pollIntervalRef = useRef<NodeJS.Timeout | null>(null);

  // Cleanup polling interval on unmount
  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
        pollIntervalRef.current = null;
        console.log("[Unifideck] Cleaned up polling interval on unmount");
      }
    };
  }, []);

  useEffect(() => {
    // Check store connectivity on mount
    checkStoreStatus();
  }, []);

  // Restore sync state on mount (in case user navigated away during sync)
  useEffect(() => {
    const restoreSyncState = async () => {
      try {
        const status = await call<
          [],
          {
            is_syncing: boolean;
            sync_progress: SyncProgress | null;
          }
        >("get_sync_status");

        if (status.is_syncing && status.sync_progress) {
          console.log(
            "[Unifideck] Restoring sync state on mount:",
            status.sync_progress,
          );

          // Restore syncing state
          setSyncing(true);
          setSyncProgress(status.sync_progress);

          // Deduplication flag (scoped to this restore)
          let completionHandled = false;

          // Clear any existing polling interval before creating a new one
          if (pollIntervalRef.current) {
            clearInterval(pollIntervalRef.current);
            console.log(
              "[Unifideck] Cleared existing polling interval before restore",
            );
          }

          // Resume polling for progress
          pollIntervalRef.current = setInterval(async () => {
            try {
              const result = await call<
                [],
                { success?: boolean } & SyncProgress
              >("get_sync_progress");

              if (result.success) {
                setSyncProgress(result);

                // Log progress updates
                if (result.current_game.label) {
                  const progress =
                    result.current_phase === "artwork"
                      ? `${result.artwork_synced}/${result.artwork_total}`
                      : `${result.synced_games}/${result.total_games}`;
                  console.log(
                    `[Unifideck] ` +
                      t(
                        `${result.current_game.label}`,
                        result.current_game.values,
                      ) +
                      ` (${progress})`,
                  );
                }

                // Stop polling when complete, error, or cancelled
                if (
                  result.status === "complete" ||
                  result.status === "error" ||
                  result.status === "cancelled"
                ) {
                  if (pollIntervalRef.current) {
                    clearInterval(pollIntervalRef.current);
                    pollIntervalRef.current = null;
                  }
                  setSyncing(false);

                  // Only run completion logic once
                  if (!completionHandled) {
                    completionHandled = true;

                    if (result.status === "complete") {
                      console.log(
                        `[Unifideck] ✓ Sync completed: ${result.synced_games} games processed`,
                      );
                    } else if (result.status === "cancelled") {
                      console.log(`[Unifideck] ⚠ Sync cancelled by user`);
                    }

                    // Show restart notification when sync completes (if library has games)
                    if (result.status === "complete") {
                      const totalGames = result.synced_games || 0;

                      // Show modal if there are any games in the library
                      if (totalGames > 0) {
                        showModal(<SteamRestartModal closeModal={() => {}} />);
                      }
                    } else if (result.status === "cancelled") {
                      toaster.toast({
                        title: t("toasts.syncCancelled"),
                        body: result.current_game.label
                          ? t(
                              result.current_game.label,
                              result.current_game.values,
                            )
                          : t("toasts.syncCancelled"),
                        duration: 5000,
                      });
                    }

                    // Start cooldown
                    setSyncCooldown(true);
                    setCooldownSeconds(5);

                    const cooldownInterval = setInterval(() => {
                      setCooldownSeconds((prev) => {
                        if (prev <= 1) {
                          clearInterval(cooldownInterval);
                          setSyncCooldown(false);
                          return 0;
                        }
                        return prev - 1;
                      });
                    }, 1000);

                    setTimeout(() => setSyncProgress(null), 5000);
                  }
                }
              }
            } catch (error) {
              console.error("[Unifideck] Error polling sync progress:", error);
            }
          }, 500);

          console.log("[Unifideck] Sync state restored, polling resumed");
        } else {
          console.log("[Unifideck] No active sync on mount");
        }
      } catch (error) {
        console.error("[Unifideck] Error restoring sync state:", error);
      }
    };

    restoreSyncState();
  }, []);

  const checkStoreStatus = async () => {
    try {
      // Add timeout wrapper
      const timeoutPromise = new Promise((_, reject) =>
        setTimeout(() => reject(new Error("Status check timed out")), 10000),
      );

      const checkPromise = call<
        [],
        {
          success: boolean;
          epic: string;
          gog: string;
          amazon: string;
          error?: string;
          legendary_installed?: boolean;
          nile_installed?: boolean;
        }
      >("check_store_status");

      const result = (await Promise.race([
        checkPromise,
        timeoutPromise,
      ])) as any;

      if (result.success) {
        setStoreStatus({
          epic: result.epic,
          gog: result.gog,
          amazon: result.amazon,
        });

        // Show warning if legendary not installed
        if (result.legendary_installed === false) {
          console.warn(
            "[Unifideck] Legendary CLI not installed - Epic Games won't work",
          );
        }
        // Show warning if nile not installed
        if (result.nile_installed === false) {
          console.warn(
            "[Unifideck] Nile CLI not installed - Amazon Games won't work",
          );
        }
      } else {
        console.error("[Unifideck] Status check failed:", result.error);
        setStoreStatus({
          epic: "error",
          gog: "error",
          amazon: "error",
        });
      }
    } catch (error) {
      console.error("[Unifideck] Error checking store status:", error);
      setStoreStatus({
        epic: "error",
        gog: "error",
        amazon: "error",
      });
    }
  };

  const handleManualSync = async (
    force: boolean = false,
    resyncArtwork: boolean = false,
  ) => {
    // Prevent concurrent syncs
    if (syncing || syncCooldown) {
      console.log("[Unifideck] Sync already in progress or on cooldown");
      return;
    }

    setSyncing(true);
    setSyncProgress(null);

    // Deduplication flag to prevent multiple polls handling completion
    let completionHandled = false;

    // Clear any existing polling interval before creating a new one
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      console.log(
        "[Unifideck] Cleared existing polling interval before manual sync",
      );
    }

    // Start polling for progress
    pollIntervalRef.current = setInterval(async () => {
      try {
        const result = await call<[], { success?: boolean } & SyncProgress>(
          "get_sync_progress",
        );

        if (result.success) {
          setSyncProgress(result);

          // Log progress updates
          if (result.current_game.label) {
            const progress =
              result.current_phase === "artwork"
                ? `${result.artwork_synced}/${result.artwork_total}`
                : `${result.synced_games}/${result.total_games}`;
            console.log(
              `[Unifideck] ` +
                t(`${result.current_game.label}`, result.current_game.values) +
                ` (${progress})`,
            );
          }
        }

        // Stop polling when complete, error, or cancelled
        if (
          result.status === "complete" ||
          result.status === "error" ||
          result.status === "cancelled"
        ) {
          if (pollIntervalRef.current) {
            clearInterval(pollIntervalRef.current);
            pollIntervalRef.current = null;
          }
          setSyncing(false);

          // CRITICAL FIX: Only run completion logic ONCE
          if (!completionHandled) {
            completionHandled = true; // Set flag IMMEDIATELY

            if (result.status === "complete") {
              console.log(
                `[Unifideck] ✓ Sync completed: ${result.synced_games} games processed`,
              );
            } else if (result.status === "cancelled") {
              console.log(`[Unifideck] ⚠ Sync cancelled by user`);
            }

            // Show restart notification when sync completes (if library has games)
            if (result.status === "complete") {
              const totalGames = result.synced_games || 0;

              // Show modal if there are any games in the library
              // (user explicitly triggered sync, so remind them to restart)
              if (totalGames > 0) {
                showModal(<SteamRestartModal closeModal={() => {}} />);
              }
            } else if (result.status === "cancelled") {
              toaster.toast({
                title: t("toasts.syncCancelled"),
                body: result.current_game.label
                  ? t(result.current_game.label, result.current_game.values)
                  : t("toasts.syncCancelled"),
                duration: 5000,
              });
            }
          } else {
            // Completion already handled by another poll - do nothing
            console.log(
              `[Unifideck] (duplicate poll detected, skipping completion logic)`,
            );
          }
        }
      } catch (error) {
        console.error("[Unifideck] Error getting sync progress:", error);
      }
    }, 500); // Poll every 500ms

    try {
      // Use force_sync_libraries for force sync (rewrites shortcuts and compatibility data)
      console.log(
        `[Unifideck] Starting ${force ? "force " : ""}sync...${
          force ? ` (resync artwork: ${resyncArtwork})` : ""
        }`,
      );

      let syncResult;
      if (force) {
        // Force sync with resyncArtwork parameter
        syncResult = await call<
          [boolean],
          {
            success: boolean;
            epic_count: number;
            gog_count: number;
            amazon_count: number;
            added_count: number;
            artwork_count: number;
            updated_count?: number;
          }
        >("force_sync_libraries", resyncArtwork);
      } else {
        // Regular sync
        syncResult = await call<
          [],
          {
            success: boolean;
            epic_count: number;
            gog_count: number;
            amazon_count: number;
            added_count: number;
            artwork_count: number;
            updated_count?: number;
          }
        >("sync_libraries");
      }

      console.log("[Unifideck] ========== SYNC COMPLETED ==========");
      console.log(`[Unifideck] Epic Games: ${syncResult.epic_count}`);
      console.log(`[Unifideck] GOG Games: ${syncResult.gog_count}`);
      console.log(`[Unifideck] Amazon Games: ${syncResult.amazon_count || 0}`);
      console.log(
        `[Unifideck] Total Games: ${
          syncResult.epic_count +
          syncResult.gog_count +
          (syncResult.amazon_count || 0)
        }`,
      );
      console.log(`[Unifideck] Games Added: ${syncResult.added_count}`);
      console.log(`[Unifideck] Artwork Fetched: ${syncResult.artwork_count}`);
      console.log("[Unifideck] =====================================");

      // Phase 3: Sync Steam Collections
      // Update collections ([Unifideck] Epic Games, etc.) with new games
      await syncUnifideckCollections().catch((err) =>
        console.error("[Unifideck] Failed to sync collections:", err),
      );

      // Reload compat cache from backend (so Great on Deck tab updates immediately)
      console.log("[Unifideck] Refreshing compat cache...");
      await loadCompatCacheFromBackend().catch((err) =>
        console.error("[Unifideck] Failed to refresh compat cache:", err),
      );

      await checkStoreStatus();
    } catch (error) {
      console.error("[Unifideck] Manual sync failed:", error);
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
        pollIntervalRef.current = null;
      }
      setSyncing(false);
    } finally {
      setSyncing(false);

      // START COOLDOWN
      setSyncCooldown(true);
      setCooldownSeconds(5);

      // Countdown timer
      const cooldownInterval = setInterval(() => {
        setCooldownSeconds((prev) => {
          if (prev <= 1) {
            clearInterval(cooldownInterval);
            setSyncCooldown(false);
            return 0;
          }
          return prev - 1;
        });
      }, 1000);

      // Clear progress after cooldown
      setTimeout(() => setSyncProgress(null), 5000);
    }
  };

  /**
   * Poll store status to detect when authentication completes
   */
  const pollForAuthCompletion = async (store: Store): Promise<boolean> => {
    const maxAttempts = 60; // 5 minutes (60 * 5s)
    let attempts = 0;

    // Helper function to check status
    const checkStatus = async (): Promise<boolean> => {
      try {
        const result = await call<
          [],
          {
            success: boolean;
            epic: string;
            gog: string;
            amazon: string;
          }
        >("check_store_status");

        if (result.success) {
          let status: string;
          if (store === "epic") {
            status = result.epic;
          } else if (store === "gog") {
            status = result.gog;
          } else {
            status = result.amazon;
          }
          if (status === "connected") {
            console.log(
              `[Unifideck] ${store} authentication completed automatically!`,
            );
            return true;
          }
        }
      } catch (error) {
        console.error(`[Unifideck] Error polling status:`, error);
      }
      return false;
    };

    // Check immediately first (in case auth completed very fast)
    if (await checkStatus()) {
      return true;
    }

    return new Promise((resolve) => {
      const pollInterval = setInterval(async () => {
        attempts++;

        if (await checkStatus()) {
          clearInterval(pollInterval);
          resolve(true);
          return;
        }

        // Timeout after max attempts
        if (attempts >= maxAttempts) {
          clearInterval(pollInterval);
          console.log(
            `[Unifideck] Polling timeout for ${store} authentication`,
          );
          resolve(false);
        }
      }, 5000); // Poll every 5 seconds
    });
  };

  const startAuth = async (store: Store) => {
    const storeName =
      store === "epic"
        ? t("storeConnections.epicGames")
        : store === "amazon"
        ? t("storeConnections.amazonGames")
        : t("storeConnections.gog");

    try {
      let methodName: string;
      if (store === "epic") {
        methodName = "start_epic_auth";
      } else if (store === "gog") {
        methodName = "start_gog_auth_auto";
      } else {
        methodName = "start_amazon_auth";
      }

      const result = await call<
        [],
        { success: boolean; url?: string; message?: string; error?: string }
      >(methodName);

      if (result.success && result.url) {
        const authUrl = result.url;

        // Open popup window
        const popup = window.open(
          authUrl,
          "_blank",
          "width=800,height=600,popup=yes",
        );

        if (!popup) {
          console.log(
            `[Unifideck] Popup window did not open, continuing with backend auth monitoring...`,
          );
        }

        console.log(
          `[Unifideck] Opened ${store} auth popup. Backend monitoring via CDP...`,
        );

        // Poll for authentication completion in background (NON-BLOCKING)
        // This allows multiple store auths to happen simultaneously
        pollForAuthCompletion(store)
          .then(async (completed) => {
            if (completed) {
              console.log(
                `[Unifideck] ✓ ${storeName} authentication successful!`,
              );
              toaster.toast({
                title: t("toasts.authConnected", { store: storeName }),
                body: t("toasts.authConnectedMessage", { store: storeName }),
                duration: 8000,
                critical: true,
              });
              await checkStoreStatus(); // Refresh status
            } else {
              console.log(`[Unifideck] ${storeName} authentication timed out`);
              toaster.toast({
                title: t("toasts.authTimeout"),
                body: t("toasts.authTimeoutMessage", { store: storeName }),
                critical: true,
                duration: 5000,
              });
            }
          })
          .catch((error) => {
            console.error(`[Unifideck] Error polling ${store} auth:`, error);
          });

        // Return immediately - don't block waiting for auth to complete
      } else {
        toaster.toast({
          title: t("toasts.authFailed"),
          body: result.error ? t(result.error) : t("toasts.authFailedMessage"),
          critical: true,
          duration: 5000,
        });
      }
    } catch (error: any) {
      console.error(`[Unifideck] Error starting ${store} auth:`, error);
      toaster.toast({
        title: t("toasts.authError"),
        body: error.message || String(error),
        critical: true,
        duration: 5000,
      });
    }
  };

  const handleLogout = async (store: Store) => {
    try {
      let methodName: string;
      if (store === "epic") {
        methodName = "logout_epic";
      } else if (store === "gog") {
        methodName = "logout_gog";
      } else {
        methodName = "logout_amazon";
      }
      const result = await call<[], { success: boolean; message?: string }>(
        methodName,
      );

      if (result.success) {
        console.log(`[Unifideck] Logged out from ${store}`);
        await checkStoreStatus();
      }
    } catch (error) {
      console.error(`[Unifideck] Error logging out from ${store}:`, error);
    }
  };

  const handleDeleteAll = async () => {
    if (!showDeleteConfirm) {
      setShowDeleteConfirm(true);
      return;
    }

    setDeleting(true);
    setShowDeleteConfirm(false);

    try {
      const result = await call<
        [{ delete_files: boolean }],
        {
          success: boolean;
          deleted_games: number;
          deleted_artwork: number;
          deleted_files_count: number;
          preserved_shortcuts: number;
          error?: string;
        }
      >("perform_full_cleanup", { delete_files: deleteFiles });

      // Reset checkbox
      setDeleteFiles(false);

      if (result.success) {
        console.log(
          `[Unifideck] Cleanup complete: ${result.deleted_games} games, ` +
            `${result.deleted_artwork} artwork sets, ${result.deleted_files_count} files deleted`,
        );

        toaster.toast({
          title: t("toasts.cleanupSuccessful"),
          body: t("toasts.cleanupSuccessfulMessage", {
            games: result.deleted_games,
            artwork: result.deleted_artwork,
            files: result.deleted_files_count,
          }),
          duration: 8000,
        });
      } else {
        console.error(`[Unifideck] Delete failed: ${result.error}`);
        toaster.toast({
          title: t("toasts.deleteFailed"),
          body: result.error ? t(result.error) : "Unknown error",
          duration: 5000,
        });
      }
    } catch (error) {
      console.error("[Unifideck] Delete error:", error);
    } finally {
      setDeleting(false);
    }
  };

  const handleCancelSync = async () => {
    try {
      // Clear polling interval immediately when user cancels
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
        pollIntervalRef.current = null;
        console.log("[Unifideck] Cleared polling interval on user cancel");
      }

      // Clear progress bar immediately
      setSyncProgress(null);
      setSyncing(false);

      const result = await call<
        [],
        {
          success: boolean;
          message: string;
        }
      >("cancel_sync");

      if (result.success) {
        console.log("[Unifideck] Sync cancelled");
        toaster.toast({
          title: t("toasts.syncCancelled").toUpperCase(),
          body: t("errors.syncCancelled"),
          duration: 3000,
        });
      } else {
        console.log("[Unifideck] Cancel failed:", result.message);
      }
    } catch (error) {
      console.error("[Unifideck] Error cancelling sync:", error);
    }
  };

  return (
    <>
      {/* Tab Navigation */}
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => handleTabChange("settings")}
            disabled={activeTab === "settings"}
          >
            <div ref={mountRef}>{t("tabs.settings")}</div>
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => handleTabChange("downloads")}
            disabled={activeTab === "downloads"}
          >
            {t("tabs.downloads")}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {/* Downloads Tab */}
      {activeTab === "downloads" && (
        <>
          <DownloadsTab />
          <StorageSettings />
        </>
      )}

      {/* Settings Tab */}
      {activeTab === "settings" && (
        <>
          {/* Store Connections - Compact View */}
          <StoreConnections
            storeStatus={storeStatus}
            onStartAuth={startAuth}
            onLogout={handleLogout}
          />

          {/* Library Sync Section */}
          <LibrarySync
            syncing={syncing}
            syncCooldown={syncCooldown}
            cooldownSeconds={cooldownSeconds}
            syncProgress={syncProgress}
            storeStatus={storeStatus}
            handleManualSync={handleManualSync}
            handleCancelSync={handleCancelSync}
            showModal={showModal}
            checkStoreStatus={checkStoreStatus}
          />

          {/* Language Settings - centralized language control */}
          <LanguageSelector />

          {/* Game Details View Mode */}
          <PanelSection title={t("gameDetailsSettings.title")}>
            <PanelSectionRow>
              <ToggleField
                label={
                  gameDetailsViewMode === "simple"
                    ? t("gameDetailsSettings.simple")
                    : t("gameDetailsSettings.detailed")
                }
                checked={gameDetailsViewMode === "simple"}
                onChange={(checked) =>
                  handleViewModeChange(checked ? "simple" : "detailed")
                }
              />
            </PanelSectionRow>
          </PanelSection>

          {/* Cleanup Section */}
          <PanelSection title={t("cleanup.title")}>
            {!showDeleteConfirm ? (
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  onClick={handleDeleteAll}
                  disabled={syncing || deleting || syncCooldown}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "2px",
                      fontSize: "0.85em",
                      padding: "2px",
                    }}
                  >
                    {t("cleanup.deleteAll")}
                  </div>
                </ButtonItem>
              </PanelSectionRow>
            ) : (
              <>
                <PanelSectionRow>
                  <Field
                    label={t("cleanup.warningTitle")}
                    description={t("cleanup.warningDescription")}
                  />
                </PanelSectionRow>

                {/* Delete Files Checkbox */}
                <PanelSectionRow>
                  <ToggleField
                    label={t("cleanup.deleteFilesLabel")}
                    checked={deleteFiles}
                    onChange={(checked) => setDeleteFiles(checked)}
                  />
                </PanelSectionRow>

                <PanelSectionRow>
                  <ButtonItem
                    layout="below"
                    onClick={handleDeleteAll}
                    disabled={deleting}
                  >
                    {deleting
                      ? t("cleanup.deleting")
                      : t("cleanup.confirmDelete")}
                  </ButtonItem>
                </PanelSectionRow>
                <PanelSectionRow>
                  <ButtonItem
                    layout="below"
                    onClick={() => {
                      setShowDeleteConfirm(false);
                      setDeleteFiles(false);
                    }}
                    disabled={deleting}
                  >
                    {t("cleanup.cancel")}
                  </ButtonItem>
                </PanelSectionRow>
              </>
            )}
          </PanelSection>
        </>
      )}
    </>
  );
};

// Store unpatch function for Steam stores
let unpatchSteamStores: (() => void) | null = null;

export default definePlugin(() => {
  console.log("[Unifideck] Plugin loaded");

  // Apply saved language preference early (loadTranslations uses navigator.language as default)
  call<[], { success: boolean; language: string }>("get_language_preference")
    .then((result) => {
      if (result?.success && result.language && result.language !== "auto") {
        changeLanguage(result.language);
        console.log("[Unifideck] Applied saved language:", result.language);
      }
    })
    .catch(() => {}); // Silently ignore if backend not ready

  // Check for account switch and show modal if needed
  call<[], { show_modal: boolean; has_registry: boolean; has_auth_tokens: boolean }>(
    "check_account_switch",
  )
    .then((result) => {
      if (result?.show_modal) {
        console.log("[Unifideck] Account switch detected, showing modal");
        showModal(
          <AccountSwitchModal
            hasRegistry={result.has_registry}
            hasAuthTokens={result.has_auth_tokens}
            onMigrate={async () => {
              const r = await call<[], { shortcuts_created: number; artwork_copied: number }>(
                "migrate_account_data",
              );
              toaster.toast({
                title: t("accountSwitch.toastMigrateTitle"),
                body: t("accountSwitch.toastMigrateBody", {
                  shortcuts: r.shortcuts_created,
                  artwork: r.artwork_copied,
                }),
              });
              showModal(<SteamRestartModal closeModal={() => {}} />);
            }}
            onClearAuths={async () => {
              await call<[], unknown>("clear_store_auths");
              toaster.toast({
                title: t("accountSwitch.toastClearTitle"),
                body: t("accountSwitch.toastClearBody"),
              });
            }}
            onSkip={() => {
              console.log("[Unifideck] Account switch skipped by user");
            }}
            closeModal={() => {}}
          />,
        );
      }
    })
    .catch(() => {}); // Silently ignore if backend not ready

  // DISABLED: Store patching was causing Steam to hang on startup
  // TODO: Re-enable once we figure out what's breaking Steam
  // loadSteamAppIdMappings().then(() => {
  //   unpatchSteamStores = patchSteamStores();
  // });

  // Share game info cache with PlayButtonOverride and game action interceptor
  setPlayButtonCacheRef(gameInfoCache);
  setGameInfoCacheRef(gameInfoCache);

  // Register game action interceptor (safety net for play button presses on uninstalled games)
  const unregisterInterceptor = registerGameActionInterceptor();
  console.log("[Unifideck] ✓ Game action interceptor registered");

  // Patch the library to add Unifideck tabs (All, Installed, Great on Deck, Steam, Epic, GOG, Amazon)
  // This uses TabMaster's approach: intercept useMemo hook to inject custom tabs
  const libraryPatch = patchLibrary();
  console.log("[Unifideck] ✓ Library tabs patch registered");

  // Patch game details route to inject Install button for uninstalled games
  // v70.3 FIX: Call extracted function to ensure proper Decky loader context
  const patchGameDetails = patchGameDetailsRoute();

  console.log(
    "[Unifideck] ✓ All route patches registered (including game details)",
  );

  // Register force refresh callback to immediately update UI after install/uninstall
  // Debounce map to prevent multiple rapid refreshes for the same app
  const refreshDebounceMap = new Map<number, number>();

  setForceRefreshCallback(async (appId) => {
    console.log(`[Unifideck] Force refreshing UI for app ${appId}`);

    // Check if user is currently viewing this game's details page
    const currentPath = window.location.pathname;
    if (!currentPath.includes(`/library/app/${appId}`)) {
      console.log(`[Unifideck] Not on game details page for app ${appId}, skipping refresh`);
      return;
    }

    // Debounce: ignore if we refreshed this app less than 500ms ago
    const now = Date.now();
    const lastRefresh = refreshDebounceMap.get(appId) || 0;
    if (now - lastRefresh < 500) {
      console.log(`[Unifideck] Debouncing refresh for app ${appId} (last refresh ${now - lastRefresh}ms ago)`);
      return;
    }
    refreshDebounceMap.set(appId, now);

    // Read the updated cache to determine action
    const cached = unifideckGameCache.get(appId);
    const isInstalled = cached?.isInstalled === true;

    // CRITICAL: Clear gameInfoCache for this appId to force components to re-fetch fresh data
    const signedId = appId;
    const unsignedId = signedId < 0 ? signedId + 0x100000000 : signedId;
    const altSignedId = signedId >= 0 && signedId > 0x7fffffff ? signedId - 0x100000000 : signedId;
    gameInfoCache.delete(signedId);
    gameInfoCache.delete(unsignedId);
    if (altSignedId !== signedId) {
      gameInfoCache.delete(altSignedId);
    }
    console.log(`[Unifideck] Cleared gameInfoCache for app ${appId} (all variants)`);

    // Always keep native play section hidden for Unifideck games —
    // our custom section handles all states (install, cancel, play).
    // Re-inject to ensure hide persists after state changes.
    console.log(`[Unifideck] Game ${appId} state changed (installed=${isInstalled}), ensuring CDP hide active`);
    injectHidePlaySectionCDP(appId);

    if (!isInstalled) {
      // Game is now uninstalled → navigate to trigger patcher re-run
      // Navigation as backup to CustomEvent (which handles the immediate update)
      const normalizedPath = currentPath.replace(/^\/routes/, '');
      console.log(`[Unifideck] Navigating back then forward to ${normalizedPath} to trigger patcher`);
      Navigation.NavigateBack();

      setTimeout(() => {
        console.log(`[Unifideck] Navigating forward to ${normalizedPath}`);
        Navigation.Navigate(normalizedPath);
      }, 100);
    }
  });

  console.log("[Unifideck] ✓ Force refresh callback registered");

  // Sync Unifideck Collections on load (with delay to ensure Steam is ready)
  // Automatic collection sync on load removed to prevent crashes
  // Users can manually sync collections from the plugin settings if needed

  // Inject CSS AFTER patches with delay to ensure patches are active
  setTimeout(() => {
    console.log("[Unifideck] Hiding original tabs with CSS");
    const styleElement = document.createElement("style");
    styleElement.id = "unifideck-tab-hider";
    styleElement.textContent = `
      /* Hide original Steam library tabs */
      .library-tabs .tab[data-tab-id="all"],
      .library-tabs .tab[data-tab-id="great-on-deck"],
      .library-tabs .tab[data-tab-id="installed"] {
        display: none !important;
      visibility: hidden !important;
      }

      .library-tabs .tab[data-tab-id="all"].Focusable,
      .library-tabs .tab[data-tab-id="great-on-deck"].Focusable,
      .library-tabs .tab[data-tab-id="installed"].Focusable {
        display: none !important;
      pointer-events: none !important;
      }

      /* Hide navigation links */
      [href="/library/all"],
      [href="/library/great-on-deck"],
      [href="/library/installed"] {
        display: none !important;
      }

      /* Spinning animation for loading indicator */
      @keyframes spin {
        from {transform: rotate(0deg); }
      to {transform: rotate(360deg); }
      }

      .spinning {
        animation: spin 1s linear infinite;
      }

      `;
    document.head.appendChild(styleElement);
    console.log("[Unifideck] ✓ CSS injection complete");
  }, 100); // 100ms delay to ensure patches are active

  // Poll for launcher toasts (first-run notifications from unifideck-launcher)
  // The launcher writes toasts to a JSON file, we read and display them here
  let launcherToastInterval: NodeJS.Timeout | null = null;
  launcherToastInterval = setInterval(async () => {
    try {
      const toasts = await call<
        [],
        Array<{
          title: string;
          body: string;
          urgency?: string;
          timestamp?: number;
        }>
      >("get_launcher_toasts");

      if (toasts && toasts.length > 0) {
        for (const toast of toasts) {
          // Parse body params (format: "key|param1=val1|param2=val2")
          let bodyKey = toast.body;
          let bodyParams: Record<string, any> = {};

          if (bodyKey.includes("|")) {
            const parts = bodyKey.split("|");
            bodyKey = parts[0];
            for (let i = 1; i < parts.length; i++) {
              const [k, v] = parts[i].split("=");
              if (k && v) {
                bodyParams[k] = v;
              }
            }
          }

          toaster.toast({
            title: `${t("toasts.unifideck")} ${t(toast.title)}`,
            body: String(t(bodyKey, bodyParams)),
            duration: toast.urgency === "critical" ? 10000 : 5000,
            critical: toast.urgency === "critical",
          });
        }
        console.log(`[Unifideck] Displayed ${toasts.length} launcher toast(s)`);
      }
    } catch (error) {
      // Silently ignore errors - launcher toasts are non-critical
    }
  }, 1500); // Check every 1.5 seconds

  // Store interval ID for cleanup
  (window as any).__unifideck_toast_interval = launcherToastInterval;

  // Background sync disabled - users manually sync via UI when needed
  console.log("[Unifideck] Background sync disabled (use manual sync button)");

  return {
    name: "UNIFIDECK",
    icon: <FaGamepad />,
    content: (
      <I18nextProvider i18n={i18n}>
        <Content />
      </I18nextProvider>
    ),
    onDismount() {
      console.log("[Unifideck] Plugin unloading");

      // Unregister game action interceptor
      unregisterInterceptor();

      // Unpatch Steam stores
      if (unpatchSteamStores) {
        unpatchSteamStores();
        unpatchSteamStores = null;
      }

      // Stop launcher toast polling
      const toastInterval = (window as any).__unifideck_toast_interval;
      if (toastInterval) {
        clearInterval(toastInterval);
        (window as any).__unifideck_toast_interval = null;
      }

      // Remove CSS injections
      const styleEl = document.getElementById("unifideck-tab-hider");
      if (styleEl) {
        styleEl.remove();
      }
      // CDP cleanup happens in backend _unload() method

      // Remove route patches
      routerHook.removePatch("/library", libraryPatch);
      routerHook.removePatch("/library/app/:appid", patchGameDetails);

      // Clear game info cache
      gameInfoCache.clear();

      // Stop background sync service
      call("stop_background_sync").catch((error) =>
        console.error("[Unifideck] Failed to stop background sync:", error),
      );
    },
  };
});
