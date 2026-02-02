import React from "react";
import { PanelSection, PanelSectionRow, ButtonItem } from "@decky/ui";
import { FaSync } from "react-icons/fa";
import { t } from "../../i18n";
import ForceSyncModal from "../ForceSyncModal";
import { SyncProgress } from "../../types/syncProgress";

interface LibrarySyncProps {
  syncing: boolean;
  syncCooldown: boolean;
  cooldownSeconds: number;
  syncProgress: SyncProgress | null;
  storeStatus: {
    epic: string;
    gog: string;
    amazon: string;
  };
  handleManualSync: (force?: boolean, resyncArtwork?: boolean) => void;
  handleCancelSync: () => void;
  showModal: (content: React.ReactNode) => void;
  checkStoreStatus: () => void;
}

const LibrarySync: React.FC<LibrarySyncProps> = ({
  syncing,
  syncCooldown,
  cooldownSeconds,
  syncProgress,
  storeStatus,
  handleManualSync,
  handleCancelSync,
  showModal,
  checkStoreStatus,
}) => {
  return (
    <PanelSection title={t("librarySync.title")}>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={() => handleManualSync(false)}
          disabled={syncing || syncCooldown}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              justifyContent: "center",
            }}
          >
            <FaSync
              style={{
                animation: syncing ? "spin 1s linear infinite" : "none",
                opacity: syncCooldown ? 0.5 : 1,
              }}
            />
            {syncing
              ? t("librarySync.syncing")
              : syncCooldown
              ? `${cooldownSeconds}s`
              : t("librarySync.syncLibraries")}
          </div>
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={() => {
            showModal(
              <ForceSyncModal
                onResyncArtwork={() => handleManualSync(true, true)}
                onKeepArtwork={() => handleManualSync(true, false)}
              />,
            );
          }}
          disabled={syncing || syncCooldown}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              justifyContent: "center",
            }}
          >
            <FaSync
              style={{
                animation: syncing ? "spin 1s linear infinite" : "none",
                opacity: syncCooldown ? 0.5 : 1,
              }}
            />
            {syncing
              ? "..."
              : syncCooldown
              ? `${cooldownSeconds}s`
              : t("librarySync.forceSync")}
          </div>
        </ButtonItem>
      </PanelSectionRow>

      {/* Cancel button - only visible during sync */}
      {syncing && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleCancelSync}>
            {t("librarySync.cancelSync")}
          </ButtonItem>
        </PanelSectionRow>
      )}

      {/* Progress display */}
      {syncProgress && syncProgress.status !== "idle" && (
        <div style={{ fontSize: "12px", width: "100%" }}>
          {/* Status text */}
          <div style={{ marginBottom: "5px", opacity: 0.9 }}>
            {t(
              syncProgress.current_game.label,
              syncProgress.current_game.values,
            )}
          </div>

          {/* Progress bar */}
          <div
            style={{
              width: "100%",
              height: "4px",
              backgroundColor: "#333",
              borderRadius: "2px",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                width: `${syncProgress.progress_percent}%`,
                height: "100%",
                backgroundColor:
                  syncProgress.status === "error"
                    ? "#ff6b6b"
                    : syncProgress.status === "complete"
                    ? "#4caf50"
                    : syncProgress.current_phase === "artwork"
                    ? "#ff9800" // Orange for artwork
                    : "#1a9fff", // Blue for sync
                transition: "width 0.3s ease",
              }}
            />
          </div>

          {/* Stats - different based on phase */}
          <div style={{ marginTop: "5px", opacity: 0.7 }}>
            {syncProgress.current_phase === "artwork" ? (
              // Artwork phase: show artwork progress
              <>
                {t("librarySync.artworkDownloaded", {
                  synced: syncProgress.artwork_synced || 0,
                  total: syncProgress.artwork_total || 0,
                })}
              </>
            ) : (
              // Sync phase: show game progress
              <>
                {t("librarySync.gamesSynced", {
                  synced: syncProgress.synced_games || 0,
                  total: syncProgress.total_games || 0,
                })}
                {(syncProgress.steam_total || 0) > 0 && (
                  <div>
                    {t("librarySync.steamMetadataDownloaded", {
                      synced: syncProgress.steam_synced || 0,
                      total: syncProgress.steam_total || 0,
                    })}
                  </div>
                )}
                {(syncProgress.unifidb_total || 0) > 0 && (
                  <div>
                    {t("librarySync.unifidbMetadataDownloaded", {
                      synced: syncProgress.unifidb_synced || 0,
                      total: syncProgress.unifidb_total || 0,
                    })}
                  </div>
                )}
                {(syncProgress.metacritic_total || 0) > 0 && (
                  <div>
                    {t("librarySync.metacriticMetadataDownloaded", {
                      synced: syncProgress.metacritic_synced || 0,
                      total: syncProgress.metacritic_total || 0,
                    })}
                  </div>
                )}
                {(syncProgress.rawg_total || 0) > 0 && (
                  <div>
                    {t("librarySync.rawgMetadataDownloaded", {
                      synced: syncProgress.rawg_synced || 0,
                      total: syncProgress.rawg_total || 0,
                    })}
                  </div>
                )}
              </>
            )}
          </div>

          {/* Error message */}
          {syncProgress.error && (
            <div style={{ color: "#ff6b6b", marginTop: "5px" }}>
              Error: {syncProgress.error}
            </div>
          )}
        </div>
      )}

      {(storeStatus.epic.includes("Error") ||
        storeStatus.gog.includes("Error")) && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={checkStoreStatus}>
            {t("librarySync.retryStatusCheck")}
          </ButtonItem>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
};

export default LibrarySync;
