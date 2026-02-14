/**
 * Game Action Interceptor
 *
 * Intercepts Steam's play button presses for Unifideck games:
 * 1. Uninstalled games: cancels launch and triggers install flow
 * 2. Downloading games: cancels launch and shows cancel confirmation
 * 3. Installed games: let Steam handle it normally
 *
 * Strategy: Only cancel when we KNOW from cache that the game is a Unifideck game.
 *
 * Based on MoonDeck's RegisterForGameActionStart pattern.
 */

import { call, toaster } from "@decky/api";
import { ConfirmModal, showModal } from "@decky/ui";
import React from "react";
import { GOGLanguageSelectModal } from "../components/GOGLanguageSelectModal";
import i18n from "i18next";

// Reference to the shared game info cache from index.tsx
let gameInfoCacheRef: Map<number, { info: any; timestamp: number }> | null = null;
const CACHE_TTL = 5000;

export function setGameInfoCacheRef(cache: Map<number, { info: any; timestamp: number }>) {
  gameInfoCacheRef = cache;
}

// Download state shared from PlayButtonOverride
let downloadStateRef: {
  isDownloading: boolean;
  downloadId?: string;
  gameInfo?: any;
} = { isDownloading: false };

export function setDownloadStateRef(state: {
  isDownloading: boolean;
  downloadId?: string;
  gameInfo?: any;
}) {
  downloadStateRef = state;
}

/**
 * Check cache synchronously (no async). Returns cached info or null.
 */
function getGameInfoFromCache(appId: number): any | null {
  if (gameInfoCacheRef) {
    const cached = gameInfoCacheRef.get(appId);
    if (cached && Date.now() - cached.timestamp < CACHE_TTL) {
      return cached.info;
    }
  }
  return null;
}

/**
 * Start download with optional language parameter.
 */
async function startDownload(gameInfo: any, language?: string) {
  const t = i18n.t.bind(i18n);

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

    if (result.is_multipart) {
      toaster.toast({
        title: t("toasts.multipartDetected"),
        body: t("toasts.multipartMessage"),
        duration: 8000,
      });
    }
  } else {
    toaster.toast({
      title: t("toasts.downloadFailed"),
      body: result.error ? t(result.error) : t("toasts.downloadFailedMessage"),
      duration: 10000,
      critical: true,
    });
  }
}

/**
 * Handle install flow for a game (GOG language check + confirmation modal + download).
 */
async function handleInstallFlow(gameInfo: any) {
  // For GOG games, check if multiple languages are available
  if (gameInfo.store === "gog") {
    try {
      const langResult = await call<
        [string],
        { success: boolean; languages: string[]; error?: string }
      >("get_gog_game_languages", gameInfo.game_id);

      const languages = langResult?.languages;
      if (!langResult?.success || !Array.isArray(languages)) {
        console.warn("[GameActionInterceptor] Invalid language response, falling back to default");
        startDownload(gameInfo);
        return;
      }

      if (languages.length > 1) {
        showModal(
          React.createElement(GOGLanguageSelectModal, {
            gameTitle: gameInfo.title,
            languages: languages,
            onConfirm: (selectedLang: string) => startDownload(gameInfo, selectedLang),
          }),
        );
        return;
      }

      startDownload(gameInfo, languages[0] || undefined);
      return;
    } catch (error) {
      console.error("[GameActionInterceptor] Error fetching GOG languages:", error);
    }
  }

  // Non-GOG games or fallback
  startDownload(gameInfo);
}

/**
 * Show install confirmation modal, then trigger install flow.
 */
function showInstallConfirmation(gameInfo: any) {
  const t = i18n.t.bind(i18n);

  showModal(
    React.createElement(ConfirmModal, {
      strTitle: t("confirmModals.installTitle"),
      strDescription: t("confirmModals.installDescription", { title: gameInfo.title }),
      strOKButtonText: t("confirmModals.yes"),
      strCancelButtonText: t("confirmModals.no"),
      onOK: () => {
        handleInstallFlow(gameInfo);
        return true;
      },
    }),
  );
}

/**
 * Show cancel confirmation modal, then cancel the download.
 */
function showCancelConfirmation(gameInfo: any, downloadId?: string) {
  const t = i18n.t.bind(i18n);
  const dlId = downloadId || `${gameInfo.store}:${gameInfo.game_id}`;

  showModal(
    React.createElement(ConfirmModal, {
      strTitle: t("confirmModals.cancelTitle"),
      strDescription: t("confirmModals.cancelDescription", { title: gameInfo.title }),
      strOKButtonText: t("confirmModals.yes"),
      strCancelButtonText: t("confirmModals.no"),
      bDestructiveWarning: true,
      onOK: async () => {
        const result = await call<[string], { success: boolean; error?: string }>(
          "cancel_download_by_id",
          dlId,
        );

        if (result.success) {
          toaster.toast({
            title: t("toasts.downloadCancelled"),
            body: t("toasts.downloadCancelledMessage", { title: gameInfo.title }),
            duration: 5000,
          });
        } else {
          toaster.toast({
            title: t("toasts.cancelFailed"),
            body: result.error ? t(result.error) : t("toasts.cancelFailedMessage"),
            duration: 5000,
            critical: true,
          });
        }
      },
    }),
  );
}

/**
 * Registers the game action interceptor.
 *
 * Strategy: Only act on games we KNOW from cache are Unifideck games.
 * - Installed: let Steam handle it normally
 * - Downloading: cancel + show cancel confirmation
 * - Uninstalled: cancel + show install confirmation
 * - Unknown (no cache): let Steam handle it
 *
 * @returns Cleanup function to unregister the interceptor.
 */
export function registerGameActionInterceptor(): () => void {
  try {
    const unregisterable = window.SteamClient?.Apps?.RegisterForGameActionStart(
      (gameActionId: number, appIdStr: string, action: string) => {
        if (action !== "LaunchApp") return;

        const appId = parseInt(appIdStr, 10);
        if (isNaN(appId)) return;

        // Only intercept non-Steam shortcuts (appId > 2 billion)
        if (appId <= 2000000000) return;

        // Check cache SYNCHRONOUSLY - if no cache, let the launch proceed
        const cachedInfo = getGameInfoFromCache(appId);
        if (!cachedInfo) {
          return;
        }

        // Game is installed and not downloading - let Steam handle it normally
        if (cachedInfo.is_installed && !downloadStateRef.isDownloading) {
          return;
        }

        // Game is downloading - cancel launch and show cancel confirmation
        if (downloadStateRef.isDownloading) {
          console.log(
            `[GameActionInterceptor] Cancelling launch for downloading game: ${cachedInfo.title} (${appId})`,
          );
          window.SteamClient?.Apps?.CancelGameAction(gameActionId);
          showCancelConfirmation(
            downloadStateRef.gameInfo || cachedInfo,
            downloadStateRef.downloadId,
          );
          return;
        }

        // Game is NOT installed - cancel the launch and show install flow
        console.log(
          `[GameActionInterceptor] Cancelling launch for uninstalled game: ${cachedInfo.title} (${appId})`,
        );
        window.SteamClient?.Apps?.CancelGameAction(gameActionId);
        showInstallConfirmation(cachedInfo);
      },
    );

    if (unregisterable) {
      console.log("[GameActionInterceptor] Registered game action interceptor");
      return () => {
        unregisterable.unregister();
        console.log("[GameActionInterceptor] Unregistered game action interceptor");
      };
    }
  } catch (error) {
    console.error("[GameActionInterceptor] Failed to register:", error);
  }

  return () => {};
}
