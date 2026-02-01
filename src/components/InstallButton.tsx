/**
 * InstallButton Component
 *
 * Displays install/uninstall/cancel button with download progress
 * for Unifideck games in the Steam game details page.
 */

import React, { FC, useState, useEffect } from "react";
import { call, toaster } from "@decky/api";
import { DialogButton, showModal, ConfirmModal } from "@decky/ui";
import { useTranslation } from "react-i18next";

import {
  useGameInfo,
  invalidateGameInfoCache,
  refreshGameInfo,
  GameInfo,
} from "../hooks/useGameInfo";
import { useDownloadState } from "../hooks/useDownloadState";
import { updateSingleGameStatus } from "../tabs";
import { UninstallConfirmModal } from "./UninstallConfirmModal";
import StoreIcon from "./StoreIcon";

export const InstallButton: FC<{ appId: number }> = ({ appId }) => {
  const [processing, setProcessing] = useState(false);
  const [localGameInfo, setLocalGameInfo] = useState<GameInfo | null>(null);

  const { t } = useTranslation();

  // Use custom hooks for data fetching
  const gameInfo = useGameInfo(appId);

  // Handle download completion
  const handleDownloadComplete = async () => {
    if (!gameInfo) return;

    // Show installation complete toast
    toaster.toast({
      title: t("toasts.installComplete"),
      body: t("toasts.installCompleteMessage", {
        title: gameInfo.title || "Game",
      }),
      duration: 10000,
      critical: true,
    });

    // Refresh game info to update button state
    const updatedInfo = await refreshGameInfo(appId);
    if (updatedInfo) {
      setLocalGameInfo(updatedInfo);
      // Update tab cache immediately so UI reflects change
      updateSingleGameStatus({
        appId,
        store: updatedInfo.store,
        isInstalled: updatedInfo.is_installed,
      });
    }
  };

  const downloadState = useDownloadState(gameInfo, handleDownloadComplete);

  // Sync local state with hook-provided gameInfo
  useEffect(() => {
    setLocalGameInfo(gameInfo);
  }, [gameInfo]);

  const displayInfo = localGameInfo || gameInfo;

  const handleInstall = async () => {
    if (!displayInfo) return;
    setProcessing(true);

    // Queue download instead of direct install
    const result = await call<[number], any>(
      "add_to_download_queue_by_appid",
      appId,
    );

    if (result.success) {
      toaster.toast({
        title: t("toasts.downloadStarted"),
        body: t("toasts.downloadQueued", { title: displayInfo.title }),
        duration: 5000,
      });

      // Show multi-part alert for GOG games with multiple installer parts
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
        body: result.error
          ? t(result.error)
          : t("toasts.downloadFailedMessage"),
        duration: 10000,
        critical: true,
      });
    }
    setProcessing(false);
  };

  const handleCancel = async () => {
    if (!displayInfo) return;

    // If we don't have a specific download ID yet (race condition at start), try to construct it
    const dlId =
      downloadState.downloadId || `${displayInfo.store}:${displayInfo.game_id}`;

    setProcessing(true);

    const result = await call<[string], { success: boolean; error?: string }>(
      "cancel_download_by_id",
      dlId,
    );

    if (result.success) {
      toaster.toast({
        title: t("toasts.downloadCancelled"),
        body: t("toasts.downloadCancelledMessage", {
          title: displayInfo?.title,
        }),
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
    setProcessing(false);
  };

  const handleUninstall = async (deletePrefix: boolean = false) => {
    if (!displayInfo) return;
    setProcessing(true);

    toaster.toast({
      title: t("toasts.uninstalling"),
      body: deletePrefix
        ? t("toasts.uninstallingMessageProton", { title: displayInfo.title })
        : t("toasts.uninstallingMessage", { title: displayInfo.title }),
      duration: 5000,
    });

    const result = await call<[number, boolean], any>(
      "uninstall_game_by_appid",
      appId,
      deletePrefix,
    );

    if (result.success) {
      // Update local state to reflect uninstallation
      setLocalGameInfo({ ...displayInfo, is_installed: false });
      invalidateGameInfoCache(appId);

      // Update tab cache immediately so UI reflects change without restart
      if (result.game_update) {
        updateSingleGameStatus(result.game_update);
      }

      toaster.toast({
        title: t("toasts.uninstallComplete"),
        body: deletePrefix
          ? t("toasts.uninstallCompleteMessageProton", {
              title: displayInfo.title,
            })
          : t("toasts.uninstallCompleteMessage", { title: displayInfo.title }),
        duration: 10000,
      });
    }
    setProcessing(false);
  };

  // Confirmation wrapper functions using native Steam modal
  const showInstallConfirmation = () => {
    showModal(
      <ConfirmModal
        strTitle={t("confirmModals.installTitle")}
        strDescription={t("confirmModals.installDescription", {
          title: displayInfo?.title,
        })}
        strOKButtonText={t("confirmModals.yes")}
        strCancelButtonText={t("confirmModals.no")}
        onOK={() => handleInstall()}
      />,
    );
  };

  const showUninstallConfirmation = () => {
    showModal(
      <UninstallConfirmModal
        gameTitle={displayInfo?.title || "this game"}
        onConfirm={(deletePrefix) => handleUninstall(deletePrefix)}
      />,
    );
  };

  const showCancelConfirmation = () => {
    showModal(
      <ConfirmModal
        strTitle={t("confirmModals.cancelTitle")}
        strDescription={t("confirmModals.cancelDescription", {
          title: displayInfo?.title,
        })}
        strOKButtonText={t("confirmModals.yes")}
        strCancelButtonText={t("confirmModals.no")}
        bDestructiveWarning={true}
        onOK={() => handleCancel()}
      />,
    );
  };

  // Not a Unifideck game - return null
  if (!displayInfo || displayInfo.error) return null;

  const isInstalled = displayInfo.is_installed;

  // Determine button display based on state
  let buttonText: string;
  let buttonAction: () => void;

  // Base button style - ROBUST AGAINST CSS MODS
  // Uses explicit colors and high specificity to override any Decky CSS themes
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
    // Explicit visibility overrides for CSS mod resistance
    opacity: 1,
    visibility: "visible",
    // Remove any inherited transparency
    backdropFilter: "none",
    WebkitBackdropFilter: "none",
  };

  // Dynamic style based on state
  let buttonStyle: React.CSSProperties;

  if (downloadState.isDownloading) {
    // Show "Cancel" button with progress during active download
    const progress = Math.max(0, downloadState.progress || 0).toFixed(0);
    buttonText = `${t("installButton.cancel")} (${progress}%)`;
    buttonAction = showCancelConfirmation;

    buttonStyle = {
      ...baseButtonStyle,
      backgroundColor: "#dc3545",
      color: "#ffffff",
      border: "2px solid #ff6b6b",
      boxShadow: "0 2px 8px rgba(220, 53, 69, 0.5)",
    };
  } else if (isInstalled) {
    // Show size for installed games if available
    const sizeText = displayInfo.size_formatted
      ? ` (${displayInfo.size_formatted})`
      : " (- GB)";
    buttonText =
      t("installButton.uninstall", { title: displayInfo.title }) + sizeText;
    buttonAction = showUninstallConfirmation;

    buttonStyle = {
      ...baseButtonStyle,
      backgroundColor: "#4a5568",
      color: "#ffffff",
      border: "2px solid #718096",
      boxShadow: "0 2px 8px rgba(74, 85, 104, 0.5)",
    };
  } else {
    // Show size in Install button
    const sizeText = displayInfo.size_formatted
      ? ` (${displayInfo.size_formatted})`
      : " (- GB)";
    buttonText =
      t("installButton.install", { title: displayInfo.title }) + sizeText;
    buttonAction = showInstallConfirmation;

    buttonStyle = {
      ...baseButtonStyle,
      backgroundColor: "#1a9fff",
      color: "#ffffff",
      border: "2px solid #47b4ff",
      boxShadow: "0 2px 8px rgba(26, 159, 255, 0.5)",
    };
  }

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
          onClick={buttonAction}
          disabled={processing}
          style={buttonStyle}
          className="unifideck-install-button"
        >
          {processing ? (
            t("installButton.processing")
          ) : (
            <>
              <StoreIcon
                store={displayInfo.store}
                size="16px"
                color="#ffffff"
              />
              {buttonText}
            </>
          )}
        </DialogButton>
      </div>
    </>
  );
};
