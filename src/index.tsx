import { definePlugin, call, toaster, routerHook } from "@decky/api";
import { PanelSection, PanelSectionRow, ButtonItem, staticClasses, afterPatch, findInReactTree, createReactTreePatcher, appDetailsClasses, appActionButtonClasses, playSectionClasses, appDetailsHeaderClasses, DialogButton, Focusable, ToggleField, showModal, ConfirmModal } from "@decky/ui";
import React, { VFC, useState, useEffect, useRef } from "react";
import { FaGamepad, FaSync, FaTrash } from "react-icons/fa";

// Import views
import { UnifiedLibraryView } from "./views/UnifiedLibraryView";

// Import tab system
import { patchLibrary, tabManager } from "./tabs";

import { syncUnifideckCollections } from "./spoofing/CollectionManager";

// Import Downloads feature components
import { DownloadsTab } from "./components/DownloadsTab";
import { StorageSettings } from "./components/StorageSettings";

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

// Install Overlay Component - modal shown when Install button clicked
const InstallOverlayComponent: VFC<{
  appId: number;
  gameInfo: any;
  onClose: () => void;
  onInstallComplete: () => void;
}> = ({ appId, gameInfo, onClose, onInstallComplete }) => {
  const [isInstalling, setIsInstalling] = useState(false);

  const handleInstall = async () => {
    setIsInstalling(true);
    const result = await call<[number], any>("install_game_by_appid", appId);

    if (result.success) {
      onInstallComplete();
    } else {
      toaster.toast({
        title: "Installation Failed",
        body: result.error || "Unknown error",
        duration: 10000,
        critical: true,
      });
      setIsInstalling(false);
    }
  };

  return (
    <div
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        backgroundColor: 'rgba(0, 0, 0, 0.8)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 10000
      }}
      onClick={onClose}
    >
      <div
        style={{
          backgroundColor: '#1e2329',
          padding: '30px',
          borderRadius: '8px',
          minWidth: '400px',
          border: '2px solid #1a9fff'
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ marginBottom: '15px', fontSize: '20px', fontWeight: 'bold', color: '#fff' }}>
          Game Not Installed
        </div>
        <div style={{ marginBottom: '20px', fontSize: '16px', color: '#ccc' }}>
          {gameInfo.title}
        </div>
        {gameInfo.size_formatted && (
          <div style={{ marginBottom: '25px', fontSize: '14px', color: '#888' }}>
            Download Size: {gameInfo.size_formatted}
          </div>
        )}
        <ButtonItem onClick={handleInstall} disabled={isInstalling}>
          {isInstalling ? 'Installing...' : 'Install Now'}
        </ButtonItem>
      </div>
    </div>
  );
};

// Install Button Component - Simple button with self-contained state
const InstallButton: VFC<{ appId: number }> = ({ appId }) => {
  const [gameInfo, setGameInfo] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [processing, setProcessing] = useState(false);

  // Fetch game info on mount
  useEffect(() => {
    const cached = gameInfoCache.get(appId);
    if (cached && (Date.now() - cached.timestamp) < CACHE_TTL) {
      setGameInfo(cached.info);
      setLoading(false);
      return;
    }

    call<[number], any>("get_game_info", appId)
      .then(info => {
        const processedInfo = info?.error ? null : info;
        setGameInfo(processedInfo);
        gameInfoCache.set(appId, { info: processedInfo, timestamp: Date.now() });
      })
      .catch(() => setGameInfo(null))
      .finally(() => setLoading(false));
  }, [appId]);

  const handleAction = async () => {
    if (!gameInfo) return;

    setProcessing(true);
    const isUninstall = gameInfo.is_installed;
    const actionName = isUninstall ? "Uninstall" : "Install";

    toaster.toast({
      title: `${actionName}ing Game`,
      body: isUninstall ? `Removing ${gameInfo.title}...` : `Downloading ${gameInfo.title}...`,
      duration: 5000,
    });

    const method = isUninstall ? "uninstall_game_by_appid" : "install_game_by_appid";
    const result = await call<[number], any>(method, appId);

    if (result.success) {
      // Toggle installed state locally
      const newState = !isUninstall;
      setGameInfo({ ...gameInfo, is_installed: newState });
      gameInfoCache.delete(appId);

      toaster.toast({
        title: `${actionName}ation Complete!`,
        body: `${gameInfo.title} ${isUninstall ? 'uninstalled' : 'installed'}.\n\n⚠️ Restart Steam to update library.`,
        duration: 20000,
        critical: true,
      });
    } else {
      toaster.toast({
        title: `${actionName}ation Failed`,
        body: result.error || "Unknown error",
        duration: 10000,
        critical: true,
      });
    }
    setProcessing(false);
  };

  // Don't show if not a Unifideck game
  if (!gameInfo || gameInfo.error) return null;

  // Show different states
  if (loading) {
    return (
      <ButtonItem disabled>
        <FaSync className="spinning" /> Checking...
      </ButtonItem>
    );
  }

  return (
    <div style={gameInfo.is_installed ? { backgroundColor: 'rgba(255, 60, 60, 0.2)', borderRadius: '4px' } : undefined}>
      <ButtonItem
        onClick={handleAction}
        disabled={processing}
      >
        {processing
          ? (gameInfo.is_installed ? 'Uninstalling...' : 'Installing...')
          : (gameInfo.is_installed ? 'Uninstall' : 'Install')}
      </ButtonItem>
    </div>
  );
};

// ========== END INSTALL BUTTON FEATURE ==========

// ========== NATIVE PLAY BUTTON OVERRIDE ==========
// 
// This component shows alongside the native Play button for uninstalled Unifideck games.
// For installed games, we hide this and let Steam's native Play button work.
// For uninstalled games, we show an Install button with size info.
//
// ================================================

// Install Info Display Component - shows download size next to play section
const InstallInfoDisplay: VFC<{ appId: number }> = ({ appId }) => {
  const [gameInfo, setGameInfo] = useState<any>(null);
  const [processing, setProcessing] = useState(false);
  const [downloadState, setDownloadState] = useState<{
    isDownloading: boolean;
    progress?: number;
    downloadId?: string;
  }>({ isDownloading: false });
  const pollIntervalRef = useRef<NodeJS.Timeout | null>(null);

  // Fetch game info on mount
  useEffect(() => {
    const cached = gameInfoCache.get(appId);
    if (cached && (Date.now() - cached.timestamp) < CACHE_TTL) {
      setGameInfo(cached.info);
      return;
    }

    call<[number], any>("get_game_info", appId)
      .then(info => {
        const processedInfo = info?.error ? null : info;
        setGameInfo(processedInfo);
        gameInfoCache.set(appId, { info: processedInfo, timestamp: Date.now() });
      })
      .catch(() => setGameInfo(null));
  }, [appId]);

  // Poll for download state when we have game info
  useEffect(() => {
    if (!gameInfo) return;

    const checkDownloadState = async () => {
      try {
        const result = await call<[string, string], {
          success: boolean;
          is_downloading: boolean;
          download_info?: {
            id: string;
            progress_percent: number;
            status: string;
          };
        }>("is_game_downloading", gameInfo.game_id, gameInfo.store);

        setDownloadState(prevState => {
          const newState = {
            isDownloading: false,
            progress: 0,
            downloadId: undefined as string | undefined
          };

          if (result.success && result.is_downloading && result.download_info) {
            const status = result.download_info.status;
            // Only show as downloading if status is actively downloading or queued
            // Cancelled/error items should not be shown as active downloads
            if (status === 'downloading' || status === 'queued') {
              newState.isDownloading = true;
              newState.progress = result.download_info.progress_percent;
              newState.downloadId = result.download_info.id;
            }
            // If status is cancelled/error/completed, isDownloading stays false
          }

          // Detect transition from Downloading -> Not Downloading (Completion)
          if (prevState.isDownloading && !newState.isDownloading) {
            console.log("[InstallInfoDisplay] Download finished, refreshing game info...");

            // Invalidate cache first to ensure fresh data
            gameInfoCache.delete(appId);

            // Refresh game info to update button state (Install -> Play/Uninstall)
            call<[number], any>("get_game_info", appId)
              .then(info => {
                const processedInfo = info?.error ? null : info;
                setGameInfo(processedInfo);
                if (processedInfo) {
                  gameInfoCache.set(appId, { info: processedInfo, timestamp: Date.now() });
                }
              });
          }

          return newState;
        });

      } catch (error) {
        console.error("[InstallInfoDisplay] Error checking download state:", error);
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

  const handleInstall = async () => {
    if (!gameInfo) return;
    setProcessing(true);

    // Queue download instead of direct install
    const result = await call<[number], any>("add_to_download_queue_by_appid", appId);

    if (result.success) {
      toaster.toast({
        title: "Download Started. Check UNIFIDECK > Downloads",
        body: `${gameInfo.title} has been added to the queue.`,
        duration: 5000,
      });

      // Show multi-part alert for GOG games with multiple installer parts
      if (result.is_multipart) {
        toaster.toast({
          title: "Multi-Part Download Detected",
          body: "Please be patient and wait for completion",
          duration: 8000,
        });
      }

      // Force immediate state check to update UI to "Cancel" faster
      setDownloadState(prev => ({ ...prev, isDownloading: true, progress: 0 }));
    } else {
      toaster.toast({
        title: "Download Failed",
        body: result.error || "Could not start download.",
        duration: 10000,
        critical: true,
      });
    }
    setProcessing(false);
  };

  const handleCancel = async () => {
    // If we don't have a specific download ID yet (race condition at start), try to construct it
    const dlId = downloadState.downloadId || `${gameInfo.store}:${gameInfo.game_id}`;

    setProcessing(true);

    const result = await call<[string], { success: boolean; error?: string }>(
      "cancel_download_by_id",
      dlId
    );

    if (result.success) {
      toaster.toast({
        title: "Download Cancelled",
        body: `${gameInfo?.title} download cancelled.`,
        duration: 5000,
      });
      setDownloadState({ isDownloading: false, progress: 0 });
    } else {
      toaster.toast({
        title: "Cancel Failed",
        body: result.error || "Could not cancel download.",
        duration: 5000,
        critical: true,
      });
    }
    setProcessing(false);
  };

  const handleUninstall = async () => {
    if (!gameInfo) return;
    setProcessing(true);

    toaster.toast({
      title: "Uninstalling Game",
      body: `Removing ${gameInfo.title}...`,
      duration: 5000,
    });

    const result = await call<[number], any>("uninstall_game_by_appid", appId);

    if (result.success) {
      setGameInfo({ ...gameInfo, is_installed: false });
      gameInfoCache.delete(appId);
      toaster.toast({
        title: "Uninstallation Complete!",
        body: `${gameInfo.title} removed.`,
        duration: 10000,
      });
    } else {
      toaster.toast({
        title: "Uninstallation Failed",
        body: result.error || "Unknown error",
        duration: 10000,
        critical: true,
      });
    }
    setProcessing(false);
  };

  // Confirmation wrapper functions using native Steam modal
  const showInstallConfirmation = () => {
    showModal(
      <ConfirmModal
        strTitle="Confirm Installation"
        strDescription={`Are you sure you want to install ${gameInfo?.title}?`}
        strOKButtonText="Yes"
        strCancelButtonText="No"
        onOK={() => handleInstall()}
      />
    );
  };

  const showUninstallConfirmation = () => {
    showModal(
      <ConfirmModal
        strTitle="Confirm Uninstallation"
        strDescription={`Are you sure you want to uninstall ${gameInfo?.title}?`}
        strOKButtonText="Yes"
        strCancelButtonText="No"
        bDestructiveWarning={true}
        onOK={() => handleUninstall()}
      />
    );
  };

  const showCancelConfirmation = () => {
    showModal(
      <ConfirmModal
        strTitle="Confirm Cancellation"
        strDescription={`Are you sure you want to cancel the download for ${gameInfo?.title}?`}
        strOKButtonText="Yes"
        strCancelButtonText="No"
        bDestructiveWarning={true}
        onOK={() => handleCancel()}
      />
    );
  };

  // Not a Unifideck game - return null
  if (!gameInfo || gameInfo.error) return null;

  const isInstalled = gameInfo.is_installed;

  // Determine button display based on state
  let buttonText: string;
  let buttonAction: () => void;
  // Dynamic style based on state
  let buttonStyle: React.CSSProperties = {
    padding: '8px 12px',
    minHeight: '42px',
    boxShadow: 'none',
    borderBottom: 'none',
  };

  if (downloadState.isDownloading) {
    // Show "Cancel" button with progress during active download
    // Use Math.max(0) to avoid negative -1 initialization
    const progress = Math.max(0, downloadState.progress || 0).toFixed(0);
    buttonText = `✖ Cancel (${progress}%)`;
    buttonAction = showCancelConfirmation;

    buttonStyle = {
      ...buttonStyle,
      backgroundColor: 'rgba(200, 40, 40, 0.4)', // Red tint for cancel
      border: '1px solid #ff4444',
    };
  } else if (isInstalled) {
    // Show size for installed games if available
    const sizeText = gameInfo.size_formatted ? ` (${gameInfo.size_formatted})` : ' (- GB)';
    buttonText = `Uninstall ${gameInfo.title}${sizeText}`;
    buttonAction = showUninstallConfirmation;
  } else {
    // Show size in Install button
    const sizeText = gameInfo.size_formatted ? ` (${gameInfo.size_formatted})` : ' (- GB)';
    buttonText = `⬇ Install ${gameInfo.title}${sizeText}`;
    buttonAction = showInstallConfirmation;
  }

  return (
    <>      {/* Install/Uninstall/Cancel Button */}
      <Focusable
        style={{
          position: 'absolute',
          top: '40px',  // Aligned with ProtonDB badge row
          right: '35px',
          zIndex: 1000,
        }}
      >
        <DialogButton
          onClick={buttonAction}
          disabled={processing}
          style={buttonStyle}
        >
          {processing ? 'Processing...' : buttonText}
        </DialogButton>
      </Focusable>
    </>
  );
};


// Patch function for game details route - EXTRACTED TO MODULE SCOPE (ProtonDB/HLTB pattern)
// This ensures the patch is registered in the correct Decky loader context
function patchGameDetailsRoute() {
  return routerHook.addPatch(
    '/library/app/:appid',
    (routerTree: any) => {
      const routeProps = findInReactTree(routerTree, (x: any) => x?.renderFunc);
      if (!routeProps) return routerTree;

      // Create tree patcher (SAFE: mutates BEFORE React reconciles)
      const patchHandler = createReactTreePatcher([
        // Finder function: return children array (NOT overview object) - ProtonDB pattern
        (tree) => findInReactTree(tree, (x: any) =>
          x?.props?.children?.props?.overview
        )?.props?.children
      ], (_, ret) => {
        // Patcher function: SAFE to mutate here (before reconciliation)
        // Extract appId from ret (not from finder closure)
        const overview = findInReactTree(ret, (x: any) =>
          x?.props?.children?.props?.overview
        )?.props?.children?.props?.overview;

        if (!overview) return ret;
        const appId = overview.appid;

        try {
          // Strategy: Find the Header area (contains Play button and game info)
          // The Header is at the top of the game details page, above the scrollable content

          // Look for the AppDetailsHeader container first (best position)
          const headerContainer = findInReactTree(ret, (x: any) =>
            Array.isArray(x?.props?.children) &&
            (x?.props?.className?.includes(appDetailsClasses?.Header) ||
              x?.props?.className?.includes(appDetailsHeaderClasses?.TopCapsule))
          );

          // Find the PlaySection container (where Play button lives)
          const playSection = findInReactTree(ret, (x: any) =>
            Array.isArray(x?.props?.children) &&
            x?.props?.className?.includes(playSectionClasses?.Container)
          );

          // Alternative: Find the AppButtonsContainer
          const buttonsContainer = findInReactTree(ret, (x: any) =>
            Array.isArray(x?.props?.children) &&
            x?.props?.className?.includes(playSectionClasses?.AppButtonsContainer)
          );

          // Find the game info row (typically contains play button, shortcuts, settings)
          const gameInfoRow = findInReactTree(ret, (x: any) =>
            Array.isArray(x?.props?.children) &&
            x?.props?.style?.display === 'flex' &&
            x?.props?.children?.some?.((c: any) =>
              c?.props?.className?.includes?.(appActionButtonClasses?.PlayButtonContainer) ||
              c?.type?.toString?.()?.includes?.('PlayButton')
            )
          );

          // Find InnerContainer as fallback (original approach)
          const innerContainer = findInReactTree(ret, (x: any) =>
            Array.isArray(x?.props?.children) &&
            x?.props?.className?.includes(appDetailsClasses?.InnerContainer)
          );

          // ProtonDB COMPATIBILITY: Always use InnerContainer first to match ProtonDB's behavior
          // When multiple plugins modify the SAME container, patches chain correctly.
          // When plugins modify DIFFERENT containers (parent vs child), React reconciliation conflicts occur.
          // Since InstallInfoDisplay uses position: absolute, it works in any container.
          let container = innerContainer || headerContainer || playSection || buttonsContainer || gameInfoRow;

          // If none of those work, log but try to proceed with whatever we have (or return)
          if (!container) {
            console.log(`[Unifideck] No suitable container found for app ${appId}, skipping injection`);
            return ret;
          }

          // Ensure children is an array
          if (!Array.isArray(container.props.children)) {
            container.props.children = [container.props.children];
          }

          // ProtonDB COMPATIBILITY: Insert at index 2
          // ProtonDB inserts at index 1. By inserting at index 2, we:
          // 1. Avoid overwriting ProtonDB's element
          // 2. Stay early in the children array so focus navigation works
          // Since InstallInfoDisplay uses position: absolute, its visual position is CSS-controlled.
          const spliceIndex = Math.min(2, container.props.children.length);

          // Inject our install info display after play button
          container.props.children.splice(
            spliceIndex,
            0,
            React.createElement(InstallInfoDisplay, {
              key: `unifideck-install-info-${appId}`,
              appId
            })
          );

          console.log(`[Unifideck] Injected install info for app ${appId} in ${innerContainer ? 'InnerContainer' : headerContainer ? 'Header' : playSection ? 'PlaySection' : buttonsContainer ? 'ButtonsContainer' : 'GameInfoRow'} at index ${spliceIndex}`);


        } catch (error) {
          console.error('[Unifideck] Error injecting install info:', error);
        }

        return ret; // Always return modified tree
      });

      // Apply patcher to renderFunc
      afterPatch(routeProps, 'renderFunc', patchHandler);

      return routerTree;
    }
  );
}

// Settings panel in Quick Access Menu
const Content: VFC = () => {
  // Tab navigation state
  const [activeTab, setActiveTab] = useState<'settings' | 'downloads'>('settings');

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
        const focusable = mountRef.current.querySelector('button, [tabindex="0"]');
        if (focusable instanceof HTMLElement) {
          focusable.focus();
          focusable.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      }
    }, 100);
    return () => clearTimeout(timer);
  }, []);

  const [storeStatus, setStoreStatus] = useState({
    epic: "Checking...",
    gog: "Checking...",
    amazon: "Checking...",
  });
  const [authDialog, setAuthDialog] = useState<{
    show: boolean;
    store: 'epic' | 'gog' | 'amazon' | null;
    url: string;
    code: string;
    processing: boolean;
    error: string;
    autoMode: boolean;
  }>({
    show: false,
    store: null,
    url: '',
    code: '',
    processing: false,
    error: '',
    autoMode: false
  });
  const [syncProgress, setSyncProgress] = useState<{
    total_games: number;
    synced_games: number;
    current_game: string;
    status: string;
    progress_percent: number;
    error?: string;
    // Artwork tracking fields
    artwork_total?: number;
    artwork_synced?: number;
    current_phase?: string;
  } | null>(null);

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
        const status = await call<[], {
          is_syncing: boolean;
          sync_progress: {
            total_games: number;
            synced_games: number;
            current_game: string;
            status: string;
            progress_percent: number;
            error?: string;
            artwork_total?: number;
            artwork_synced?: number;
            current_phase?: string;
          } | null;
        }>("get_sync_status");

        if (status.is_syncing && status.sync_progress) {
          console.log("[Unifideck] Restoring sync state on mount:", status.sync_progress);

          // Restore syncing state
          setSyncing(true);
          setSyncProgress(status.sync_progress);

          // Deduplication flag (scoped to this restore)
          let completionHandled = false;

          // Clear any existing polling interval before creating a new one
          if (pollIntervalRef.current) {
            clearInterval(pollIntervalRef.current);
            console.log("[Unifideck] Cleared existing polling interval before restore");
          }

          // Resume polling for progress
          pollIntervalRef.current = setInterval(async () => {
            try {
              const result = await call<[], {
                success?: boolean;
                total_games: number;
                synced_games: number;
                current_game: string;
                status: string;
                progress_percent: number;
                error?: string;
                artwork_total?: number;
                artwork_synced?: number;
                current_phase?: string;
              }>("get_sync_progress");

              if (result.success) {
                setSyncProgress(result);

                // Log progress updates
                if (result.current_game) {
                  const progress = result.current_phase === 'artwork'
                    ? `${result.artwork_synced}/${result.artwork_total}`
                    : `${result.synced_games}/${result.total_games}`;
                  console.log(`[Unifideck] ${result.current_game} (${progress})`);
                }

                // Stop polling when complete, error, or cancelled
                if (result.status === 'complete' || result.status === 'error' || result.status === 'cancelled') {
                  if (pollIntervalRef.current) {
                    clearInterval(pollIntervalRef.current);
                    pollIntervalRef.current = null;
                  }
                  setSyncing(false);

                  // Only run completion logic once
                  if (!completionHandled) {
                    completionHandled = true;

                    if (result.status === 'complete') {
                      console.log(`[Unifideck] ✓ Sync completed: ${result.synced_games} games processed`);
                    } else if (result.status === 'cancelled') {
                      console.log(`[Unifideck] ⚠ Sync cancelled by user`);
                    }

                    // Show toast only if changes were made
                    if (result.status === 'complete') {
                      const addedCount = result.synced_games || 0;
                      if (addedCount > 0) {
                        toaster.toast({
                          title: "Sync Complete!",
                          body: `Added ${addedCount} games. RESTART STEAM (exit completely, not just return to game mode) to see them in your library.`,
                          duration: 15000,
                          critical: true,
                        });
                      }
                    } else if (result.status === 'cancelled') {
                      toaster.toast({
                        title: "Sync Cancelled",
                        body: result.current_game || "Sync was cancelled by user",
                        duration: 5000,
                      });
                    }

                    // Start cooldown
                    setSyncCooldown(true);
                    setCooldownSeconds(5);

                    const cooldownInterval = setInterval(() => {
                      setCooldownSeconds(prev => {
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
        setTimeout(() => reject(new Error('Status check timed out')), 10000)
      );

      const checkPromise = call<[], {
        success: boolean;
        epic: string;
        gog: string;
        amazon: string;
        error?: string;
        legendary_installed?: boolean;
        nile_installed?: boolean;
      }>("check_store_status");

      const result = await Promise.race([checkPromise, timeoutPromise]) as any;

      if (result.success) {
        setStoreStatus({
          epic: result.epic,
          gog: result.gog,
          amazon: result.amazon
        });

        // Show warning if legendary not installed
        if (result.legendary_installed === false) {
          console.warn("[Unifideck] Legendary CLI not installed - Epic Games won't work");
        }
        // Show warning if nile not installed
        if (result.nile_installed === false) {
          console.warn("[Unifideck] Nile CLI not installed - Amazon Games won't work");
        }
      } else {
        console.error("[Unifideck] Status check failed:", result.error);
        setStoreStatus({
          epic: "Error - Check logs",
          gog: "Error - Check logs",
          amazon: "Error - Check logs"
        });
      }
    } catch (error) {
      console.error("[Unifideck] Error checking store status:", error);
      setStoreStatus({
        epic: "Error - " + (error as Error).message,
        gog: "Error - " + (error as Error).message,
        amazon: "Error - " + (error as Error).message
      });
    }
  };

  const handleManualSync = async (force: boolean = false) => {
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
      console.log("[Unifideck] Cleared existing polling interval before manual sync");
    }

    // Start polling for progress
    pollIntervalRef.current = setInterval(async () => {
      try {
        const result = await call<[], {
          success?: boolean;
          total_games: number;
          synced_games: number;
          current_game: string;
          status: string;
          progress_percent: number;
          error?: string;
          artwork_total?: number;
          artwork_synced?: number;
          current_phase?: string;
        }>("get_sync_progress");

        if (result.success) {
          setSyncProgress(result);

          // Log progress updates
          if (result.current_game) {
            const progress = result.current_phase === 'artwork'
              ? `${result.artwork_synced}/${result.artwork_total}`
              : `${result.synced_games}/${result.total_games}`;
            console.log(`[Unifideck] ${result.current_game} (${progress})`);
          }
        }

        // Stop polling when complete, error, or cancelled
        if (result.status === 'complete' || result.status === 'error' || result.status === 'cancelled') {
          if (pollIntervalRef.current) {
            clearInterval(pollIntervalRef.current);
            pollIntervalRef.current = null;
          }
          setSyncing(false);

          // CRITICAL FIX: Only run completion logic ONCE
          if (!completionHandled) {
            completionHandled = true;  // Set flag IMMEDIATELY

            if (result.status === 'complete') {
              console.log(`[Unifideck] ✓ Sync completed: ${result.synced_games} games processed`);
            } else if (result.status === 'cancelled') {
              console.log(`[Unifideck] ⚠ Sync cancelled by user`);
            }

            // Show restart notification when sync completes (only if changes were made)
            if (result.status === 'complete') {
              // Only show toast if there were actual changes (not just a refresh that added 0 games)
              const addedCount = result.synced_games || 0;
              if (addedCount > 0) {
                toaster.toast({
                  title: force ? "Force Sync Complete!" : "Sync Complete!",
                  body: force
                    ? `Updated ${addedCount} games. RESTART STEAM to see changes.`
                    : `Added ${addedCount} games. RESTART STEAM (exit completely, not just return to game mode) to see them in your library.`,
                  duration: 15000,
                  critical: true,
                });
              }
            } else if (result.status === 'cancelled') {
              toaster.toast({
                title: "Sync Cancelled",
                body: result.current_game || "Sync was cancelled by user",
                duration: 5000,
              });
            }
          } else {
            // Completion already handled by another poll - do nothing
            console.log(`[Unifideck] (duplicate poll detected, skipping completion logic)`);
          }
        }
      } catch (error) {
        console.error("[Unifideck] Error getting sync progress:", error);
      }
    }, 500); // Poll every 500ms

    try {
      // Use force_sync_libraries for force sync (rewrites shortcuts and compatibility data)
      const methodName = force ? "force_sync_libraries" : "sync_libraries";
      console.log(`[Unifideck] Starting ${force ? 'force ' : ''}sync...`);

      const syncResult = await call<[], {
        success: boolean;
        epic_count: number;
        gog_count: number;
        amazon_count: number;
        added_count: number;
        artwork_count: number;
        updated_count?: number;
      }>(methodName);

      console.log("[Unifideck] ========== SYNC COMPLETED ==========");
      console.log(`[Unifideck] Epic Games: ${syncResult.epic_count}`);
      console.log(`[Unifideck] GOG Games: ${syncResult.gog_count}`);
      console.log(`[Unifideck] Amazon Games: ${syncResult.amazon_count || 0}`);
      console.log(`[Unifideck] Total Games: ${syncResult.epic_count + syncResult.gog_count + (syncResult.amazon_count || 0)}`);
      console.log(`[Unifideck] Games Added: ${syncResult.added_count}`);
      console.log(`[Unifideck] Artwork Fetched: ${syncResult.artwork_count}`);
      console.log("[Unifideck] =====================================");

      // Phase 3: Sync Steam Collections
      // Update collections ([Unifideck] Epic Games, etc.) with new games
      await syncUnifideckCollections().catch(err =>
        console.error("[Unifideck] Failed to sync collections:", err)
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
        setCooldownSeconds(prev => {
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
  const pollForAuthCompletion = async (store: 'epic' | 'gog' | 'amazon'): Promise<boolean> => {
    const maxAttempts = 60; // 5 minutes (60 * 5s)
    let attempts = 0;

    // Helper function to check status
    const checkStatus = async (): Promise<boolean> => {
      try {
        const result = await call<[], {
          success: boolean;
          epic: string;
          gog: string;
          amazon: string;
        }>("check_store_status");

        if (result.success) {
          let status: string;
          if (store === 'epic') {
            status = result.epic;
          } else if (store === 'gog') {
            status = result.gog;
          } else {
            status = result.amazon;
          }
          if (status === "Connected") {
            console.log(`[Unifideck] ${store} authentication completed automatically!`);
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
          console.log(`[Unifideck] Polling timeout for ${store} authentication`);
          resolve(false);
        }
      }, 5000); // Poll every 5 seconds
    });
  };

  const startAuth = async (store: 'epic' | 'gog' | 'amazon') => {
    try {
      let methodName: string;
      if (store === 'epic') {
        methodName = 'start_epic_auth';
      } else if (store === 'gog') {
        methodName = 'start_gog_auth_auto';
      } else {
        methodName = 'start_amazon_auth';
      }

      const result = await call<[], { success: boolean; url?: string; message?: string; error?: string }>(
        methodName
      );

      if (result.success && result.url) {
        const authUrl = result.url;

        // Open popup window
        const popup = window.open(authUrl, '_blank', 'width=800,height=600,popup=yes');

        if (!popup) {
          setAuthDialog({
            show: true,
            store,
            url: authUrl,
            code: '',
            processing: false,
            error: 'Failed to open popup window - popup may be blocked',
            autoMode: false
          });
          return;
        }

        console.log(`[Unifideck] Opened ${store} auth popup. Backend monitoring via CDP...`);

        // Show dialog indicating we're waiting
        setAuthDialog({
          show: true,
          store,
          url: authUrl,
          code: '',
          processing: true,
          error: '',
          autoMode: true
        });

        // Poll for authentication completion
        const completed = await pollForAuthCompletion(store);

        if (completed) {
          toaster.toast({
            title: "Auth Successful",
            body: "You can close the window",
            duration: 5000,
          });
          setAuthDialog({ show: false, store: null, url: '', code: '', processing: false, error: '', autoMode: false });
          await checkStoreStatus(); // Refresh status
        } else {
          setAuthDialog(prev => ({
            ...prev,
            processing: false,
            error: 'Authentication timeout - please check logs or try again'
          }));
        }
      } else {
        setAuthDialog({
          show: true,
          store,
          url: '',
          code: '',
          processing: false,
          error: result.error || 'Failed to start authentication',
          autoMode: false
        });
      }
    } catch (error: any) {
      console.error(`[Unifideck] Error starting ${store} auth:`, error);
      setAuthDialog({
        show: true,
        store,
        url: '',
        code: '',
        processing: false,
        error: `Error: ${error.message || error}`,
        autoMode: false
      });
    }
  };

  const handleLogout = async (store: 'epic' | 'gog' | 'amazon') => {
    try {
      let methodName: string;
      if (store === 'epic') {
        methodName = 'logout_epic';
      } else if (store === 'gog') {
        methodName = 'logout_gog';
      } else {
        methodName = 'logout_amazon';
      }
      const result = await call<[], { success: boolean; message?: string }>(
        methodName
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
      const result = await call<[{ delete_files: boolean }], {
        success: boolean;
        deleted_games: number;
        deleted_artwork: number;
        deleted_files_count: number;
        preserved_shortcuts: number;
        error?: string;
      }>("perform_full_cleanup", { delete_files: deleteFiles });

      // Reset checkbox
      setDeleteFiles(false);

      if (result.success) {
        console.log(`[Unifideck] Cleanup complete: ${result.deleted_games} games, ` +
          `${result.deleted_artwork} artwork sets, ${result.deleted_files_count} files deleted`);

        toaster.toast({
          title: "Cleanup Successful",
          body: `Removed ${result.deleted_games} games, ${result.deleted_artwork} artwork sets, ` +
            `and ${result.deleted_files_count} file directories. Auth & cache cleared.`,
          duration: 8000,
        });
      } else {
        console.error(`[Unifideck] Delete failed: ${result.error}`);
        toaster.toast({
          title: "Delete Failed",
          body: result.error || "Unknown error",
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

      const result = await call<[], {
        success: boolean;
        message: string;
      }>("cancel_sync");

      if (result.success) {
        console.log("[Unifideck] Sync cancelled");
        toaster.toast({
          title: "UNIFIDECK SYNC CANCELED",
          body: "",
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
      {/* Tab Navigation - Vertical Stack Layout */}
      <PanelSection>



        <PanelSectionRow>
          <div ref={mountRef} style={{ width: "100%" }}>
            <Focusable
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "4px",
                width: "100%",
              }}
            >
              <DialogButton
                onClick={() => setActiveTab('settings')}
                style={{
                  width: "100%",
                  padding: "8px 12px",
                  fontSize: "13px",
                  backgroundColor: activeTab === 'settings' ? '#1a9fff' : 'transparent',
                  border: activeTab === 'settings' ? 'none' : '1px solid #444',
                  borderRadius: '4px',
                  fontWeight: activeTab === 'settings' ? 'bold' : 'normal',
                  textAlign: "left",
                  justifyContent: "flex-start",
                  display: "flex",
                  alignItems: "center",
                }}
              >
                ⚙️ Settings
              </DialogButton>
              <DialogButton
                onClick={() => setActiveTab('downloads')}
                style={{
                  width: "100%",
                  padding: "8px 12px",
                  fontSize: "13px",
                  backgroundColor: activeTab === 'downloads' ? '#1a9fff' : 'transparent',
                  border: activeTab === 'downloads' ? 'none' : '1px solid #444',
                  borderRadius: '4px',
                  fontWeight: activeTab === 'downloads' ? 'bold' : 'normal',
                  textAlign: "left",
                  justifyContent: "flex-start",
                  display: "flex",
                  alignItems: "center",
                }}
              >
                ⬇️ Downloads
              </DialogButton>
            </Focusable>
          </div>
        </PanelSectionRow>
      </PanelSection>

      {/* Downloads Tab */}
      {activeTab === 'downloads' && (
        <>
          <DownloadsTab />
          <StorageSettings />
        </>
      )}

      {/* Settings Tab */}
      {activeTab === 'settings' && (
        <>
          <PanelSection title="Unifideck Settings">
            <PanelSectionRow>
              <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                <div>
                  Add Epic, GOG, and Amazon games to your Steam Deck library.
                </div>
                <div style={{ fontSize: "12px", opacity: 0.7 }}>
                  All your games under one roof.
                </div>
              </div>
            </PanelSectionRow>
          </PanelSection>

          {/* Epic Games Store Section */}
          <PanelSection title="Epic Games">
            <PanelSectionRow>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}>
                <div style={{ fontSize: "14px" }}>
                  Status: {
                    storeStatus.epic === "Connected" ? "✓ Connected" :
                      storeStatus.epic === "Legendary not installed" ? "⚠️ Installing..." :
                        storeStatus.epic === "Checking..." ? "Checking..." :
                          storeStatus.epic.includes("Error") ? `❌ ${storeStatus.epic}` :
                            "✗ Not Connected"
                  }
                </div>
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <div>
                {storeStatus.epic === "Connected" ? (
                  <ButtonItem layout="below" onClick={() => handleLogout('epic')}>
                    <div style={{ fontSize: "0.85em", padding: "2px" }}>Logout</div>
                  </ButtonItem>
                ) : storeStatus.epic !== "Checking..." && !storeStatus.epic.includes("Error") && storeStatus.epic !== "Legendary not installed" ? (
                  <ButtonItem layout="below" onClick={() => startAuth('epic')}>
                    <div style={{ fontSize: "0.85em", padding: "2px" }}>Authenticate</div>
                  </ButtonItem>
                ) : null}
              </div>
            </PanelSectionRow>
            {storeStatus.epic === "Legendary not installed" && (
              <PanelSectionRow>
                <div style={{ fontSize: '11px', opacity: 0.7 }}>
                  Installing legendary CLI automatically...
                </div>
              </PanelSectionRow>
            )}
          </PanelSection>

          {/* GOG Store Section */}
          <PanelSection title="GOG">
            <PanelSectionRow>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}>
                <div style={{ fontSize: "14px" }}>
                  Status: {
                    storeStatus.gog === "Connected" ? "✓ Connected" :
                      storeStatus.gog === "Checking..." ? "Checking..." :
                        storeStatus.gog.includes("Error") ? `❌ ${storeStatus.gog}` :
                          "✗ Not Connected"
                  }
                </div>
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <div>
                {storeStatus.gog === "Connected" ? (
                  <ButtonItem layout="below" onClick={() => handleLogout('gog')}>
                    <div style={{ fontSize: "0.85em", padding: "2px" }}>Logout</div>
                  </ButtonItem>
                ) : storeStatus.gog !== "Checking..." && !storeStatus.gog.includes("Error") ? (
                  <ButtonItem layout="below" onClick={() => startAuth('gog')}>
                    <div style={{ fontSize: "0.85em", padding: "2px" }}>Authenticate</div>
                  </ButtonItem>
                ) : null}
              </div>
            </PanelSectionRow>
          </PanelSection>

          {/* Amazon Games Section */}
          <PanelSection title="AMAZON GAMES">
            <PanelSectionRow>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}>
                <div style={{ fontSize: "14px" }}>
                  Status: {
                    storeStatus.amazon === "Connected" ? "✓ Connected" :
                      storeStatus.amazon === "Nile not installed" ? "⚠️ Missing CLI" :
                        storeStatus.amazon === "Checking..." ? "Checking..." :
                          storeStatus.amazon.includes("Error") ? `❌ ${storeStatus.amazon}` :
                            "✗ Not Connected"
                  }
                </div>
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <div>
                {storeStatus.amazon === "Connected" ? (
                  <ButtonItem layout="below" onClick={() => handleLogout('amazon')}>
                    <div style={{ fontSize: "0.85em", padding: "2px" }}>Logout</div>
                  </ButtonItem>
                ) : storeStatus.amazon !== "Checking..." && !storeStatus.amazon.includes("Error") && storeStatus.amazon !== "Nile not installed" ? (
                  <ButtonItem layout="below" onClick={() => startAuth('amazon')}>
                    <div style={{ fontSize: "0.85em", padding: "2px" }}>Authenticate</div>
                  </ButtonItem>
                ) : null}
              </div>
            </PanelSectionRow>
            {storeStatus.amazon === "Nile not installed" && (
              <PanelSectionRow>
                <div style={{ fontSize: '11px', opacity: 0.7 }}>
                  Nile CLI not found. Amazon Games unavailable.
                </div>
              </PanelSectionRow>
            )}
          </PanelSection>

          {/* Library Sync Section */}
          <PanelSection title="LIBRARY SYNC">
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={() => handleManualSync(false)}
                disabled={syncing || syncCooldown}
              >
                <div style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "2px",
                  justifyContent: "center",
                  fontSize: "0.85em",
                  padding: "2px"
                }}>
                  <FaSync style={{
                    animation: syncing ? "spin 1s linear infinite" : "none",
                    opacity: syncCooldown ? 0.5 : 1,
                    fontSize: "10px"
                  }} />
                  {syncing
                    ? "Syncing..."
                    : syncCooldown
                      ? `${cooldownSeconds}s`
                      : "Sync Libraries"}
                </div>
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={() => handleManualSync(true)}
                disabled={syncing || syncCooldown}
              >
                <div style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "2px",
                  justifyContent: "center",
                  color: "#ff9800",
                  fontSize: "0.85em",
                  padding: "2px"
                }}>
                  <FaSync style={{
                    animation: syncing ? "spin 1s linear infinite" : "none",
                    opacity: syncCooldown ? 0.5 : 1,
                    fontSize: "10px"
                  }} />
                  {syncing
                    ? "..."
                    : syncCooldown
                      ? `${cooldownSeconds}s`
                      : "Force Sync"}
                </div>
              </ButtonItem>
            </PanelSectionRow>

            {/* Cancel button - only visible during sync */}
            {syncing && (
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  onClick={handleCancelSync}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: "8px", color: "#ff6b6b" }}>
                    Cancel Sync
                  </div>
                </ButtonItem>
              </PanelSectionRow>
            )}

            {/* Progress display */}
            {syncProgress && syncProgress.status !== 'idle' && (
              <PanelSectionRow>
                <div style={{ fontSize: '12px', width: '100%' }}>
                  {/* Status text */}
                  <div style={{ marginBottom: '5px', opacity: 0.9 }}>
                    {syncProgress.current_game}
                  </div>

                  {/* Progress bar */}
                  <div style={{
                    width: '100%',
                    height: '4px',
                    backgroundColor: '#333',
                    borderRadius: '2px',
                    overflow: 'hidden'
                  }}>
                    <div style={{
                      width: `${syncProgress.progress_percent}%`,
                      height: '100%',
                      backgroundColor:
                        syncProgress.status === 'error' ? '#ff6b6b' :
                          syncProgress.status === 'complete' ? '#4caf50' :
                            syncProgress.current_phase === 'artwork' ? '#ff9800' : // Orange for artwork
                              '#1a9fff', // Blue for sync
                      transition: 'width 0.3s ease'
                    }} />
                  </div>

                  {/* Stats - different based on phase */}
                  <div style={{ marginTop: '5px', opacity: 0.7 }}>
                    {syncProgress.current_phase === 'artwork' ? (
                      // Artwork phase: show artwork progress
                      <>
                        {syncProgress.artwork_synced} / {syncProgress.artwork_total} artwork downloaded
                      </>
                    ) : (
                      // Sync phase: show game progress
                      <>
                        {syncProgress.synced_games} / {syncProgress.total_games} games synced
                      </>
                    )}
                  </div>

                  {/* Error message */}
                  {syncProgress.error && (
                    <div style={{ color: '#ff6b6b', marginTop: '5px' }}>
                      Error: {syncProgress.error}
                    </div>
                  )}
                </div>
              </PanelSectionRow>
            )}

            {(storeStatus.epic.includes("Error") || storeStatus.gog.includes("Error")) && (
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={checkStoreStatus}>
                  Retry Status Check
                </ButtonItem>
              </PanelSectionRow>
            )}
          </PanelSection>

          {/* Cleanup Section */}
          <PanelSection title="Cleanup">
            {!showDeleteConfirm ? (
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  onClick={handleDeleteAll}
                  disabled={syncing || deleting || syncCooldown}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: "2px", fontSize: "0.85em", padding: "2px" }}>
                    <FaTrash style={{ fontSize: "10px" }} />
                    Delete all UNIFIDECK Libraries and Cache
                  </div>
                </ButtonItem>
              </PanelSectionRow>
            ) : (
              <>
                <PanelSectionRow>
                  <div style={{ color: "#ff6b6b", fontWeight: "bold" }}>
                    Are you sure? This will delete ALL Unifideck games, artwork, auth tokens, and cache.
                    This action is irreversible.
                  </div>
                </PanelSectionRow>

                {/* Delete Files Checkbox */}
                <PanelSectionRow>
                  <div style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "10px",
                    margin: "10px 0"
                  }}>
                    <ToggleField
                      label="Also delete installed game files? (Destructive)"
                      checked={deleteFiles}
                      onChange={(checked) => setDeleteFiles(checked)}
                    />
                  </div>
                </PanelSectionRow>

                <PanelSectionRow>
                  <ButtonItem
                    layout="below"
                    onClick={handleDeleteAll}
                    disabled={deleting}
                  >
                    <div style={{ color: "#ff6b6b", fontSize: "0.85em", padding: "2px" }}>
                      {deleting ? "Deleting..." : "Yes, Delete Everything"}
                    </div>
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
                    <div style={{ fontSize: "0.85em", padding: "2px" }}>Cancel</div>
                  </ButtonItem>
                </PanelSectionRow>
              </>
            )}
          </PanelSection>

          {/* Authentication Dialog */}
          {authDialog.show && (
            <div style={{
              position: 'fixed',
              top: 0,
              left: 0,
              right: 0,
              bottom: 0,
              backgroundColor: 'rgba(0, 0, 0, 0.8)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 10000
            }}>
              <div style={{
                backgroundColor: '#1e2329',
                padding: '20px',
                borderRadius: '8px',
                maxWidth: '500px',
                width: '90%',
                maxHeight: '80vh',
                overflow: 'auto'
              }}>
                <h2 style={{ marginTop: 0 }}>
                  {authDialog.store === 'epic' ? 'Epic Games' : authDialog.store === 'amazon' ? 'Amazon Games' : 'GOG'} Authentication
                </h2>

                <div>
                  <div style={{ marginBottom: '15px', fontSize: '14px' }}>
                    {authDialog.processing ? (
                      <div>
                        <p>Please complete the login in the popup window.</p>
                        <p style={{ fontSize: '0.9em', color: '#888', marginTop: '5px' }}>
                          The window will close automatically after authentication.
                        </p>
                        <div style={{ marginTop: '20px', textAlign: 'center' }}>
                          <div style={{ fontSize: '32px' }}>⏳</div>
                          <p style={{ fontSize: '12px', opacity: 0.7, marginTop: '10px' }}>
                            Waiting for authentication...
                          </p>
                        </div>
                      </div>
                    ) : (
                      <div>
                        <p>✓ Ready to authenticate</p>
                      </div>
                    )}
                  </div>

                  {authDialog.error && (
                    <div style={{
                      marginBottom: '15px',
                      padding: '10px',
                      backgroundColor: '#5c1f1f',
                      borderRadius: '4px',
                      fontSize: '13px'
                    }}>
                      {authDialog.error}
                      {authDialog.error.includes('cross-origin') && (
                        <p style={{ fontSize: '0.8em', marginTop: '5px' }}>
                          The popup closed before authentication could complete. Please try again.
                        </p>
                      )}
                    </div>
                  )}

                  <div style={{ display: 'flex', gap: '10px' }}>
                    <button
                      onClick={() => setAuthDialog({ show: false, store: null, url: '', code: '', processing: false, error: '', autoMode: false })}
                      style={{
                        flex: 1,
                        padding: '10px',
                        backgroundColor: '#3d4450',
                        border: 'none',
                        borderRadius: '4px',
                        color: 'white',
                        cursor: 'pointer'
                      }}
                    >
                      {authDialog.processing ? 'Cancel' : 'Close'}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </>
  );
};

export default definePlugin(() => {
  console.log("[Unifideck] Plugin loaded");

  // Patch the library to add Unifideck tabs (All, Installed, Great on Deck, Steam, Epic, GOG, Amazon)
  // This uses TabMaster's approach: intercept useMemo hook to inject custom tabs
  const libraryPatch = patchLibrary();
  console.log("[Unifideck] ✓ Library tabs patch registered");

  // Patch game details route to inject Install button for uninstalled games
  // v70.3 FIX: Call extracted function to ensure proper Decky loader context
  const patchGameDetails = patchGameDetailsRoute();

  console.log("[Unifideck] ✓ All route patches registered (including game details)");

  // Sync Unifideck Collections on load (with delay to ensure Steam is ready)
  setTimeout(async () => {
    console.log("[Unifideck] Triggering initial collection sync...");
    try {
      await syncUnifideckCollections();
      console.log("[Unifideck] ✓ Initial collection sync complete");
    } catch (err) {
      console.error("[Unifideck] Initial collection sync failed:", err);
    }
  }, 5000); // 5 second delay to ensure Steam is fully loaded

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

  // Background sync disabled - users manually sync via UI when needed
  console.log("[Unifideck] Background sync disabled (use manual sync button)");

  return {
    name: "UNIFIDECK",
    icon: <FaGamepad />,
    content: <Content />,
    onDismount() {
      console.log("[Unifideck] Plugin unloading");



      // Remove CSS injection
      const styleEl = document.getElementById("unifideck-tab-hider");
      if (styleEl) {
        styleEl.remove();
      }

      // Remove route patches
      routerHook.removePatch("/library", libraryPatch);
      routerHook.removePatch("/library/app/:appid", patchGameDetails);

      // Clear game info cache
      gameInfoCache.clear();

      // Stop background sync service
      call("stop_background_sync")
        .catch((error) => console.error("[Unifideck] Failed to stop background sync:", error));
    },
  };
});
