import { definePlugin, call, toaster, routerHook } from "@decky/api";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  DialogButton,
  ToggleField,
  showModal,
  ConfirmModal,
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

// Import Steam patching utilities
import { patchGameDetailsRoute } from "./patching/GameDetailsPatching";

// Import hooks
import { clearGameInfoCache } from "./hooks/useGameInfo";

// Import Downloads feature components
import { DownloadsTab } from "./components/DownloadsTab";
import { StorageSettings } from "./components/StorageSettings";
import { UninstallConfirmModal } from "./components/UninstallConfirmModal";
import { LanguageSelector } from "./components/LanguageSelector";
import { SettingsTab } from "./components/settings/SettingsTab";
import { InstallButton } from "./components/InstallButton";
import StoreConnections from "./components/settings/StoreConnections";
import { Store } from "./types/store";
import LibrarySync from "./components/settings/LibrarySync";
import StoreIcon from "./components/StoreIcon";
import GameInfoPanel from "./components/GameInfoPanel";
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

// ========== END INSTALL BUTTON FEATURE ==========

// ========== NATIVE PLAY BUTTON OVERRIDE ==========
//
// This component shows alongside the native Play button for uninstalled Unifideck games.
// For installed games, we hide this and let Steam's native Play button work.
// For uninstalled games, we show an Install button with size info.
//
// ================================================

// Install button component is now in src/components/InstallButton.tsx

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
    epic: "not_connected",
    gog: "not_connected",
    amazon: "not_connected",
  });

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
    console.log("[Unifideck] Component mounted, calling checkStoreStatus");
    checkStoreStatus().catch((error) => {
      console.error(
        "[Unifideck] Failed to check store status on mount:",
        error,
      );
      // Set to not_connected so buttons show even on error
      setStoreStatus({
        epic: "not_connected",
        gog: "not_connected",
        amazon: "not_connected",
      });
    });
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

                    // Show toast only if changes were made
                    if (result.status === "complete") {
                      const addedCount = result.synced_games || 0;
                      if (addedCount > 0) {
                        toaster.toast({
                          title: t("toasts.syncComplete"),
                          body: t("toasts.syncCompleteMessage", {
                            count: addedCount,
                          }),
                          duration: 15000,
                          critical: true,
                        });
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
    console.log("[Unifideck] Starting store status check...");
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

      console.log("[Unifideck] Store status result:", result);

      if (result.success) {
        const newStatus = {
          epic: result.epic,
          gog: result.gog,
          amazon: result.amazon,
        };
        console.log("[Unifideck] Setting store status to:", newStatus);
        setStoreStatus(newStatus);
        // Cache the status in localStorage
        try {
          localStorage.setItem(
            "unifideck_store_status",
            JSON.stringify(newStatus),
          );
        } catch (e) {
          console.error("[Unifideck] Failed to cache store status:", e);
        }

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
        // Use not_connected instead of error so buttons show
        setStoreStatus({
          epic: "not_connected",
          gog: "not_connected",
          amazon: "not_connected",
        });
      }
    } catch (error) {
      console.error("[Unifideck] Error checking store status:", error);
      // Use not_connected instead of error so buttons show
      setStoreStatus({
        epic: "not_connected",
        gog: "not_connected",
        amazon: "not_connected",
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

            // Show restart notification when sync completes (only if changes were made)
            if (result.status === "complete") {
              // Only show toast if there were actual changes (not just a refresh that added 0 games)
              const addedCount = result.synced_games || 0;
              if (addedCount > 0) {
                toaster.toast({
                  title: force
                    ? t("toasts.forceSyncComplete")
                    : t("toasts.syncComplete"),
                  body: force
                    ? t("toasts.forceSyncCompleteMessage", {
                        count: addedCount,
                      })
                    : t("toasts.syncCompleteMessage", { count: addedCount }),
                  duration: 15000,
                  critical: true,
                });
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
        <SettingsTab
          storeStatus={storeStatus}
          onStartAuth={startAuth}
          onLogout={handleLogout}
          syncing={syncing}
          syncCooldown={syncCooldown}
          cooldownSeconds={cooldownSeconds}
          syncProgress={syncProgress}
          handleManualSync={handleManualSync}
          handleCancelSync={handleCancelSync}
          showModal={showModal}
          checkStoreStatus={checkStoreStatus}
        />
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

  // DISABLED: Store patching was causing Steam to hang on startup
  // TODO: Re-enable once we figure out what's breaking Steam
  // loadSteamAppIdMappings().then(() => {
  //   unpatchSteamStores = patchSteamStores();
  // });

  // Patch the library to add Unifideck tabs (All, Installed, Great on Deck, Steam, Epic, GOG, Amazon)
  // This uses TabMaster's approach: intercept useMemo hook to inject custom tabs
  const libraryPatch = patchLibrary();
  console.log("[Unifideck] ✓ Library tabs patch registered");

  // Patch game details route to inject Install button for uninstalled games
  // Pass the components to the patching function
  const patchGameDetails = patchGameDetailsRoute(InstallButton, GameInfoPanel);

  console.log(
    "[Unifideck] ✓ All route patches registered (including game details)",
  );

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

      // Remove CSS injection
      const styleEl = document.getElementById("unifideck-tab-hider");
      if (styleEl) {
        styleEl.remove();
      }

      // Remove route patches
      routerHook.removePatch("/library", libraryPatch);
      routerHook.removePatch("/library/app/:appid", patchGameDetails);

      // Clear game info cache
      clearGameInfoCache();

      // Stop background sync service
      call("stop_background_sync").catch((error) =>
        console.error("[Unifideck] Failed to stop background sync:", error),
      );
    },
  };
});
